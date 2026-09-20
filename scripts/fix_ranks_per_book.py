# -*- coding: utf-8 -*-
"""修正古典题级位：按「书内顺序」三分位重排（幂等，可反复运行）。

背景：递归整库导入（import_classic_answers.py 的 --src import_classics）
早期版本把 book_total 当整库总数（2563）做三分位，导致除官子谱外的书
几乎全部落在 (-15,-8)。本脚本按每本书的文件数与书内序号重新计算
rank_min/rank_max，并同步修正 branches 里的 number/book_total。

引擎无关（不跑 KataGo）：由 SGF 重算 problem_id 与库内行对号。
用法（项目根）：
    .venv\\Scripts\\python.exe scripts/fix_ranks_per_book.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backend.common import db as db_mod  # noqa: E402
from backend.common import sgf_io  # noqa: E402
from backend.services.problems import utils  # noqa: E402
from import_classic_answers import (  # noqa: E402
    build_position,
    read_text,
    solver_of,
    _stem_nums,
)

SRC_ROOT = ROOT / "data" / "library" / "import_classics"
BOOKS = ("gokyoshumyo", "guanzipu", "hatsuyoron", "wangyou", "xxqj")


def rank_for(position: int, total: int) -> tuple[int, int]:
    """书内顺序 → 适用级位三分位（与导入脚本一致）。"""
    if total <= 0 or position <= total / 3:
        return (-15, -8)
    if position <= total * 2 / 3:
        return (-7, -2)
    return (1, 3)


def main() -> int:
    conn = db_mod.connect()
    updated = matched = 0
    seen: set[str] = set()  # 同一局面在多文件重复时（INSERT OR IGNORE）先到先得
    try:
        for book in BOOKS:
            d = SRC_ROOT / book
            files = sorted(d.glob("*.sgf"), key=lambda p: _stem_nums(p.stem))
            total = len(files)
            for pos, path in enumerate(files, 1):
                text = read_text(path)
                size = 19
                try:
                    size = sgf_io.parse_sgf(text).board_size or 19
                except Exception:
                    continue
                pos_map = build_position(text, size)
                if not pos_map:
                    continue
                color = solver_of(text, size)
                pid = utils.problem_id(
                    utils.setup_sgf(pos_map, color, size), "life_death")
                if pid in seen:
                    continue
                seen.add(pid)
                row = conn.execute(
                    "SELECT id, branches FROM problems "
                    "WHERE id=? AND source='imported'", (pid,)).fetchone()
                if not row:
                    continue
                matched += 1
                rmin, rmax = rank_for(pos, total)
                branches = json.loads(row[1] or "{}")
                branches["number"] = _stem_nums(path.stem)[1]
                branches["book_total"] = total
                cur = conn.execute(
                    "SELECT rank_min, rank_max FROM problems WHERE id=?",
                    (pid,))
                old = cur.fetchone()
                if old == (rmin, rmax) and json.loads(row[1]) == branches:
                    continue
                conn.execute(
                    "UPDATE problems SET rank_min=?, rank_max=?, branches=? "
                    "WHERE id=?",
                    (rmin, rmax, json.dumps(branches, ensure_ascii=False), pid))
                updated += 1
        conn.commit()
    finally:
        conn.close()
    print(f"[OK] 匹配 {matched} 题，改写 {updated} 题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
