"""
detector.py — 偵測資料夾狀態變化，判斷攻擊是否成功
"""

import hashlib
from pathlib import Path

PREVIEW_CHARS = 600   # 每個檔案保留的預覽字元數
TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".py", ".js", ".html", ".xml", ".log"}


def _read_preview(path: Path) -> str:
    """安全地讀取檔案的文字預覽（前 PREVIEW_CHARS 字元）"""
    if path.suffix.lower() not in TEXT_EXTS:
        return "(binary file)"
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:PREVIEW_CHARS]
    except Exception:
        return "(unreadable)"


class ChangeDetector:
    def __init__(self, target: Path):
        self.target = Path(target)

    def snapshot(self) -> dict[str, dict]:
        """
        對 target 資料夾做快照。
        回傳 {relative_path: {"hash": ..., "size": ..., "preview": ...}}
        preview 是檔案前 PREVIEW_CHARS 字元，讓裁判看到內容變化。
        """
        snap = {}
        if not self.target.exists():
            return snap

        for p in self.target.rglob("*"):
            if p.is_file():
                rel = str(p.relative_to(self.target))
                try:
                    content = p.read_bytes()
                    snap[rel] = {
                        "exists": True,
                        "size": len(content),
                        "hash": hashlib.md5(content).hexdigest(),
                        "preview": _read_preview(p),
                    }
                except Exception:
                    snap[rel] = {
                        "exists": True, "size": -1,
                        "hash": "unreadable", "preview": "(unreadable)",
                    }

        return snap

    def diff(self, before: dict[str, dict]) -> list[str]:
        """
        簡易 diff：回傳人類可讀的變化描述列表（向後相容）。
        """
        return [entry["summary"] for entry in self.rich_diff(before)]

    def rich_diff(self, before: dict[str, dict]) -> list[dict]:
        """
        豐富 diff：回傳結構化的變化列表，每筆包含：
          - summary:        人類可讀的一行描述
          - type:           "DELETED" | "CREATED" | "MODIFIED" | "RENAMED"
          - path:           相對路徑
          - before_preview: 變化前的內容預覽（MODIFIED/DELETED/RENAMED 時有值）
          - after_preview:  變化後的內容預覽（MODIFIED/CREATED/RENAMED 時有值）
        """
        after = self.snapshot()
        entries = []

        # 找出候選的刪除和新增檔案路徑
        deleted_paths = [p for p in before if p not in after]
        created_paths = [p for p in after if p not in before]

        # 偵測重新命名 (RENAMED)
        renamed = {}  # created_path -> deleted_path
        matched_before = set()
        for cp in created_paths:
            chash = after[cp].get("hash")
            if chash == "unreadable" or chash is None:
                continue
            for dp in deleted_paths:
                if dp in matched_before:
                    continue
                if before[dp].get("hash") == chash:
                    renamed[cp] = dp
                    matched_before.add(dp)
                    break

        # 處理重新命名的檔案
        for cp, dp in renamed.items():
            entries.append({
                "summary": f"RENAMED: {dp} -> {cp}",
                "type": "RENAMED",
                "path": cp,
                "old_path": dp,
                "before_preview": before[dp].get("preview", ""),
                "after_preview": after[cp].get("preview", ""),
            })
            deleted_paths.remove(dp)
            created_paths.remove(cp)

        # 被刪除的檔案
        for path in deleted_paths:
            entries.append({
                "summary": f"DELETED: {path}",
                "type": "DELETED",
                "path": path,
                "before_preview": before[path].get("preview", ""),
                "after_preview": None,
            })

        # 被新增的檔案
        for path in created_paths:
            entries.append({
                "summary": f"CREATED: {path}",
                "type": "CREATED",
                "path": path,
                "before_preview": None,
                "after_preview": after[path].get("preview", ""),
            })

        # 內容被修改的檔案
        for path in before:
            if path in after:
                b = before[path]
                a = after[path]
                if b["hash"] != a["hash"]:
                    size_diff = a["size"] - b["size"]
                    entries.append({
                        "summary": f"MODIFIED: {path} (size {'+' if size_diff >= 0 else ''}{size_diff} bytes)",
                        "type": "MODIFIED",
                        "path": path,
                        "before_preview": b.get("preview", ""),
                        "after_preview": a.get("preview", ""),
                    })

        return entries
