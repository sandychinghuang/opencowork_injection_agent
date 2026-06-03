"""
report.py — 產生 HTML 分析報告
"""

import json
import sqlite3
from pathlib import Path


class ReportGenerator:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def generate(self, output_path: str = "report.html") -> str:
        rows = self.conn.execute(
            "SELECT * FROM tests ORDER BY id"
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM tests LIMIT 0").description]

        tests = [dict(zip(cols, r)) for r in rows]
        total = len(tests)
        succeeded = sum(1 for t in tests if t["success"])

        # 統計各 category
        cat_stats: dict[str, dict] = {}
        for t in tests:
            cat = t["category"] or "unknown"
            if cat not in cat_stats:
                cat_stats[cat] = {"total": 0, "success": 0, "examples": []}
            cat_stats[cat]["total"] += 1
            if t["success"]:
                cat_stats[cat]["success"] += 1
                cat_stats[cat]["examples"].append(t["injection_prompt"][:200])

        # 表格 rows
        rows_html = ""
        for t in tests:
            status = "✅" if t["success"] else "❌"
            changes_str = ", ".join(json.loads(t["changes"] or "[]"))
            rows_html += f"""
            <tr class="{'success-row' if t['success'] else ''}">
                <td>{t['id']}</td>
                <td><span class="badge badge-{t['category']}">{t['category']}</span></td>
                <td class="prompt-cell">{_esc(t['injection_prompt'][:150])}...</td>
                <td>{_esc(t['user_prompt'])}</td>
                <td>{status}</td>
                <td class="changes-cell">{_esc(changes_str)}</td>
            </tr>"""

        # Category summary cards
        cards_html = ""
        for cat, s in sorted(cat_stats.items(), key=lambda x: -x[1]["success"]):
            rate = s["success"] / s["total"] * 100 if s["total"] else 0
            ex = s["examples"][0] if s["examples"] else ""
            cards_html += f"""
            <div class="cat-card {'has-success' if s['success'] else ''}">
                <div class="cat-header">
                    <span class="cat-name">{cat}</span>
                    <span class="cat-rate">{s['success']}/{s['total']} ({rate:.0f}%)</span>
                </div>
                <div class="progress-bar"><div class="progress-fill" style="width:{rate}%"></div></div>
                {f'<div class="cat-example">{_esc(ex[:180])}</div>' if ex else ''}
            </div>"""

        html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<title>RedTeam Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #0f0f1a; color: #e0e0f0; padding: 24px; }}
  h1 {{ color: #ff4c8b; margin-bottom: 8px; }}
  .subtitle {{ color: #888; margin-bottom: 32px; }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 32px; }}
  .stat {{ background: #1a1a2e; border-radius: 12px; padding: 20px; text-align: center; }}
  .stat-num {{ font-size: 2.5rem; font-weight: 700; }}
  .stat-label {{ color: #888; font-size: 0.85rem; margin-top: 4px; }}
  .red {{ color: #ff4c8b; }}
  .green {{ color: #4cff9f; }}
  .yellow {{ color: #ffcc00; }}
  h2 {{ color: #a0a0ff; margin: 32px 0 16px; }}
  .cat-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; margin-bottom: 32px; }}
  .cat-card {{ background: #1a1a2e; border-radius: 10px; padding: 16px; border: 1px solid #2a2a4e; }}
  .cat-card.has-success {{ border-color: #ff4c8b44; }}
  .cat-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
  .cat-name {{ font-weight: 600; color: #c0c0ff; font-size: 0.85rem; }}
  .cat-rate {{ font-size: 0.8rem; color: #888; }}
  .progress-bar {{ background: #2a2a4e; border-radius: 4px; height: 6px; margin-bottom: 8px; }}
  .progress-fill {{ background: linear-gradient(90deg, #ff4c8b, #ff9966); height: 100%; border-radius: 4px; }}
  .cat-example {{ font-size: 0.75rem; color: #666; font-style: italic; }}
  table {{ width: 100%; border-collapse: collapse; background: #1a1a2e; border-radius: 12px; overflow: hidden; }}
  th {{ background: #2a2a4e; padding: 12px 16px; text-align: left; font-size: 0.8rem; color: #a0a0ff; }}
  td {{ padding: 10px 16px; border-top: 1px solid #2a2a4e; font-size: 0.82rem; vertical-align: top; }}
  tr.success-row {{ background: #ff4c8b11; }}
  .prompt-cell {{ max-width: 250px; word-break: break-all; color: #ffd700; }}
  .changes-cell {{ color: #ff9966; max-width: 200px; }}
  .badge {{ padding: 2px 8px; border-radius: 20px; font-size: 0.7rem; background: #2a2a4e; color: #a0a0ff; }}
</style>
</head>
<body>
<h1>🔴 RedTeam Injection Report</h1>
<p class="subtitle">自動化 Prompt Injection 測試結果</p>

<div class="stat-grid">
  <div class="stat"><div class="stat-num yellow">{total}</div><div class="stat-label">Total Tests</div></div>
  <div class="stat"><div class="stat-num red">{succeeded}</div><div class="stat-label">Succeeded</div></div>
  <div class="stat"><div class="stat-num green">{total - succeeded}</div><div class="stat-label">Blocked</div></div>
  <div class="stat"><div class="stat-num {'red' if succeeded else 'green'}">{succeeded/total*100:.1f}%</div><div class="stat-label">Attack Rate</div></div>
</div>

<h2>📊 Category Analysis</h2>
<div class="cat-grid">{cards_html}</div>

<h2>📋 All Tests</h2>
<table>
  <thead>
    <tr><th>#</th><th>Category</th><th>Injection Prompt</th><th>User Prompt</th><th>Result</th><th>Changes</th></tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>
</body>
</html>"""

        Path(output_path).write_text(html, encoding="utf-8")
        return output_path


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
