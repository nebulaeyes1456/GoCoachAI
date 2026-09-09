"""抓取 OGS《忘忧清乐集》37 题（公版宋代古籍）→ import_classics 格式。

OGS puzzle collection 47 "The Book of Pure Pleasures and Forgotten Worries"。
题面 initial_state（black/white 为 2 字符坐标串，界面坐标 A-T 无 I、A=底），
initial_player=行棋方，move_tree 首分支=原书答案首手。

输出 data/library/import_classics/wangyou/NNNN.sgf（可被
import_classic_answers.py 答案模式 / --auto 模式导入）。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common import sgf_io  # noqa: E402

OUT_DIR = ROOT / "data" / "library" / "import_classics" / "wangyou"
API = "https://online-go.com/api/v1/puzzles/{}/"

COLS = "ABCDEFGHJKLMNOPQRST"  # 界面列：无 I
ROWS = "ABCDEFGHJKLMNOPQRST"


def fetch_json(url: str) -> dict:
    req = request.Request(url, headers={"User-Agent": "yiyou-library-import/1.0"})
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ogx_to_sgf(x: int, y: int, size: int) -> str:
    """OGS (x,y) 0 基（列 A~T 无 I，行 A=底）→ SGF 坐标。"""
    coord = COLS[x] + str(y + 1)  # 界面坐标（列字母+行数字）
    return sgf_io.coord_to_sgf(coord, size)


def first_answer(puzzle: dict) -> tuple[int, int] | None:
    """move_tree 首分支首手=原书答案。"""
    tree = (puzzle.get("puzzle") or {}).get("move_tree") or {}
    branches = tree.get("branches") or []
    if not branches:
        return None
    b = branches[0]
    if isinstance(b.get("x"), int) and isinstance(b.get("y"), int):
        return b["x"], b["y"]
    return None


def main() -> None:
    # 系列 37 题 id 列表
    listing = fetch_json("https://online-go.com/api/v1/puzzles?collection=47&page_size=50")
    ids = [p["id"] for p in listing.get("results") or []]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for idx, pid in enumerate(ids, start=1):
        d = fetch_json(API.format(pid))
        pz = d.get("puzzle") or {}
        state = pz.get("initial_state") or {}
        black = state.get("black") or ""
        white = state.get("white") or ""
        player = (pz.get("initial_player") or "black").lower()
        size = int(d.get("width") or 19)
        ab, aw = [], []
        for c in re.findall(r"..", black):
            if len(c) == 2 and c[0].upper() in COLS and c[1].upper() in ROWS:
                ui = c[0].upper() + str(ROWS.index(c[1].upper()) + 1)
                ab.append(sgf_io.coord_to_sgf(ui, size))
        for c in re.findall(r"..", white):
            if len(c) == 2 and c[0].upper() in COLS and c[1].upper() in ROWS:
                ui = c[0].upper() + str(ROWS.index(c[1].upper()) + 1)
                aw.append(sgf_io.coord_to_sgf(ui, size))
        ans = first_answer(d)
        cn_color = "白先" if player == "white" else "黑先"
        head = (
            f"(;GM[1]FF[4]CA[UTF-8]SZ[{size}]RE[life_death]"
            f"PB[《忘忧清乐集》第{idx}题]PW[{cn_color}·公版古典死活]"
        )
        if ab:
            head += "AB" + "".join(f"[{x}]" for x in ab)
        if aw:
            head += "AW" + "".join(f"[{x}]" for x in aw)
        if ans:
            ans_color = "W" if player == "white" else "B"
            head += f";{ans_color}[{ogx_to_sgf(ans[0], ans[1], size)}]"
        elif player == "white":
            head += ";B[tt]"
        (OUT_DIR / f"{idx:06d}.sgf").write_text(head + ")", encoding="utf-8")
        n += 1
        time.sleep(0.4)  # 礼貌节流
    print(f"fetched {n} problems -> {OUT_DIR}")


if __name__ == "__main__":
    main()
