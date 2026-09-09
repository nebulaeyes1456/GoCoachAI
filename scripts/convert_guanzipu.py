"""官子谱（Guanzi Pu）转换：baduk 库 SGF → import_library 格式。

来源：benjaminmantle/baduk-study-material（collections/guanzipu）
《官子谱》(1690, 陶式玉) 为清代公版著作，positions 属公有领域
（见该库 docs/provenance.md 的 honesty note：Guanzipu 为 genuine
public domain；本脚本仅导入题面摆子，不含该库任何 PDF 内容）。

源格式：每题一个 SGF，GB18030 编码，结构
  (;AW[...]AB[...]PL[B]GN[官子谱 第N题];W[...];B[...]... )   ← 主线是答案序列
转换后仅保留题面（AB/AW 摆子 + PL 行棋方），主线答案剥离；
答案由 import_library 的 KataGo 验题自动判定（0.95/0.3 战术阈值）。

用法：python scripts/convert_guanzipu.py
输出：data/library/import_classics/guanzipu/<n>.sgf
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "data" / "library" / "_guanzipu_extract" / "guanzipu"
OUT_DIR = ROOT / "data" / "library" / "import_classics" / "guanzipu"

RE_SETUP = r"(AB|AW)((?:\[[a-zA-Z]*\])+)"
RE_PL = r"PL\s*\[\s*([BW])\s*\]"
RE_GN = r"GN\s*\[([^\]]*)\]"


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def main() -> int:
    files = sorted(
        SRC_DIR.glob("*.sgf"),
        key=lambda p: int(p.stem) if p.stem.isdigit() else -1,
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.sgf"):
        old.unlink()
    count = 0
    skipped = 0
    for path in files:
        text = read_text(path)
        ab_parts, aw_parts = [], []
        for pm in re.finditer(RE_SETUP, text, re.IGNORECASE):
            kind, block = pm.group(1).upper(), pm.group(2)
            coords = [x for x in re.findall(r"\[([a-zA-Z]*)\]", block) if x]
            if kind == "AB":
                ab_parts.extend(coords)
            else:
                aw_parts.extend(coords)
        if not ab_parts and not aw_parts:
            skipped += 1
            continue
        pl = re.search(RE_PL, text)
        color = "white" if (pl and pl.group(1).upper() == "W") else "black"
        gn = re.search(RE_GN, text)
        label = gn.group(1).strip() if gn else f"第{path.stem}题"
        cn_color = "白先" if color == "white" else "黑先"
        head = (
            f"(;GM[1]FF[4]CA[UTF-8]SZ[19]RE[life_death]"
            f"PB[《官子谱》{label}]PW[{cn_color}·公版古典死活]"
        )
        if ab_parts:
            head += "AB" + "".join(f"[{x}]" for x in ab_parts)
        if aw_parts:
            head += "AW" + "".join(f"[{x}]" for x in aw_parts)
        body = ";B[tt]" if color == "white" else ""
        (OUT_DIR / f"{int(path.stem):04d}.sgf").write_text(
            head + body + ")", encoding="utf-8"
        )
        count += 1
    print(f"[convert] 官子谱: {count} 题（跳过 {skipped}）-> {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
