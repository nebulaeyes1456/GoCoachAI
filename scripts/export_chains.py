# -*- coding: utf-8 -*-
"""导出题链与链上题（v1.7.6）。

题链与链上题存在 SQLite（`data/goapp.db`）里，而 `data/` 不入 git——别人克隆
下来只有种子 SGF，要自己跑引擎生长才有题。本脚本把现有链与题**导成一份可入库
的 JSON**，让下载者一条命令就能拿到同样的一批题（见 `import_chains.py`）。

用法（项目根）：
    .\\.venv\\Scripts\\python.exe scripts\\export_chains.py
    # 产出 data/chains/chains_export.json（幂等，可反复覆盖）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common import db as db_mod  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "chains" / "chains_export.json"
CHAIN_COLS = ("id", "name", "theme", "root_sgf", "seed_sgf", "description",
              "status", "created_at")
PROBLEM_COLS = ("id", "source", "review_id", "theme", "rank_min", "rank_max",
                "setup_sgf", "answer", "branches", "verdict", "hint",
                "explanation", "status", "goal", "chain_id", "chain_step",
                "created_at")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--db", default=None, help="源库（默认 data/goapp.db）")
    args = ap.parse_args()

    conn = db_mod.connect(args.db)
    try:
        chains = [dict(r) for r in conn.execute(
            "SELECT * FROM problem_chains ORDER BY created_at, id")]
        problems = [dict(r) for r in conn.execute(
            "SELECT * FROM problems WHERE chain_id IS NOT NULL"
            " ORDER BY chain_id, COALESCE(chain_step, 9999), id")]
    finally:
        conn.close()

    payload = {
        "_doc": "YiYou 题链导出：import_chains.py 可幂等导入（INSERT OR IGNORE）",
        "version": 1,
        "chains": [{k: c.get(k) for k in CHAIN_COLS} for c in chains],
        "problems": [{k: p.get(k) for k in PROBLEM_COLS} for p in problems],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8", newline="\n")
    size_kb = out.stat().st_size / 1024
    print(f"[OK] 导出 {len(chains)} 条链、{len(problems)} 道链上题 → {out}"
          f"（{size_kb:.0f} KB）")


if __name__ == "__main__":
    main()
