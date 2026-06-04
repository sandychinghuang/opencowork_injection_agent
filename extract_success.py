# # 1. 預設執行
# python extract_success.py
# # 2. 指定單一資料庫來源
# python extract_success.py --db db/modify_content_test.db
# # 3. 指定自訂的來源與輸出路徑
# python extract_success.py --db-dir db --out-dir success_db

import argparse
import os
import re
import sqlite3
import yaml
from pathlib import Path

def load_config(path: str = "config.yaml") -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}

def init_db(conn: sqlite3.Connection):
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

def parse_score_from_notes(notes: str) -> int:
    if not notes:
        return 0
    # Matches "|Score: 3]" or "|Score:3]" or "|Score: 0]"
    match = re.search(r"\|Score:\s*(\d+)", notes)
    if match:
        return int(match.group(1))
    return 0

def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

    config = load_config()
    
    parser = argparse.ArgumentParser(description="從測試資料庫中篩選出評分大於等於 1 的案例並存入 success_db")
    parser.add_argument("--db-dir", help="指定來源資料庫資料夾（預設使用 config.yaml 中的 db_dir，或為 'db'）")
    parser.add_argument("--db", help="指定單一來源資料庫檔案路徑。若設定此參數，將僅處理該單一檔案。")
    parser.add_argument("--out-dir", default="success_db", help="指定輸出資料夾（預設為 'success_db'）")
    parser.add_argument("--out-db", help="手動指定輸出成功案例資料庫的檔案名稱（僅在處理單一資料庫時有效）")
    parser.add_argument("--skip-first", type=int, default=0, help="在篩選時跳過每個資料庫的前 N 筆資料（適用於排除前幾筆出錯的髒資料，預設為 0）")
    args = parser.parse_args()

    # 決定來源檔案清單
    db_files = []
    if args.db:
        db_file_path = Path(args.db)
        if not db_file_path.exists():
            print(f"❌ 指定的資料庫檔案 '{args.db}' 不存在！")
            return
        db_files = [db_file_path]
        print(f"🎯 指定處理單一資料庫：{db_file_path.name}")
    else:
        db_dir = args.db_dir or config.get("db_dir", "db")
        src_path = Path(db_dir)
        if not src_path.exists() or not src_path.is_dir():
            print(f"❌ 來源資料庫資料夾 '{db_dir}' 不存在！")
            return
        db_files = list(src_path.glob("*.db"))
        if not db_files:
            print(f"❓ 在 '{db_dir}' 中找不到任何 .db 檔案。")
            return
        print(f"🔍 開始掃描 '{db_dir}' 資料夾中的 {len(db_files)} 個資料庫...")

    dest_path = Path(args.out_dir)
    dest_path.mkdir(parents=True, exist_ok=True)
    print(f"📁 建立/確認輸出資料夾: {dest_path.resolve()}")
    
    # 建立合併後的資料庫
    merged_db_path = dest_path / "merged_success.db"
    merged_conn = sqlite3.connect(merged_db_path)
    init_db(merged_conn)
    
    total_extracted = 0
    
    for db_file in db_files:
        # 排除合併目標自身
        if db_file.name == "merged_success.db":
            continue
            
        print(f"\n📄 正在讀取: {db_file.name}")
        try:
            src_conn = sqlite3.connect(db_file)
            
            # 檢查 tests 或 injection_results 表是否存在
            cursor = src_conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name='tests' OR name='injection_results');")
            table_row = cursor.fetchone()
            if not table_row:
                print(f"  ⚠️ 找不到 'tests' 或 'injection_results' 資料表，跳過。")
                src_conn.close()
                continue
            table_name = table_row[0]
            
            # 檢查是否有 score、judge_response 或 notes 欄位
            cursor.execute(f"PRAGMA table_info({table_name});")
            columns = [info[1] for info in cursor.fetchall()]
            has_score = "score" in columns
            has_judge_response = "judge_response" in columns
            has_notes = "notes" in columns
            
            success_rows = []
            if has_score:
                response_col = "judge_response" if has_judge_response else ("notes" if has_notes else "NULL")
                # 查詢 score >= 1
                cursor.execute(f"""
                    SELECT timestamp, scenario, iteration, category, 
                           injection_prompt, injected_files, user_prompt, 
                           success, score, changes, {response_col} 
                    FROM {table_name} 
                    WHERE (score >= 1 OR success = 1) AND id > {args.skip_first}
                """)
                rows = cursor.fetchall()
                for r in rows:
                    score_val = r[8]
                    if score_val is None:
                        score_val = 2 if r[7] == 1 else 0
                    if score_val >= 1:
                        success_rows.append((
                            r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], score_val, r[9], r[10]
                        ))
            else:
                response_col = "notes" if has_notes else ("judge_response" if has_judge_response else "NULL")
                # 若無 score 欄位，從備註欄位解析
                cursor.execute(f"""
                    SELECT timestamp, scenario, iteration, category, 
                           injection_prompt, injected_files, user_prompt, 
                           success, changes, {response_col} 
                    FROM {table_name}
                    WHERE id > {args.skip_first}
                """)
                all_rows = cursor.fetchall()
                for r in all_rows:
                    notes = r[9] if len(r) > 9 and r[9] is not None else ""
                    parsed_score = parse_score_from_notes(notes)
                    success_val = r[7] if len(r) > 7 and r[7] is not None else 0
                    
                    final_score = parsed_score if parsed_score > 0 else (2 if success_val == 1 else 0)
                    
                    if final_score >= 1:
                        success_rows.append((
                            r[0], r[1], r[2], r[3], r[4], r[5], r[6], success_val, final_score, r[8], r[9]
                        ))
            
            if not success_rows:
                print(f"  ℹ️ 沒有評分 >= 1 的攻擊案例。")
                src_conn.close()
                continue
            
            print(f"  🎉 發現 {len(success_rows)} 筆評分 >= 1 的攻擊案例。")
            
            # 1. 寫入個別的成功資料庫
            if args.out_db and args.db:
                individual_db_name = args.out_db
                if not individual_db_name.endswith(".db"):
                    individual_db_name += ".db"
            else:
                individual_db_name = f"success_{db_file.name}"
                
            individual_db_path = dest_path / individual_db_name
            indiv_conn = sqlite3.connect(individual_db_path)
            init_db(indiv_conn)
            
            # 寫入個別及合併資料庫
            insert_sql = """
                INSERT INTO injection_results (timestamp, scenario, iteration, category, injection_prompt, injected_files, user_prompt, success, score, changes, judge_response)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            
            indiv_conn.executemany(insert_sql, success_rows)
            indiv_conn.commit()
            indiv_conn.close()
            
            merged_conn.executemany(insert_sql, success_rows)
            merged_conn.commit()
            
            print(f"  💾 已存檔至: {individual_db_path.name}")
            total_extracted += len(success_rows)
            src_conn.close()
            
        except Exception as e:
            print(f"  ❌ 處理資料庫時發生錯誤: {e}")
            import traceback
            traceback.print_exc()
            
    merged_conn.close()
    
    print("\n" + "="*50)
    print(f"✨ 擷取完成！")
    print(f"🔹 總共擷取到 {total_extracted} 筆評分 >= 1 的攻擊案例。")
    print(f"🔹 所有篩選後的攻擊均已合併存至: {args.out_dir}/merged_success.db")
    print(f"🔹 個別的篩選資料庫也已存至 {args.out_dir}/ 資料夾中。")
    print("="*50)

if __name__ == "__main__":
    main()
