# -*- coding: utf-8 -*-
"""死活结果类型 + 脱先判断原型。

对每道角部死活题：
  1. Q_first：轮到先手方查询 → 先手方视角胜率 wr_first（正解方向）
  2. Q_opp：轮到对方查询 → 对方一选 m；再查询 opp 下完后的局面 → wr_after
     tenuki_loss = wr_first - wr_after：先手方脱先让对方抢点的损失
     分级：≥0.15 紧急 / 0.05~0.15 半紧急 / <0.05 可脱先
  3. pv 走完后的 ownership（先手方块归属）→ 供 LLM 判断净活/劫活/双活/净杀/劫杀

用法：python scripts/classify_result.py --sample 10 [--with-llm]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common.sgf_io import sgf_to_coord  # noqa: E402
from backend.services.engine.analyze_sgf import _game_to_query, _resolve_paths  # noqa: E402
from backend.services.engine.engine import KataGoEngine  # noqa: E402

DB = ROOT / "data" / "goapp.db"


def parse_setup(setup_sgf: str):
    size = 19
    m = re.search(r"SZ\s*\[\s*(\d+)", setup_sgf or "", re.IGNORECASE)
    if m:
        size = int(m.group(1))
    stones = []
    for prop, color in (("AB", 1), ("AW", 2)):
        for block in re.finditer(rf"{prop}((?:\[[a-zA-Z]*\])+)", setup_sgf or "", re.IGNORECASE):
            for mm in re.finditer(r"\[([a-zA-Z]*)\]", block.group(1)):
                coord = sgf_to_coord(mm.group(1), size)
                if not coord:
                    continue
                col = ord(coord[0]) - 65
                if col >= 8:
                    col -= 1
                row = size - int(coord[1:])
                stones.append((color, col, row))
    return size, stones


def corner_region(size, stones):
    """角部区域（allowMoves 包围盒外扩 2 格）。"""
    if not stones:
        return None
    xs = [s[1] for s in stones]
    ys = [s[2] for s in stones]
    x0, x1 = max(0, min(xs) - 2), min(size - 1, max(xs) + 2)
    y0, y1 = max(0, min(ys) - 2), min(size - 1, max(ys) + 2)
    moves = []
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            col = chr(65 + x + (1 if x >= 8 else 0))
            moves.append(col + str(size - y))
    return moves


def build_query(setup_sgf, moves, turns, allow):
    req = _game_to_query(setup_sgf, "fast", turns)
    req["moves"] = moves
    if allow:
        req["allowMoves"] = [
            {"player": "B", "moves": allow, "untilDepth": 100},
            {"player": "W", "moves": allow, "untilDepth": 100},
        ]
    return req


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--with-llm", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, setup_sgf, branches, goal FROM problems "
        "WHERE status='active' AND goal IN ('做活','杀棋') LIMIT 2000").fetchall()
    conn.close()

    # 只挑角部题
    picked = []
    for r in rows:
        try:
            size, stones = parse_setup(r["setup_sgf"])
        except Exception:
            continue
        if not stones:
            continue
        xs = [s[1] for s in stones]
        ys = [s[2] for s in stones]
        edges = sum([min(xs) <= 1, max(xs) >= size - 2, min(ys) <= 1, max(ys) >= size - 2])
        if edges >= 2 and max(max(xs) - min(xs), max(ys) - min(ys)) <= 12:
            picked.append(r)
        if len(picked) >= args.sample:
            break

    executable, model, cfg = _resolve_paths()
    eng = KataGoEngine(executable, model, cfg, analysis_threads=1)
    eng.start()
    print("引擎就绪，开始抽样分析…")

    results = []
    for r in picked:
        b = json.loads(r["branches"] or "{}")
        solver = b.get("solver") or "B"
        opp = "W" if solver == "B" else "B"
        answer = (b.get("answer") or {}).get("coord")
        pv = (b.get("answer") or {}).get("pv") or []
        size, stones = parse_setup(r["setup_sgf"])
        allow = corner_region(size, stones)
        setup = r["setup_sgf"]

        # 轮先手方的序列：题面 SGF 已含奇偶修正；KataGo 不接受空 moves，用对方 pass 占位
        first_moves = [["B", "pass"]] if solver == "W" else [["W", "pass"]]
        q1 = build_query(setup, first_moves, [1], allow)
        r1 = eng.query(q1)
        # reportAnalysisWinratesAs=SIDETOMOVE：winrate 已是当前行棋方（=先手方）视角，勿再换算
        wr_first = (r1 or {}).get("rootInfo", {}).get("winrate")

        # 对方先下：opp 一选，再查下完局面（均带 ownership，用于脱先损失）
        opp_moves = first_moves[:]
        q2 = build_query(setup, opp_moves, [1], allow)
        q2["includeOwnership"] = True
        r2 = eng.query(q2)
        infos2 = (r2 or {}).get("moveInfos") or []
        best_opp = next((x for x in infos2 if x.get("order") == 0), None)
        opp_move = best_opp.get("move") if best_opp else None
        wr_after = None
        own_after = None
        if opp_move:
            q3 = build_query(setup, opp_moves + [[opp, opp_move]], [1], allow)
            q3["includeOwnership"] = True
            r3 = eng.query(q3)
            # 下完对手一选后轮到先手方 → winrate 即先手方视角
            wr_after = (r3 or {}).get("rootInfo", {}).get("winrate")
            own_after = (r3 or {}).get("ownership")

        solver_c = 1 if solver == "B" else 2
        # 目标区域：正解点及周围曼哈顿距离 ≤2 的格子（死活的最终归属集中于此）
        target_cells = []
        if answer and re.match(r"^[A-HJ-Z]\d+$", answer):
            col = ord(answer[0]) - 65
            if col >= 8:
                col -= 1
            row = size - int(answer[1:])
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    x, y = col + dx, row + dy
                    if 0 <= x < size and 0 <= y < size:
                        target_cells.append((x, y))

        def own_of(arr):
            """目标区域 ownership（先手方视角，正=目标达成：活/杀成）"""
            if not arr or not target_cells or len(arr) != size * size:
                return None
            vals = [arr[y * size + x] for x, y in target_cells]
            avg = sum(vals) / len(vals)
            return round(avg if solver == "B" else -avg, 4)

        # 正解 pv 走完后的 ownership
        pv_own = None
        wr4 = None
        if pv:
            seq = first_moves[:]
            c = solver
            for m in pv[:8]:
                seq.append([c, m])
                c = "W" if c == "B" else "B"
            q4 = build_query(setup, seq, [len(seq)], allow)
            q4["includeOwnership"] = True
            r4 = eng.query(q4)
            pv_own = own_of((r4 or {}).get("ownership"))
            # pv 走完轮到对方：其胜率越低=先手方正解越彻底（劫活时对方 wr 不会过低）
            wr4 = (r4 or {}).get("rootInfo", {}).get("winrate")

        own_after = own_of(own_after)
        tenuki = None
        if pv_own is not None and own_after is not None:
            tenuki = round(pv_own - own_after, 4)
        grade = None
        if tenuki is not None:
            grade = "紧急" if tenuki >= 0.4 else ("半紧急" if tenuki >= 0.15 else "可脱先")

        # 结果类型初判（净/劫双活），LLM 复核时细化
        rtype = "净"
        if pv_own is not None:
            if pv_own < 0.5:
                rtype = "异常"
            elif pv_own < 0.85 or (wr4 is not None and wr4 > 0.05):
                rtype = "劫/双活"

        results.append({
            "id": r["id"][:8], "goal": r["goal"], "solver": solver,
            "wr_first": wr_first, "wr4": wr4,
            "tenuki_loss": tenuki, "grade": grade,
            "pv_ownership": pv_own, "own_after": own_after, "rtype": rtype,
        })
        print(f"{r['id'][:8]} {r['goal']} {solver}先 own={pv_own} own_after={own_after} "
              f"tenuki={tenuki} [{grade}] wr4={wr4} {rtype}")

    eng.stop()
    print("\n== 抽样统计 ==")
    print("紧急度分布:", Counter(x["grade"] for x in results))
    print("结果类型分布:", Counter(x["rtype"] for x in results))
    print("pv_ownership 分布:", Counter(
        "净(≥0.85)" if (x["pv_ownership"] or 0) >= 0.85 else
        ("偏弱(0.6~0.85)" if (x["pv_ownership"] or 0) >= 0.6 else "弱(<0.6)")
        for x in results))


if __name__ == "__main__":
    main()
