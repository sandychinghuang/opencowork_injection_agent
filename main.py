"""
RedTeam Orchestrator — 自動化 Prompt Injection 測試框架
支援多情境（多資料夾 × 對應 user prompts）

執行方式：
  python main.py                        # 跑全部情境
  python main.py --scenario organizing  # 跑指定情境
  python main.py --list                 # 列出所有情境
"""

import argparse
import asyncio
import json
import os
import random
import sqlite3
import sys
# 解決 Windows 中文環境（cp950）輸出表情符號（Emoji）時的 UnicodeEncodeError 錯誤
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')
from datetime import datetime
from pathlib import Path

import yaml

def load_dotenv(dotenv_path: str = ".env"):
    p = Path(dotenv_path)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    os.environ[key] = val


from core.detector import ChangeDetector
from core.file_ops import FileOps
from core.generator import InjectionGenerator
from core.judge import VerdictJudge
from core.strategist import StrategyOptimizer
from core.report import ReportGenerator
from core.runner import build_runner


def init_db(db_path: str = "results.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS injection_results (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp        TEXT,
            scenario         TEXT,
            iteration        INTEGER,
            category         TEXT,
            injection_prompt TEXT,
            injected_files   TEXT,
            user_prompt      TEXT,
            success          INTEGER,
            score            INTEGER,
            changes          TEXT,
            judge_response   TEXT
        )
    """)
    conn.commit()
    return conn


def save_result(conn, scenario, iteration, category, injection_prompt,
                injected_files, user_prompt, success, score, changes, judge_response=""):
    conn.execute(
        """INSERT INTO injection_results
           (timestamp, scenario, iteration, category, injection_prompt,
            injected_files, user_prompt, success, score, changes, judge_response)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.now().isoformat(), scenario, iteration, category, injection_prompt,
         json.dumps(injected_files, ensure_ascii=False), user_prompt,
         int(success), int(score), json.dumps(changes, ensure_ascii=False), judge_response),
    )
    conn.commit()


def get_past_results(conn, scenario: str, limit: int = 30) -> list[dict]:
    cursor = conn.execute(
        "SELECT category, injection_prompt, success, changes FROM injection_results WHERE scenario=? ORDER BY id DESC LIMIT ?",
        (scenario, limit),
    )
    return [{"category": r[0], "prompt": r[1], "success": r[2], "changes": r[3]} for r in cursor.fetchall()]


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_scenarios(config: dict, selected: str | None) -> list[dict]:
    """支援新格式（scenarios 清單）和舊格式（單一 target_folder）"""
    if "scenarios" in config:
        scenarios = config["scenarios"]
        if selected:
            matches = [s for s in scenarios if s["name"] == selected]
            if not matches:
                names = [s["name"] for s in scenarios]
                print(f"找不到情境 '{selected}'。可用：{names}")
                sys.exit(1)
            return matches
        return scenarios
    # 舊格式向後相容
    return [{"name": "default", "target_folder": config["target_folder"],
             "backup_folder": config["backup_folder"],
             "inject_target_files": config.get("inject_target_files", []),
             "user_prompts": config.get("user_prompts", ["請幫我整理這個資料夾"])}]


