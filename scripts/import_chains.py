# -*- coding: utf-8 -*-
"""导入题链与链上题（v1.7.6）。

配套 `export_chains.py`：把导出的 JSON 幂等写回本机库，**不需要引擎**——克隆
仓库后一条命令即可拥有同样的题链与题目（否则得自己跑 `seed_chains.py` +
逐链生长，耗时数小时引擎时间）。

用法（项目根）：
    .\\.venv\\Scripts\\python.exe scripts\\import_chains.py
    # 默认读 data/chains/chains_export.json，写 data/goapp.db
    # 桌面版用户（数据在 %APPDATA%\\GoCoachAI）加 --desktop
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common import db as db_mod  # noqa: E402

DEFAULT_SRC = ROOT / "data" / "chains" / "chains_export.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--db", default=None, help="目标库（默认 data/goapp.db）")
    ap.add_argument("--desktop", action="store_true",
                    help="写桌面版库（%%APPDATA%%\\GoCoachAI\\goapp.db）")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_file():
        print(f"[X] 找不到导出文件：{src}")
        print("    先在有题的机器上跑 scripts/export_chains.py")
        sys.exit(1)
    data = json.loads(src.read_text(encoding="utf-8"))
    target = args.db
    if args.desktop:
        target = Path(os.environ["APPDATA"]) / "GoCoachAI" / "goapp.db"
    db_mod.init_db(target)          # 老库自动补齐迁移（v10/v11 的链表与列）
    conn = db_mod.connect(target)
    added_c = added_p = 0
    try:
        for c in data.get("chains") or []:
            cols = [k for k in c if c[k] is not None or k in ("id", "name",
                                                              "root_sgf")]
            q = ",".join(cols)
            ph = ",".join("?" * len(cols))
            cur = conn.execute(
                f"INSERT OR IGNORE INTO problem_chains ({q}) VALUES ({ph})",
                [c[k] for k in cols])
            added_c += cur.rowcount
        for p in data.get("problems") or []:
            cols = [k for k in p if k in (
                "id", "source", "review_id", "theme", "rank_min", "rank_max",
                "setup_sgf", "answer", "branches", "verdict", "hint",
                "explanation", "status", "goal", "chain_id", "chain_step",
                "created_at")]
            q = ",".join(cols)
            ph = ",".join("?" * len(cols))
            cur = conn.execute(
                f"INSERT OR IGNORE INTO problems ({q}) VALUES ({ph})",
                [p[k] for k in cols])
            added_p += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    where = target if target else db_mod.DB_PATH
    print(f"[OK] 新增链 {added_c}、题目 {added_p}（已存在的不重复）→ {where}")


if __name__ == "__main__":
    main()
