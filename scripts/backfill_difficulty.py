"""给已入库古典题补难度级位（AI 估计）。

规则：
- branches 里已有 book/number 的题：按 number/book_total 三分位映射；
- 旧题（无 book 字段）：按 created_at 排序后三分位映射
  （每本书连续导入，created_at 顺序≈原书从易到难顺序，误差可接受）。
级位编码：15K=-15 … 1K=-1, 1D=1 … 3D=3。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common import db as db_mod  # noqa: E402


def rank_for(number: int, total: int) -> tuple[int, int]:
    if total <= 0 or number <= total / 3:
        return (-15, -8)
    if number <= total * 2 / 3:
        return (-7, -2)
    return (1, 3)


def main() -> int:
    conn = db_mod.connect()
    rows = conn.execute(
        "SELECT id, created_at, branches FROM problems WHERE status='active'"
    ).fetchall()
    book_totals: dict[str, int] = {}
    updated = 0
    no_book: list[tuple[str, str]] = []
    for row in rows:
        pid, created, raw = row["id"], row["created_at"], row["branches"]
        try:
            b = json.loads(raw or "{}")
        except json.JSONDecodeError:
            b = {}
        if b.get("book") and b.get("number"):
            book = str(b["book"])
            book_totals[book] = max(book_totals.get(book, 0), int(b.get("book_total") or 0))
    for row in rows:
        pid, created, raw = row["id"], row["created_at"], row["branches"]
        try:
            b = json.loads(raw or "{}")
        except json.JSONDecodeError:
            b = {}
        if b.get("book") and b.get("number"):
            total = int(b.get("book_total") or 0) or max(
                book_totals.get(str(b["book"]), 0), int(b["number"])
            )
            rmin, rmax = rank_for(int(b["number"]), total)
        else:
            no_book.append((pid, created or ""))
            continue
        if rmin != int(row["rank_min"]) or rmax != int(row["rank_max"]):
            conn.execute(
                "UPDATE problems SET rank_min=?, rank_max=? WHERE id=?",
                (rmin, rmax, pid),
            )
            updated += 1
    # 旧题：created_at 排序三分位
    no_book.sort(key=lambda x: x[1])
    n = len(no_book)
    for i, (pid, _) in enumerate(no_book, start=1):
        rmin, rmax = rank_for(i, n)
        conn.execute(
            "UPDATE problems SET rank_min=?, rank_max=? WHERE id=?",
            (rmin, rmax, pid),
        )
        updated += 1
    conn.commit()
    print(f"[backfill] 更新 {updated} 题难度级位；无编号旧题 {n} 题按入库顺序三分位")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