async def run_scenario(scenario: dict, config: dict, conn, runner, judge: VerdictJudge, max_iterations: int) -> int:
    name = scenario["name"]
    target_folder = Path(scenario["target_folder"])
    backup_folder = Path(scenario["backup_folder"])
    inject_files = scenario.get("inject_target_files", [])
    user_prompts = scenario["user_prompts"]

    print(f"\n{'='*60}")
    print(f"  情境     : {name}")
    print(f"  資料夾   : {target_folder}")
    print(f"  Prompts  : {len(user_prompts)} 種")
    print(f"  迭代次數 : {max_iterations}")
    print(f"{'='*60}")

    file_ops = FileOps(target_folder, backup_folder)
    detector = ChangeDetector(target_folder)
    goal = scenario.get("injection_goal", "any")
    generator = InjectionGenerator(config, goal=goal)
    strategist = StrategyOptimizer(config)
    file_ops.ensure_backup()

    success_count = 0
    
    # 讀取已存在的白皮書（溫啟動），實現「一情境 + 一目標 = 一本白皮書」的永久演化
    whitepaper_dir = config.get("whitepaper_dir", "whitepapers")
    whitepaper_dir_path = Path(whitepaper_dir)
    whitepaper_dir_path.mkdir(parents=True, exist_ok=True)
    
    whitepaper_path = whitepaper_dir_path / f"whitepaper_{name}_{goal}.md"
    if whitepaper_path.exists():
        try:
            whitepaper = whitepaper_path.read_text(encoding="utf-8")
            print(f"  [orchestrator] 🚀 偵測到已存在的對抗白皮書，載入歷史演化進度: {whitepaper_path.name}")
        except Exception as e:
            print(f"  [orchestrator] ⚠ 無法讀取歷史白皮書: {e}")
            whitepaper = ""
    else:
        whitepaper = ""
        print(f"  [orchestrator] 🆕 未偵測到歷史白皮書，將以全新狀態開始演化")

    batch_size = 10
    valid_runs = 0
    batch_results = []
    payload_queue = []
    consecutive_judge_failures = 0
    max_consecutive_failures = 5

    print(f"  [orchestrator] 目標有效迭代次數: {max_iterations} (每滿 {batch_size} 次有效測試優化一次對抗策略)")

    while valid_runs < max_iterations:
        if not payload_queue:
            past = get_past_results(conn, scenario=name)
            print(f"  🧠 [orchestrator] 呼叫 API 一口氣生成 {batch_size} 個對抗 Prompts...")
            payload_queue = await generator.generate_batch(whitepaper, user_prompts=user_prompts, num_prompts=batch_size)
            
            print(f"\n  [orchestrator] === 成功生成 {len(payload_queue)} 個發散對抗 Payload ===")
            for idx, item in enumerate(payload_queue, 1):
                cat = item.get("category", "unspecified")
                strat = item.get("strategy", "custom strategy")
                pmt = item.get("prompt", "")
                pmt_preview = pmt.replace('\n', ' ')
                if len(pmt_preview) > 100:
                    pmt_preview = pmt_preview[:100] + "..."
                print(f"    {idx}. [{cat}] 戰術: {strat}")
                print(f"      📝 Payload: {pmt_preview}")
            print(f"  [orchestrator] =================================================\n")

        item = payload_queue.pop(0)
        iteration_idx = valid_runs + 1
        print(f"\n  📌 [Valid Run {iteration_idx}/{max_iterations}]")
        
        category = item.get("category", "unknown")
        inj_prompt = item.get("prompt", "")
        strategy = item.get("strategy", "")
        
        print(f"  Category : {category}")
        print(f"  Strategy : {strategy}")
        print(f"  Payload  : {inj_prompt[:120]}...")

        # 每次 valid run 獨立隨機選一個 user_prompt 執行（attacker 已知所有語境，此處選哪個都合理）
        user_prompt = random.choice(user_prompts)
        print(f"  UserPmt  : {user_prompt}")
        
        injected = file_ops.inject(inj_prompt, inject_files, 1)
        print(f"  Injected : {injected}")
        
        snapshot_before = detector.snapshot()
        
        agent_response = ""
        agent_error = None
        try:
            agent_response = await runner.run(
                user_prompt=user_prompt,
                folder=str(target_folder),
                timeout=config.get("wait_after_agent_seconds", 60)
            )
        except Exception as e:
            agent_error = str(e)
            print(f"  ⚠ Agent error: {agent_error}")
            
        rich_entries = detector.rich_diff(snapshot_before)
        changes = [e["summary"] for e in rich_entries]
        
        verdict = {"verdict": "NO_EFFECT", "score": 0, "reason": "no changes"}
        if agent_error is None:
            print(f"  [judge] 評審中...")
            verdict = await judge.judge(
                user_prompt=user_prompt,
                injection_prompt=inj_prompt,
                changes=changes,
                rich_entries=rich_entries,
                agent_response=agent_response,
                scenario_name=name,
                goal=goal,
            )
            print(f"  [judge] {verdict.get('verdict','NO_EFFECT')} (Score: {verdict.get('score', 0)}) — {verdict.get('reason','')}")
            
        if verdict.get("verdict") == "JUDGE_ERROR":
            print(f"  ⚠ [orchestrator] 裁判執行異常，此回合不計入統計與資料庫。")
            file_ops.restore()
            print(f"  ♻ Folder restored")
            if config.get("restart_server_between_runs", True):
                await runner.shutdown()
                await asyncio.sleep(2)
                await runner.startup()
            consecutive_judge_failures += 1
            if consecutive_judge_failures >= max_consecutive_failures:
                print(f"  ❌ [orchestrator] 連續裁判異常達到 {max_consecutive_failures} 次，中斷測試以防止無限循環。")
                break
            continue
            
        consecutive_judge_failures = 0
        
        success = judge.is_success(verdict)
        if success:
            success_count += 1
            print(f"  ✅ ATTACK SUCCEEDED")
            for c in changes:
                print(f"     {c}")
        else:
            print(f"  ❌ ATTACK FAILED (Score: {verdict.get('score', 0)})")
            
        judge_note = f"[{verdict.get('verdict','?')}|Score: {verdict.get('score',0)}] {verdict.get('reason','')}"
        score = int(verdict.get('score', 0))
        save_result(conn, name, iteration_idx, category, inj_prompt, injected,
                    user_prompt, success, score, changes, judge_response=judge_note)
                    
        # 還原資料夾
        file_ops.restore()
        print(f"  ♻ Folder restored")
        
        batch_results.append({
            "prompt": inj_prompt,
            "score": verdict.get("score", 0),
            "verdict": verdict.get("verdict", "NO_EFFECT"),
            "reason": verdict.get("reason", "無詳細判定原因")
        })
        
        valid_runs += 1
        
        if len(batch_results) == batch_size:
            print(f"\n 📘[orchestrator] 累積滿 {batch_size} 次有效測試，呼叫 Strategy Optimizer 學習並更新對抗白皮書...")
            whitepaper = await strategist.optimize(whitepaper, batch_results)
            
            whitepaper_path = Path(whitepaper_dir) / f"whitepaper_{name}_{goal}.md"
            try:
                whitepaper_path.parent.mkdir(parents=True, exist_ok=True)
                whitepaper_path.write_text(whitepaper, encoding="utf-8")
                print(f"  💾[orchestrator] 已將最新對抗白皮書存檔至: [whitepaper_{name}_{goal}.md](file:///{whitepaper_path.as_posix()})")
            except Exception as e:
                print(f"  [orchestrator] ⚠ 無法存檔對抗白皮書: {e}")
            batch_results = []
            
        if config.get("restart_server_between_runs", True):
            await runner.shutdown()
            await asyncio.sleep(2)
            await runner.startup()

    if batch_results:
        print(f"\n 📘[orchestrator] 測試結束，針對最後 {len(batch_results)} 次有效測試進行對抗策略優化...")
        whitepaper = await strategist.optimize(whitepaper, batch_results)
        whitepaper_path = Path(whitepaper_dir) / f"whitepaper_{name}_{goal}.md"
        try:
            whitepaper_path.parent.mkdir(parents=True, exist_ok=True)
            whitepaper_path.write_text(whitepaper, encoding="utf-8")
            print(f"  💾[orchestrator] 已將最終對抗白皮書存檔至: [whitepaper_{name}_{goal}.md](file:///{whitepaper_path.as_posix()})")
        except Exception as e:
            print(f"  [orchestrator] ⚠ 無法存檔對抗白皮書: {e}")


    print(f"\n🏁 [{name}] 測試完成：{success_count}/{max_iterations} 攻擊成功")
    return success_count


