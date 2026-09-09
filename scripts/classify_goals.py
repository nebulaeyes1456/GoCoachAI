# -*- coding: utf-8 -*-
"""题目目标分类器 v1 —— 先规则、后 AI。

判断标准（对 life_death 死活题）：
  1. 题面块分析（BFS 连通块 → 每块气数）：
     - 极危：气数 ≤1（被紧到最后一口气，必须立即行动）
     - 危急：气数 ≤3
     - 受困：气数 ≤5
  2. 答案首手邻接分析（answer.coord 四邻）：
     - 只邻己方棋子 → 「做活」倾向（接回/延气/做眼）
     - 只邻对方棋子 → 「杀棋」倾向（紧气/破眼）
     - 邻接双方或都不邻 → 倾向不明
  3. 组合规则（按置信度）：
     高：己方极危 且 对方无危机块 → 做活；对方极危 且 己方无 → 杀棋
     中：己方危急（≤3 气）且对方无 → 做活；对方危急且己方无 → 杀棋
     中：双方都危急 → 对杀
     辅：块级信号不明时，用答案邻接信号判定（只邻己=做活、只邻对=杀棋）
     其余 → 存疑（交给 AI 兜底）
其他主题直接映射：
     capturing_race → 对杀取胜；endgame → 收官最大；middle → 中盘要点

用法：
  python scripts/classify_goals.py --dry-run     # 只统计覆盖率，不写库
  python scripts/classify_goals.py --write       # 规则结果写库（goal 列）
  python scripts/classify_goals.py --with-ai     # 存疑题 AI 兜底后写库（约¥1，断点续跑）
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common.sgf_io import sgf_to_coord  # noqa: E402

DB = ROOT / "data" / "goapp.db"

DIRS4 = ((1, 0), (-1, 0), (0, 1), (0, -1))

THEME_GOALS = {
    "capturing_race": "对杀取胜",
    "endgame": "收官最大",
    "middle": "中盘要点",
}


def parse_setup(setup_sgf: str):
    """AB/AW 摆子 → (size, stones: list[(color, x, y)])，x 0..size-1 从左往右，y 0..size-1 从下往上。"""
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
                    col -= 1  # 跳过 I
                row = size - int(coord[1:])
                stones.append((color, col, row))
    return size, stones


def analyze_board(size, stones):
    """连通块分析 → blocks: [{color, count, liberties, liberty_set, bbox}]"""
    grid = [[0] * size for _ in range(size)]
    for color, x, y in stones:
        if 0 <= x < size and 0 <= y < size:
            grid[y][x] = color
    seen = [[False] * size for _ in range(size)]
    blocks = []
    for y in range(size):
        for x in range(size):
            c = grid[y][x]
            if not c or seen[y][x]:
                continue
            q = deque([(x, y)])
            seen[y][x] = True
            stones_ = []
            libs = set()
            while q:
                cx, cy = q.popleft()
                stones_.append((cx, cy))
                for dx, dy in DIRS4:
                    nx, ny = cx + dx, cy + dy
                    if not (0 <= nx < size and 0 <= ny < size):
                        continue
                    if grid[ny][nx] == 0:
                        libs.add((nx, ny))
                    elif grid[ny][nx] == c and not seen[ny][nx]:
                        seen[ny][nx] = True
                        q.append((nx, ny))
            xs = [p[0] for p in stones_]
            ys = [p[1] for p in stones_]
            blocks.append({
                "color": c, "count": len(stones_),
                "liberties": len(libs),
                "lib_set": libs,
                "bbox": (min(xs), min(ys), max(xs), max(ys)),
            })
    return blocks


def find_tendons(size, stones, color):
    """找 color 色的棋筋：同色四邻 ≥2 且移除本子后邻居互不连通的子。返回 [(x,y,气数)]"""
    grid = [[0] * size for _ in range(size)]
    for c, x, y in stones:
        if 0 <= x < size and 0 <= y < size:
            grid[y][x] = c
    me = {c: (x, y) for c, x, y in stones if c == color}
    tendons = []
    for (x, y) in me.values():
        nbrs = []
        for dx, dy in DIRS4:
            nx, ny = x + dx, y + dy
            if 0 <= nx < size and 0 <= ny < size and grid[ny][nx] == color:
                nbrs.append((nx, ny))
        if len(nbrs) < 2:
            continue
        # 移除本子后从第一个邻居 BFS，检查其余邻居是否可达
        start = nbrs[0]
        q = deque([start])
        vis = {start}
        while q:
            cx, cy = q.popleft()
            for dx, dy in DIRS4:
                nx, ny = cx + dx, cy + dy
                if (nx, ny) == (x, y) or (nx, ny) in vis:
                    continue
                if 0 <= nx < size and 0 <= ny < size and grid[ny][nx] == color:
                    vis.add((nx, ny))
                    q.append((nx, ny))
        if not all(n in vis for n in nbrs):
            libs = 0
            for dx, dy in DIRS4:
                nx, ny = x + dx, y + dy
                if 0 <= nx < size and 0 <= ny < size and grid[ny][nx] == 0:
                    libs += 1
            tendons.append((x, y, libs))
    return tendons


def classify_life_death(size, stones, solver, answer_coord):
    """死活题 → (goal, confidence, basis)

    规则（按优先级）：
      1. 己方 ≤1 气且对方无 ≤3 气块 → 做活（高）
      2. 对方 ≤1 气且己方无 ≤3 气块 → 杀棋（高）
      3. 己方 ≤3 气且对方无 → 做活（中）
      4. 对方 ≤3 气且己方无 → 杀棋（中）
      5. 双方都有 ≤3 气块：
         - 两个最危块互相紧气（共享公共气点）→ 对杀（高）
         - 否则看答案邻接：只邻己=做活、只邻对=杀棋、其余存疑
      6. 双方无危急块：答案邻接信号；无信号存疑（AI 兜底）
    """
    blocks = analyze_board(size, stones)
    me, opp = solver, "W" if solver == "B" else "B"
    me_c, opp_c = 1 if me == "B" else 2, 1 if opp == "B" else 2

    # 棋筋信号（优先级最高）：气 ≤2 的筋子 —— 逃出己方筋 / 吃掉对方筋
    me_tendons = [t for t in find_tendons(size, stones, me_c) if t[2] <= 2]
    opp_tendons = [t for t in find_tendons(size, stones, opp_c) if t[2] <= 2]
    if me_tendons and not opp_tendons:
        return "逃棋筋", "high", f"己方棋筋受攻(气≤2，{len(me_tendons)}处)"
    if opp_tendons and not me_tendons:
        return "吃棋筋", "high", f"对方棋筋可吃(气≤2，{len(opp_tendons)}处)"

    def worst_block(color, threshold):
        cand = [b for b in blocks if b["color"] == color and b["liberties"] <= threshold]
        return min(cand, key=lambda b: b["liberties"]) if cand else None

    me1 = worst_block(me_c, 1)
    me3 = worst_block(me_c, 3)
    opp1 = worst_block(opp_c, 1)
    opp3 = worst_block(opp_c, 3)

    # 答案邻接信号
    adj_me = adj_opp = False
    if answer_coord:
        col = ord(answer_coord[0].upper()) - 65
        if col >= 8:
            col -= 1
        row = size - int(answer_coord[1:])
        grid = [[0] * size for _ in range(size)]
        for color, x, y in stones:
            if 0 <= x < size and 0 <= y < size:
                grid[y][x] = color
        for dx, dy in DIRS4:
            nx, ny = col + dx, row + dy
            if 0 <= nx < size and 0 <= ny < size and grid[ny][nx]:
                if grid[ny][nx] == me_c:
                    adj_me = True
                else:
                    adj_opp = True

    if me1 is not None and opp3 is None:
        return "做活", "high", "己方仅剩1气块且对方无危机"
    if opp1 is not None and me3 is None:
        return "杀棋", "high", "对方仅剩1气块且己方无危机"
    if me3 is not None and opp3 is None:
        return "做活", "mid", "己方有≤3气块且对方无"
    if opp3 is not None and me3 is None:
        return "杀棋", "mid", "对方有≤3气块且己方无"
    if me3 is not None and opp3 is not None:
        # 双方都危急：最危两块的公共气点非空 → 真对杀
        shared = me3["lib_set"] & opp3["lib_set"]
        if shared:
            return "对杀", "high", "双方危机块互相紧气"
        # 答案落点信号：落在己方危机块的气点上=延气/做眼；落在对方气点上=紧气/破眼
        ans_pt = None
        if answer_coord:
            col = ord(answer_coord[0].upper()) - 65
            if col >= 8:
                col -= 1
            row = size - int(answer_coord[1:])
            ans_pt = (col, row)
        if ans_pt in me3["lib_set"]:
            return "做活", "mid", "答案落在己方危机块气点上"
        if ans_pt in opp3["lib_set"]:
            return "杀棋", "mid", "答案落在对方危机块气点上"
        if adj_me and not adj_opp:
            return "做活", "mid", "危机块未互紧+答案邻己"
        if adj_opp and not adj_me:
            return "杀棋", "mid", "危机块未互紧+答案邻对"
        return None, None, "双方危急但未互紧（存疑）"
    if adj_me and not adj_opp:
        return "做活", "mid", "答案邻接己方"
    if adj_opp and not adj_me:
        return "杀棋", "mid", "答案邻接对方"
    return None, None, "规则无法判定（存疑）"


GOAL_OPTIONS = ["做活", "杀棋", "对杀", "逃棋筋", "吃棋筋"]


def run_ai_classify(rows):
    """存疑题交给 DeepSeek 判断目标；断点续跑（goal 已非空跳过）；max_tokens≤200。"""
    from backend.services.coach.llm import LLMClient, LLMError
    client = LLMClient()
    conn = sqlite3.connect(DB)
    done = skip = 0
    total_cost = 0.0
    try:
        for pid, sgf, solver, book, number in rows:
            existing = conn.execute("SELECT goal FROM problems WHERE id=?", (pid,)).fetchone()
            if existing and existing[0]:
                skip += 1
                continue
            book_text = f"{book} 第 {number} 题" if book and number else (f"{book} 第 {number} 题" if book else "未知出处")
            messages = [
                {"role": "system", "content":
                 "你是围棋死活题分类器。判断题目要求，只输出 JSON：{\"goal\": \"做活\"}。"
                 "可选值：做活（先手方做活自己的棋）/ 杀棋（杀死对方）/ 对杀（互相紧气比快）/"
                 "逃棋筋（救出己方关键棋筋）/ 吃棋筋（吃掉对方关键棋筋）。"},
                {"role": "user", "content":
                 f"出处：{book_text}\n先手方：{'白' if solver == 'W' else '黑'}\n题面 SGF：{sgf[:600]}\n"
                 "这道题的要求是？"},
            ]
            try:
                res = client.chat_json(messages, {"goal": str},
                                       kind="goal_classify", max_tokens=200)
                data = res.data or {}
                goal = str(data.get("goal") or "").strip()
                if goal not in GOAL_OPTIONS:
                    print(f"[AI] {pid} 输出非法 {goal!r}，跳过")
                    continue
                conn.execute("UPDATE problems SET goal=? WHERE id=?", (goal, pid))
                conn.commit()
                done += 1
                total_cost += float(res.cost or 0)
                print(f"[AI] {pid} -> {goal}  累计 {done}/{len(rows)}  ¥{total_cost:.4f}")
            except LLMError as e:
                print(f"[AI] {pid} 失败: {e}，跳过（可重跑续传）")
    finally:
        conn.close()
    print(f"[AI] 完成：成功 {done} 跳过(已有) {skip} 总成本 ¥{total_cost:.4f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--write", action="store_true", help="规则结果写库")
    ap.add_argument("--with-ai", action="store_true", help="存疑题 AI 兜底后写库（含 --write）")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, theme, setup_sgf, branches FROM problems WHERE status='active'").fetchall()
    if args.limit:
        rows = rows[:args.limit]

    stats = {"total": 0, "mapped": 0, "class_ok": 0, "uncertain": 0}
    goal_stats = {}
    theme_stats = {}
    write_rows = []   # (id, goal)
    ai_rows = []      # (id, sgf, solver, book, number) 存疑题
    samples = {"做活": [], "杀棋": [], "对杀": [], "逃棋筋": [], "吃棋筋": [], "对杀取胜": [], "收官最大": [], "中盘要点": [], "存疑": []}

    for r in rows:
        stats["total"] += 1
        theme = r["theme"]
        theme_stats[theme] = theme_stats.get(theme, 0) + 1
        try:
            branches = json.loads(r["branches"] or "{}")
        except (ValueError, TypeError):
            branches = {}
        solver = branches.get("solver") or "B"
        answer = branches.get("answer") or {}
        answer_coord = answer.get("coord")

        if theme in THEME_GOALS:
            goal, conf, basis = THEME_GOALS[theme], "high", "主题直接映射"
            stats["mapped"] += 1
            write_rows.append((r["id"], goal))
        else:
            try:
                size, stones = parse_setup(r["setup_sgf"])
            except Exception as e:
                stats["uncertain"] += 1
                goal, conf, basis = None, None, f"解析失败 {e}"
            else:
                goal, conf, basis = classify_life_death(size, stones, solver, answer_coord)
                if goal:
                    stats["class_ok"] += 1
                    write_rows.append((r["id"], goal))
                else:
                    stats["uncertain"] += 1
                    ai_rows.append((r["id"], r["setup_sgf"], solver,
                                    branches.get("book"), branches.get("number")))

        key = goal or "存疑"
        goal_stats[key] = goal_stats.get(key, 0) + 1
        if len(samples[key]) < 2:
            samples[key].append((r["id"][:8], solver, basis))

    conn.close()

    # --write / --with-ai：把规则结果写入 goal 列
    if args.write or args.with_ai:
        conn = sqlite3.connect(DB)
        n = 0
        for pid, goal in write_rows:
            conn.execute("UPDATE problems SET goal=? WHERE id=? AND goal IS NULL", (goal, pid))
            n += 1
        conn.commit()
        conn.close()
        print(f"[write] 规则结果入库 {n} 条")

    # --with-ai：AI 兜底存疑题
    if args.with_ai and ai_rows:
        run_ai_classify(ai_rows)

    print("== 题库目标分类统计 ==")
    print(f"总题数: {stats['total']}  主题直接映射: {stats['mapped']}  规则判定: {stats['class_ok']}  存疑(需AI): {stats['uncertain']}")
    print("主题分布:", theme_stats)
    print("目标分布:", goal_stats)
    for k, v in samples.items():
        print(f"  [{k}] 样例:", v)


if __name__ == "__main__":
    main()
