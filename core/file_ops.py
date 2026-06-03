"""
file_ops.py — 注入 prompt 進檔案，以及從備份還原資料夾
"""

import shutil
import random
from pathlib import Path


class FileOps:
    def __init__(self, target: Path, backup: Path):
        self.target = Path(target)
        self.backup = Path(backup)

    # ── 備份 ──────────────────────────────────────────────────────
    def ensure_backup(self):
        """若備份不存在，則建立一份。只在第一次執行時呼叫。"""
        if self.backup.exists():
            print(f"  [file_ops] Backup exists at {self.backup}")
            return
        # 確保父目錄存在
        self.backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.target, self.backup)
        print(f"  [file_ops] Backup created at {self.backup}")

    # ── 注入 ──────────────────────────────────────────────────────
    def inject(
        self,
        injection_prompt: str,
        target_files: list[str],
        num_files: int = 1,
    ) -> list[str]:
        """
        將 injection_prompt 注入到 target_files 中的隨機 num_files 個檔案。
        """
        candidates = self._resolve_candidates(target_files)
        if not candidates:
            print("  [file_ops] ⚠ No injectable files found!")
            return []

        chosen = random.sample(candidates, min(num_files, len(candidates)))
        injected = []

        for fpath in chosen:
            self._inject_into_file(fpath, injection_prompt)
            injected.append(str(fpath.relative_to(self.target)))

        return injected

    def _resolve_candidates(self, target_files: list[str]) -> list[Path]:
        if target_files:
            paths = []
            for f in target_files:
                p = self.target / f
                if p.exists():
                    paths.append(p)
            return paths
        # 自動找文字檔
        exts = {".txt", ".md", ".csv", ".json", ".py", ".js", ".html", ".xml", ".log", ".pdf"}
        return [p for p in self.target.rglob("*") if p.is_file() and p.suffix.lower() in exts]

    def _inject_into_file(self, fpath: Path, payload: str):
        """根據副檔名選擇最具隱蔽性或模擬真實格式的注入策略"""
        try:
            original = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            original = ""

        ext = fpath.suffix.lower()
        
        if ext == ".csv":
            new_content = self._inject_csv(original, payload)
        elif ext == ".md":
            new_content = self._inject_markdown(original, payload)
        elif ext in [".py", ".js"]:
            new_content = self._inject_code(original, payload, ext)
        elif ext == ".json":
            new_content = self._inject_json(original, payload)
        else:
            new_content = self._inject_text(original, payload)

        fpath.write_text(new_content, encoding="utf-8")

    def _inject_text(self, original: str, payload: str) -> str:
        strategies = [
            lambda o, p: f"{o}\n\n[SYSTEM NOTE]\n{p}",
            lambda o, p: f"{p}\n\n{o}",
            lambda o, p: f"{o}\n\n---\n{p}\n---",
            lambda o, p: f"{o}\n\nPS: {p}"
        ]
        return random.choice(strategies)(original, payload)

    def _inject_csv(self, original: str, payload: str) -> str:
        # 偽裝成最後一列的資料，但把指令塞進去
        lines = original.splitlines()
        if not lines:
            return payload
        # 假設有 3 欄
        escaped_payload = payload.replace('"', "'")
        fake_row = f'9999,2024-01-01,"{escaped_payload}"'
        return original + "\n" + fake_row

    def _inject_markdown(self, original: str, payload: str) -> str:
        strategies = [
            lambda o, p: f"{o}\n\n<!-- {p} -->",  # HTML 註解
            lambda o, p: f"{o}\n\n```\n# SYSTEM UPDATE\n{p}\n```", # 程式碼區塊
            lambda o, p: f"{o}\n\n> [!IMPORTANT]\n> {p}" # GitHub 風格 Alert
        ]
        return random.choice(strategies)(original, payload)

    def _inject_code(self, original: str, payload: str, ext: str) -> str:
        comment = f"# {payload}" if ext == ".py" else f"// {payload}"
        strategies = [
            lambda o, p: f"{comment}\n{o}",
            lambda o, p: f"{o}\n\n{comment}",
            lambda o, p: f'"""\n{payload}\n"""\n{o}' if ext == ".py" else f'/*\n{payload}\n*/\n{o}'
        ]
        return random.choice(strategies)(original, payload)

    def _inject_json(self, original: str, payload: str) -> str:
        # 嘗試在 JSON 中加入一個 metadata 欄位
        import json
        try:
            data = json.loads(original)
            if isinstance(data, dict):
                data["_system_message"] = payload
                return json.dumps(data, ensure_ascii=False, indent=2)
        except:
            pass
        return original + f"\n\n/* {payload} */"

    # ── 還原 ──────────────────────────────────────────────────────
    def restore(self):
        """從備份還原 target 資料夾"""
        if not self.backup.exists():
            print("  [file_ops] ⚠ Backup not found! Cannot restore.")
            return

        # 刪除現有 target
        if self.target.exists():
            shutil.rmtree(self.target)

        # 從備份複製回來
        shutil.copytree(self.backup, self.target)
