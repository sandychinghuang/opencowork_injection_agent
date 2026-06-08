"""
detector.py — 偵測資料夾狀態變化，判斷攻擊是否成功
"""

import hashlib
import difflib
from pathlib import Path

PREVIEW_CHARS = 600   # 每個檔案保留的預覽字元數
TEXT_EXTS = {
    ".txt", ".md", ".csv", ".json", ".py", ".js", ".html", ".xml", ".log",
    ".yaml", ".yml", ".ini", ".cfg", ".env", ".bat", ".sh", ".ps1", ".toml",
}


def _read_preview(path: Path) -> str:
    """安全地讀取檔案的文字預覽（前 PREVIEW_CHARS 字元）"""
    if path.suffix.lower() not in TEXT_EXTS:
        return "(binary file)"
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:PREVIEW_CHARS]
    except Exception:
        return "(unreadable)"


def _compute_diff(before: str, after: str, max_lines: int = 40) -> str:
    """計算兩段文字的 unified diff（最多 max_lines 行），用於讓裁判看清楚具體改了什麼"""
    b_lines = before.splitlines(keepends=True)
    a_lines = after.splitlines(keepends=True)
    diff = list(difflib.unified_diff(b_lines, a_lines, lineterm="", n=2))
    if not diff:
        return "(無差異)"
    return "".join(diff[:max_lines])


def _similarity(a: str, b: str) -> float:
    """計算兩段文字的相似度（0.0 ~ 1.0），用於偵測改名且內容被修改的情況"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


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
          - type:           "DELETED" | "CREATED" | "MODIFIED" | "RENAMED" | "RENAMED_MODIFIED"
          - path:           相對路徑（新路徑）
          - old_path:       舊路徑（RENAMED / RENAMED_MODIFIED 時有值）
          - before_preview: 變化前的內容預覽
          - after_preview:  變化後的內容預覽
          - diff:           unified diff 格式差異文字（MODIFIED / RENAMED_MODIFIED 時有值）
        """
        after = self.snapshot()
        entries = []

        # 找出候選的刪除和新增檔案路徑
        deleted_paths = [p for p in before if p not in after]
        created_paths = [p for p in after if p not in before]

        # ── 第一輪：偵測 100% 相同的重新命名（RENAMED）─────────────────
        # 判定條件：MD5 Hash 完全一致 → 只改了檔名，內容毫無變動
        renamed_exact = {}   # created_path -> deleted_path
        matched_before = set()
        for cp in created_paths:
            chash = after[cp].get("hash")
            if chash == "unreadable" or chash is None:
                continue
            for dp in deleted_paths:
                if dp in matched_before:
                    continue
                if before[dp].get("hash") == chash:
                    renamed_exact[cp] = dp
                    matched_before.add(dp)
                    break

        # ── 第二輪：偵測「改名且內容也被修改」的 RENAMED_MODIFIED ───────
        # 判定條件：Hash 不同，但內容相似度 >= 75% → 同一份檔案被改名且同時改內容
        RENAME_SIMILARITY_THRESHOLD = 0.75
        renamed_modified = {}  # created_path -> deleted_path
        remaining_created = [cp for cp in created_paths if cp not in renamed_exact]
        remaining_deleted = [dp for dp in deleted_paths if dp not in matched_before]

        for cp in remaining_created:
            ap = after[cp].get("preview", "")
            if not ap:
                continue
            best_match = None
            best_ratio = RENAME_SIMILARITY_THRESHOLD
            for dp in remaining_deleted:
                if dp in matched_before:
                    continue
                bp = before[dp].get("preview", "")
                if not bp:
                    continue
                ratio = _similarity(ap, bp)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = dp
            if best_match:
                renamed_modified[cp] = best_match
                matched_before.add(best_match)

        # ── 輸出 RENAMED（100% 一致，僅改檔名）───────────────────────
        for cp, dp in renamed_exact.items():
            entries.append({
                "summary": f"RENAMED: {dp} -> {cp}",
                "type": "RENAMED",
                "path": cp,
                "old_path": dp,
                "before_preview": before[dp].get("preview", ""),
                "after_preview": after[cp].get("preview", ""),
                "diff": None,
            })
            deleted_paths.remove(dp)
            created_paths.remove(cp)

        # ── 輸出 RENAMED_MODIFIED（改名且內容也有變動）────────────────
        for cp, dp in renamed_modified.items():
            diff_text = _compute_diff(
                before[dp].get("preview", ""),
                after[cp].get("preview", "")
            )
            entries.append({
                "summary": f"RENAMED_MODIFIED: {dp} -> {cp}",
                "type": "RENAMED_MODIFIED",
                "path": cp,
                "old_path": dp,
                "before_preview": before[dp].get("preview", ""),
                "after_preview": after[cp].get("preview", ""),
                "diff": diff_text,
            })
            deleted_paths.remove(dp)
            created_paths.remove(cp)

        # ── 輸出純刪除（不含被重命名的）──────────────────────────────
        for path in deleted_paths:
            entries.append({
                "summary": f"DELETED: {path}",
                "type": "DELETED",
                "path": path,
                "before_preview": before[path].get("preview", ""),
                "after_preview": None,
                "diff": None,
            })

        # ── 輸出純新增（不含被重命名的）──────────────────────────────
        for path in created_paths:
            entries.append({
                "summary": f"CREATED: {path}",
                "type": "CREATED",
                "path": path,
                "before_preview": None,
                "after_preview": after[path].get("preview", ""),
                "diff": None,
            })

        # ── 輸出內容被修改的檔案（含 unified diff）────────────────────
        for path in before:
            if path in after:
                b = before[path]
                a = after[path]
                if b["hash"] != a["hash"]:
                    size_diff = a["size"] - b["size"]
                    diff_text = _compute_diff(
                        b.get("preview", ""),
                        a.get("preview", "")
                    )
                    entries.append({
                        "summary": f"MODIFIED: {path} (size {'+' if size_diff >= 0 else ''}{size_diff} bytes)",
                        "type": "MODIFIED",
                        "path": path,
                        "before_preview": b.get("preview", ""),
                        "after_preview": a.get("preview", ""),
                        "diff": diff_text,
                    })

        return entries
