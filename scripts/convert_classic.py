"""古典公版死活题转换：tasuki 题集 → import_library 格式。

来源（公版）：
- Xuanxuan Qijing《玄玄棋经》(1349, 严德甫/晏天章) — xxqj.sgf (347 题)
- Gokyo Shumyo《碁経衆妙》(1812, 林元美) — gokyoshumyo.sgf (520 题)
- Igo Hatsuyoron《发阳论》(1713, 桑原道节) — hatsuyoron.sgf (183 题)

tasuki 题树格式：每题一个 (;C[problem N, color to play]PL[X]AB[...]AW[...])
仅题面无答案 → 答案由 import_library 的 KataGo 验题自动判定。
白先题按仓库约定前置 ;B[tt]（黑 pass）修正行棋方奇偶。

用法：python scripts/convert_classic.py
输出：data/library/import_classics/<书>/<n>.sgf
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "data" / "library" / "_tasuki_extract" / "tasuki2sgf-main" / "generated"
OUT_DIR = ROOT / "data" / "library" / "import_classics"

BOOKS = [
    ("xxqj.sgf", "xxqj", "玄玄棋经", "Xuanxuan Qijing (1349)"),
    ("gokyoshumyo.sgf", "gokyoshumyo", "碁経衆妙", "Gokyo Shumyo (1812)"),
    ("hatsuyoron.sgf", "hatsuyoron", "发阳论", "Igo Hatsuyoron (1713)"),
]

RE_ROOT = r"\(\;"
RE_COMMENT = r"problem\s+(\d+)(?:-(\d+))?,\s*(black|white)\s+to\s+play"
RE_PL = r"PL\s*\[\s*([BW])\s*\]"
RE_SETUP = r"(AB|AW)((?:\[[a-zA-Z]*\])+)"


def extract_trees(text: str) -> list[str]:
    starts = [m.start() for m in re.finditer(RE_ROOT, text)]
    trees = []
    for i, st in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        trees.append(text[st:end])
    return trees


def convert_book(filename: str, slug: str, cn: str, citation: str) -> dict:
    src = (SRC_DIR / filename).read_text(encoding="utf-8", errors="replace")
    trees = extract_trees(src)
    out_dir = OUT_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    # 清空旧输出（幂等重跑）
    for old in out_dir.glob("*.sgf"):
        old.unlink()
    count = 0
    for tree in trees:
        c = re.search(r"C\[([^\]]*)\]", tree)
        comment = c.group(1).replace("\n", " ") if c else ""
        m = re.search(RE_COMMENT, comment, re.IGNORECASE)
        if not m:
            continue  # 介绍/无题号 tree
        num_main, num_sub, color = m.group(1), m.group(2), m.group(3).lower()
        label = num_main if not num_sub else f"{num_main}-{num_sub}"
        pl = re.search(RE_PL, tree)
        if pl:
            color = "white" if pl.group(1).upper() == "W" else "black"
        ab_parts, aw_parts = [], []
        for pm in re.finditer(RE_SETUP, tree, re.IGNORECASE):
            kind, block = pm.group(1).upper(), pm.group(2)
            coords = re.findall(r"\[([a-zA-Z]*)\]", block)
            coords = [x for x in coords if x]
            if kind == "AB":
                ab_parts.extend(coords)
            else:
                aw_parts.extend(coords)
        if not ab_parts and not aw_parts:
            continue
        cn_color = "白先" if color == "white" else "黑先"
        head = (
            f"(;GM[1]FF[4]CA[UTF-8]SZ[19]RE[life_death]"
            f"PB[《{cn}》第{label}题]PW[{cn_color}·公版古典死活]"
        )
        if ab_parts:
            head += "AB" + "".join(f"[{x}]" for x in ab_parts)
        if aw_parts:
            head += "AW" + "".join(f"[{x}]" for x in aw_parts)
        body = ";B[tt]" if color == "white" else ""
        (out_dir / f"{label.replace('-', '_'):>08}.sgf").write_text(
            head + body + ")", encoding="utf-8"
        )
        count += 1
    return {"book": cn, "citation": citation, "count": count, "dir": str(out_dir)}


def main() -> int:
    total = 0
    for filename, slug, cn, citation in BOOKS:
        r = convert_book(filename, slug, cn, citation)
        print(f"[convert] {r['book']}: {r['count']} 题 -> {r['dir']}")
        total += r["count"]
    print(f"[convert] 合计 {total} 题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
