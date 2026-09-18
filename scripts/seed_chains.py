# -*- coding: utf-8 -*-
"""定式种子注册（v1.7.0 死活题生长链条）。

把 ``data/chains/*.sgf`` 里的定式注册进 ``problem_chains`` 表（幂等）：

- 链 id：``chain-<文件名>``（如 ``chain-tuotui``）；
- 链名：SGF 根节点的 ``C[...]``（约定写定式名，如「托退定式」）；
- 主题：``RE[...]``（life_death / capturing_race / mixed，缺省 mixed）；
- 说明：``GC[...]``；``root_sgf`` 为该 SGF 全文（含全部手顺）。

重复执行只覆盖内容字段（name/theme/root_sgf/description），保留 status 与
created_at —— 改完 SGF 重跑即同步，不会动已经长出来的题。

用法：
    python scripts/seed_chains.py                 # 注册全部种子
    python scripts/seed_chains.py --list          # 只列出现有链
    python scripts/seed_chains.py --dir other/    # 指定种子目录
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common import db as db_mod  # noqa: E402
from backend.services.problems import chains  # noqa: E402

DEFAULT_DIR = ROOT / "data" / "chains"
VALID_THEMES = ("life_death", "capturing_race", "mixed")


def _prop(sgf_text: str, key: str) -> str:
    """取 SGF 根节点属性值（如 C[...] / RE[...] / GC[...]）。"""
    m = re.search(rf"{key}\[([^\]]*)\]", sgf_text or "", re.IGNORECASE)
    return (m.group(1) if m else "").strip()


def chain_from_file(path: Path) -> dict:
    """种子 SGF → problem_chains 行（链名取 C[]，主题取 RE[]）。"""
    text = path.read_text(encoding="utf-8")
    name = _prop(text, "C") or _prop(text, "GN") or path.stem
    theme = _prop(text, "RE").lower()
    if theme not in VALID_THEMES:
        theme = "mixed"
    return {
        "id": f"chain-{path.stem}",
        "name": name,
        "theme": theme,
        "root_sgf": text.strip(),
        "seed_sgf": None,   # 空 = 自动从 root_sgf 重放裁剪（见 chains.grow_chain）
        "description": _prop(text, "GC"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--list", action="store_true", help="只列出现有链")
    ap.add_argument("--db", default=None, help="数据库路径（默认 data/goapp.db）")
    args = ap.parse_args()

    if args.list:
        db_mod.init_db(args.db)
        for c in chains.list_chains(args.db):
            print(f"{c['id']:24s} {c['name']:12s} {c['theme'] or '-':14s}"
                  f" 题数={c['problems_count']:3d} status={c['status']}")
        return

    seed_dir = Path(args.dir)
    files = sorted(seed_dir.glob("*.sgf"))
    if not files:
        print(f"[X] {seed_dir} 下没有 .sgf 种子文件")
        return
    db_mod.init_db(args.db)
    created = updated = 0
    for path in files:
        chain = chain_from_file(path)
        is_new = chains.register_chain(chain, args.db)
        created += 1 if is_new else 0
        updated += 0 if is_new else 1
        print(f"[OK] {chain['id']:24s} {chain['name']:12s}"
              f" {chain['theme']:14s} {'新建' if is_new else '更新'}")
    print(f"\n共 {len(files)} 条链：新建 {created}，更新 {updated}"
          f"（库：{args.db or db_mod.DB_PATH}）")


if __name__ == "__main__":
    main()
