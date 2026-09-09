"""拆分 u-go.net 的 xxqj.sgf（347 题含答案）为每题一个 SGF。

输出 data/library/_xxqj_answers/split/NNNN.sgf：
根节点保留 GM/SZ/AB/AW/PL，主线保留首手（答案）+ 完整变化，
供 import_classic_answers.py 答案模式导入。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "library" / "_xxqj_answers" / "xxqj.sgf"
OUT = ROOT / "data" / "library" / "_xxqj_answers" / "split"


def split_toplevel(text: str) -> list[str]:
    """按括号深度拆分顶层 (...) 块。"""
    blocks = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start >= 0:
                blocks.append(text[start:i + 1])
                start = -1
    return blocks


def main() -> None:
    text = SRC.read_text(encoding="utf-8", errors="replace")
    blocks = [b for b in split_toplevel(text) if "GM[" in b or ("SZ[" in b and "AB" in b)]
    OUT.mkdir(parents=True, exist_ok=True)
    n = 0
    for b in blocks:
        gm = re.search(r"GM\s*\[\s*(\d+)", b, re.IGNORECASE)
        sz = re.search(r"SZ\s*\[\s*(\d+)", b, re.IGNORECASE)
        setup = re.search(r"(AB|AW)\s*\[", b)
        if setup is None:
            continue
        n += 1
        # 根节点标准化：GM[1]FF[4]SZ[n] + AB/AW + PL（若原文件缺 GM/FF 补上）
        gm_txt = f"GM[{gm.group(1) if gm else 1}]"
        sz_txt = f"SZ[{sz.group(1) if sz else 19}]"
        # 去掉原 GM/FF/SZ，统一写入头部
        body = re.sub(r"GM\s*\[\s*\d+\]", "", b, flags=re.IGNORECASE)
        body = re.sub(r"FF\s*\[\s*\d+\]", "", body, flags=re.IGNORECASE)
        body = re.sub(r"SZ\s*\[\s*\d+\]", "", body, flags=re.IGNORECASE)
        body = body.replace("(;", "", 1)
        out = f"(;{gm_txt}FF[4]{sz_txt};{body}"
        if not out.endswith(")"):
            out += ")"
        (OUT / f"{n:06d}.sgf").write_text(out, encoding="utf-8")
    print(f"split {n} problems -> {OUT}")


if __name__ == "__main__":
    main()