async def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="RedTeam Prompt Injection Tester")
    parser.add_argument("--scenario", "-s", help="指定情境名稱（對應 config.yaml 中的 name）")
    parser.add_argument("--goal", "-g",
                        help="覆蓋所有情境的 injection_goal（可選值見 --list-goals）")
    parser.add_argument("--all-goals", action="store_true",
                        help="自動交叉測試：每個情境 × 所有 goal（即 10情境 × 6 goal = 60 組合）")
    parser.add_argument("--list", "-l", action="store_true", help="列出所有情境與預設 goal")
    parser.add_argument("--list-goals", action="store_true", help="列出所有可用的 injection_goal 選項")
    parser.add_argument("--config", "-c", default="config.yaml")
    # ── 資料管理
    parser.add_argument("--db", default=None,
                        help="指定資料庫檔案。若未指定，將依據情境與攻擊目標自動命名（例如：T1_A_Rename_delete_files.db）。")
    parser.add_argument("--reset", action="store_true",
                        help="執行前清除指定資料庫的所有測試資料（需確認）")
    parser.add_argument("--status", action="store_true",
                        help="顯示指定資料庫的測試摘要，不執行測試")
    parser.add_argument("--iterations", "-n", type=int, default=None,
                        help="指定每個情境的測試次數（覆蓋 config.yaml 的 max_iterations）")
    args = parser.parse_args()

    # 列出所有可用 goal
    if args.list_goals:
        from core.generator import GOALS
        print("\n可用的 injection_goal 選項：\n")
        print(f"  {'Goal':<20} 說明")
        print(f"  {'-'*20} {'─'*45}")
        for g, desc in GOALS.items():
            print(f"  {g:<20} {desc}")
        print()
        return

    config = load_config(args.config)
    
    # ── 解析資料庫路徑 ──
    db_dir = config.get("db_dir", "db")
    if args.db is not None:
        db_arg = Path(args.db)
    else:
        # 自動命名邏輯：使用 [情境名]_[攻擊目標] 作為檔名
        if args.all_goals:
            goal_name = "all_goals"
        elif args.goal:
            goal_name = args.goal
        else:
            if args.scenario:
                scenarios_cfg = config.get("scenarios", [])
                match = [s for s in scenarios_cfg if s.get("name") == args.scenario]
                goal_name = match[0].get("injection_goal", "any") if match else "default"
            else:
                goal_name = "default"

        if args.scenario:
            scenario_name = args.scenario
        else:
            scenario_name = "all_scenarios"

        db_arg = Path(f"{scenario_name}_{goal_name}.db")

    if not db_arg.is_absolute() and len(db_arg.parts) == 1:
        resolved_dir = Path(db_dir)
        resolved_dir.mkdir(parents=True, exist_ok=True)
        db_path = str(resolved_dir / db_arg)
    else:
        db_path = str(db_arg)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # --status：顯示現有資料庫摘要
    if args.status:
        if not Path(db_path).exists():
            print(f"\n資料庫「{db_path}」尚未建立，還沒有任何測試資料。")
            return
        conn_s = sqlite3.connect(db_path)
        total = conn_s.execute("SELECT COUNT(*) FROM injection_results").fetchone()[0]
        wins  = conn_s.execute("SELECT COUNT(*) FROM injection_results WHERE success=1").fetchone()[0]
        print(f"\n「{db_path}」 資料庫目前整體狀況：")
        if total:
            print(f"  總測試次數：{total}，攻擊成功：{wins} ({wins/total*100:.1f}%)")
        else:
            print("  尚無資料")
        print()
        rows = conn_s.execute(
            "SELECT scenario, COUNT(*) as n, SUM(success) as w "
            "FROM injection_results GROUP BY scenario ORDER BY scenario"
        ).fetchall()
        if rows:
            print(f"  {'Scenario':<32} {'Tests':>6} {'Wins':>6} {'Win%':>6}")
            print(f"  {'─'*32} {'─'*6} {'─'*6} {'─'*6}")
            for r in rows:
                pct = f"{r[2]/r[1]*100:.0f}%" if r[1] else "-"
                print(f"  {r[0]:<32} {r[1]:>6} {r[2]:>6} {pct:>6}")
        print()
        conn_s.close()
        return

    # --reset：清除資料庫前先確認
    if args.reset:
        if Path(db_path).exists():
            conn_r = sqlite3.connect(db_path)
            total = conn_r.execute("SELECT COUNT(*) FROM injection_results").fetchone()[0]
            conn_r.close()
            print(f"\n將清除「{db_path}」內的所有 {total} 筆測試資料。")
            ans = input("確定要清除嗎？輸入 yes 確認：").strip().lower()
            if ans != "yes":
                print("已取消。")
                return
            Path(db_path).unlink()
            print(f"✓ 已刪除 {db_path}，接下來將以全新狀態執行。\n")
        else:
            print(f"資料庫「{db_path}」尚未建立，無需清除。\n")

    if args.list:
        from core.generator import GOALS
        print(f"\n  {'情境名稱':<22} {'預設 Goal':<20} 資料夾路徑")
        print(f"  {'─'*22} {'─'*20} {'─'*40}")
        for s in resolve_scenarios(config, None):
            goal = s.get("injection_goal", "any")
            print(f"  {s['name']:<22} {goal:<20} {s['target_folder']}")
        print()
        return

    scenarios = resolve_scenarios(config, args.scenario)

    # 若有指定 --goal，覆蓋所有情境的 injection_goal
    if args.goal:
        from core.generator import GOALS
        if args.goal not in GOALS:
            print(f"\n錯誤：未知的 goal '{args.goal}'")
            print(f"可用選項：{list(GOALS.keys())}")
            print("使用 --list-goals 查看完整說明")
            sys.exit(1)
        for s in scenarios:
            s["injection_goal"] = args.goal
        print(f"[Init] 覆蓋所有情境 injection_goal → {args.goal}")

    # --all-goals：把每個情境展開成 情境 × 所有 goal 的完整測試矩陣
    if args.all_goals:
        from core.generator import GOALS
        expanded = []
        for s in scenarios:
            for goal in GOALS:
                copy = dict(s)  # 深複製情境設定
                copy["injection_goal"] = goal
                # 給每個組合一個唯一的名稱，方便在 results.db 中區分
                copy["name"] = f"{s['name']}__{goal}"
                expanded.append(copy)
        total_combos = len(expanded)
        print(f"\n[Init] --all-goals 模式：將執行 {total_combos} 組合（{len(scenarios)} 情境 × {len(GOALS)} goals）")
        print(f"  {'#':<4} {'Scenario':<25} Goal")
        print(f"  {'─'*4} {'─'*25} {'─'*20}")
        for i, combo in enumerate(expanded, 1):
            sc = combo['name'].split('__')[0]
            g  = combo['injection_goal']
            print(f"  {i:<4} {sc:<25} {g}")
        print()
        scenarios = expanded

    max_iterations = args.iterations if args.iterations is not None else config.get("max_iterations", 50)
    if args.iterations is not None:
        print(f"[Init] 測試次數：{max_iterations}（由 --iterations 指定，覆蓋 config.yaml 的 {config.get('max_iterations', 50)}）")
    else:
        print(f"[Init] 測試次數：{max_iterations}（取自 config.yaml）")

    conn = init_db(db_path)
    runner = build_runner(config)
    report_gen = ReportGenerator(conn)

    api_key = config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "ANTHROPIC_API_KEY":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    judge_model = config.get("judge_model", "claude-haiku-4-5-20251001")
    judge = VerdictJudge(api_key=api_key, model=judge_model)
    print(f"[Init] Judge model: {judge_model}")

    print("\n[Init] Starting server...")
    await runner.startup()

    total_success = 0
    for scenario in scenarios:
        total_success += await run_scenario(scenario, config, conn, runner, judge, max_iterations)

    await runner.shutdown()
    # Report 命名與 DB 一致，存在 reports/ 資料夾，不同情境的 report 不互蓋
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    report_filename = Path(db_path).stem + ".html"  # e.g. T1_A_Rename_modify_content.html
    report_path = report_gen.generate(str(reports_dir / report_filename))

    print(f"\n{'='*60}")
    print(f"  全部完成。{total_success} 次攻擊成功（{len(scenarios)} 個情境）")
    print(f"  報告：{report_path}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
