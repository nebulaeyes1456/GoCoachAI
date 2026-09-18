"""题目系统共用工具（窗口3，纯函数，便于单元测试）。

- 坐标换算（界面坐标 ↔ 0 基 (x, y)，y=0 为最下行）；
- 对局重放（含提子、禁自杀，忽略打劫），用于从"失误手之前"的局面构图；
- 棋串（连通块）与气计算；
- 局部裁剪：以失误手为中心、半径 ``crop_radius`` 的方框，保留与框内
  相交的**完整棋串**（任务书：半径 3~4 路，保留完整棋串）；
  ``crop_bbox`` 是公共实现（包围盒裁剪），``crop_stones`` 与题链生长
  的区域裁剪都基于它，避免各写一份「保留完整棋串」的逻辑；
- 题面 SGF 构造：AB/AW 摆子 + 奇偶修正 pass（让 verify_position 正确
  识别行棋方，见 generator 注释）；
- 主题归类 / 难度分级 / 提示与结论规则模板 / 默认级位区间。

主题归类采用务实策略（任务书允许，写注释）：
- **endgame**：失误手手数 > 总手数 × ``endgame_move_fraction``（0.8）；
- **capturing_race**：裁剪区内双方均有气数 ≤3 的棋串（互相紧气对杀）；
- **life_death**：裁剪区内存在气数 ≤2 的棋串（可杀/可活）；
- **middle**：其余中盘选点失误。
"""

from __future__ import annotations

import hashlib
from collections import deque
from typing import Iterable, Optional

from ...common import sgf_io

# 界面列字母表（A~T 跳过 I），与 common/sgf_io.py 保持一致
_COLS = sgf_io.BOARD_COLS

COLOR_CN = {"B": "黑", "W": "白"}
VALID_THEMES = ("life_death", "capturing_race", "endgame", "middle")


# ---------------------------------------------------------------------------
# 坐标
# ---------------------------------------------------------------------------


def coord_to_xy(coord: str) -> tuple[int, int]:
    """界面坐标 "D15" → (x, y)，x=列下标 0 基，y=行号-1（y=0 为最下行）。"""
    c = (coord or "").strip().upper()
    if not c or c == "PASS":
        raise ValueError(f"空坐标: {coord!r}")
    col_letter, row_text = c[0], c[1:]
    if col_letter not in _COLS:
        raise ValueError(f"非法列字母: {coord!r}")
    return _COLS.index(col_letter), int(row_text) - 1


def xy_to_coord(x: int, y: int, size: int) -> str:
    """(x, y) → 界面坐标（x∈[0,size), y∈[0,size)）。"""
    if not (0 <= x < size and 0 <= y < size):
        raise ValueError(f"坐标越界: ({x}, {y}) size={size}")
    return f"{_COLS[x]}{y + 1}"


def neighbors(x: int, y: int, size: int) -> list[tuple[int, int]]:
    """四邻接点（不越界）。"""
    out = []
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < size and 0 <= ny < size:
            out.append((nx, ny))
    return out


# ---------------------------------------------------------------------------
# 对局重放与棋串
# ---------------------------------------------------------------------------

Position = dict[tuple[int, int], str]  # (x, y) -> "B"/"W"


def apply_moves(
    stones: Position,
    moves: Iterable[tuple[str, str]],
    size: int,
) -> Position:
    """在既有局面上继续落子（含提子；自杀着法抛 ValueError），返回同一字典。

    摆子局（AB/AW 摆子 + 手顺）与整盘重放共用这一份落子逻辑。
    """
    for color, coord in moves:
        if not coord or coord.strip().upper() == "PASS":
            continue
        x, y = coord_to_xy(coord)
        if (x, y) in stones:
            raise ValueError(f"落子于已有棋子处: {coord}")
        stones[(x, y)] = color.upper()
        opp = "W" if color.upper() == "B" else "B"
        captured_any = False
        for nx, ny in neighbors(x, y, size):
            if stones.get((nx, ny)) == opp:
                g = group_at(stones, nx, ny, size)
                if not group_liberties(g, stones, size):
                    for px, py in g:
                        stones.pop((px, py), None)
                    captured_any = True
        if not captured_any:
            g = group_at(stones, x, y, size)
            if not group_liberties(g, stones, size):
                stones.pop((x, y), None)
                raise ValueError(f"自杀着法: {coord}")
    return stones


def replay_position(
    moves: Iterable[tuple[str, str]], size: int
) -> Position:
    """按主线着法重放局面（含提子；自杀着法抛 ValueError）。

    忽略打劫禁令：打劫提回会得到"当前局面 + 对方刚提子"的近似图，
    对半径 4 路的局部裁剪影响可忽略（生成器的题面以此为代价换取简单性）。
    """
    return apply_moves({}, moves, size)


def group_at(
    stones: Position, x: int, y: int, size: int
) -> set[tuple[int, int]]:
    """含 (x, y) 的连通棋串（4 邻接，同色）。"""
    color = stones.get((x, y))
    if color is None:
        return set()
    seen = {(x, y)}
    dq = deque([(x, y)])
    while dq:
        cx, cy = dq.popleft()
        for nx, ny in neighbors(cx, cy, size):
            if stones.get((nx, ny)) == color and (nx, ny) not in seen:
                seen.add((nx, ny))
                dq.append((nx, ny))
    return seen


def group_liberties(
    group: Iterable[tuple[int, int]], stones: Position, size: int
) -> set[tuple[int, int]]:
    """棋串的全部气（空邻点）。"""
    libs: set[tuple[int, int]] = set()
    for x, y in group:
        for nx, ny in neighbors(x, y, size):
            if (nx, ny) not in stones:
                libs.add((nx, ny))
    return libs


def all_groups(stones: Position, size: int) -> list[set[tuple[int, int]]]:
    """全盘棋串列表（每串一个集合）。"""
    visited: set[tuple[int, int]] = set()
    groups: list[set[tuple[int, int]]] = []
    for pt, _color in sorted(stones.items()):
        if pt in visited:
            continue
        g = group_at(stones, pt[0], pt[1], size)
        visited |= g
        groups.append(g)
    return groups


# ---------------------------------------------------------------------------
# 局部裁剪与题面 SGF
# ---------------------------------------------------------------------------


def bbox_of(points: Iterable[tuple[int, int]]) -> tuple[int, int, int, int]:
    """点集的包围盒 (x0, y0, x1, y1)（含端点）；空集抛 ValueError。"""
    pts = list(points)
    if not pts:
        raise ValueError("空点集没有包围盒")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def crop_bbox(
    position: Position,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    size: int,
) -> Position:
    """按包围盒 [x0..x1] × [y0..y1]（含端点）裁剪；与框相交的棋串整体保留。

    公共裁剪函数（generator 的中心裁剪与 chains 的定式区域裁剪共用），
    边界自动收敛到棋盘内；坐标顺序颠倒（x0 > x1）时返回空。
    """
    kept: Position = {}
    for x in range(max(0, x0), min(size - 1, x1) + 1):
        for y in range(max(0, y0), min(size - 1, y1) + 1):
            color = position.get((x, y))
            if color is not None:
                kept[(x, y)] = color
    # 框边界上与外部相连的棋串：补全整串（保留完整棋串）
    for group in all_groups(position, size):
        if any(pt in kept for pt in group):
            for pt in group:
                kept[pt] = position[pt]
    return kept


def crop_stones(
    position: Position,
    center_xy: tuple[int, int],
    radius: int,
    size: int,
) -> Position:
    """以 center 为中心、Chebyshev 半径 radius 裁剪；与框相交的棋串整体保留。"""
    cx, cy = center_xy
    return crop_bbox(
        position, cx - radius, cy - radius, cx + radius, cy + radius, size
    )


def setup_sgf(
    kept: Position,
    solver_color: str,
    size: int,
    komi: float = 7.5,
) -> str:
    """由摆子构造题面 SGF（AB/AW）。

    verify_position 以主线手数奇偶判断行棋方，纯摆子局面需要修正：
    - 黑先：0 手主线 → 奇偶判定为 B，无需修正；
    - 白先：前置一手黑 pass（`;B[tt]`）使主线长度变奇 → 判定为 W。
    pass 对局面无影响，是保持接口签名不变下的最小修正（见 docs/plan.md）。
    """
    ab = sorted(xy_to_coord(x, y, size) for (x, y), c in kept.items() if c == "B")
    aw = sorted(xy_to_coord(x, y, size) for (x, y), c in kept.items() if c == "W")
    parts = ["(;GM[1]FF[4]CA[UTF-8]", f"SZ[{size}]", f"KM[{komi}]"]
    if ab:
        parts.append("AB" + "".join(f"[{sgf_io.coord_to_sgf(c, size)}]" for c in ab))
    if aw:
        parts.append("AW" + "".join(f"[{sgf_io.coord_to_sgf(c, size)}]" for c in aw))
    body = ""
    if solver_color.upper() == "W":
        body = ";B[" + chr(ord("a") + size) * 2 + "]"
    return "".join(parts) + body + ")"


# ---------------------------------------------------------------------------
# 主题归类（务实策略）
# ---------------------------------------------------------------------------


def classify_theme(
    kept: Position,
    full_position: Position,
    size: int,
    move_number: int,
    total_moves: int,
    endgame_fraction: float = 0.8,
) -> str:
    """对一道候选失误题做主题归类（规则见模块 docstring）。

    只统计"有意义的"棋串：长度 ≥ 2 的串，或仅 1 气的单子（被打吃）。
    普通单子（2 气以上）不触发死活/对杀归类，避免误判。
    """
    if total_moves > 0 and move_number / total_moves >= endgame_fraction:
        return "endgame"
    libs_by_color: dict[str, list[int]] = {"B": [], "W": []}
    for group in all_groups(kept, size):
        color = kept[next(iter(group))]
        libs = len(group_liberties(group, full_position, size))
        if len(group) >= 2 or libs <= 1:
            libs_by_color.setdefault(color, []).append(libs)
    tight = [
        l for color in libs_by_color for l in libs_by_color[color] if l <= 3
    ]
    if libs_by_color["B"] and libs_by_color["W"] and len(tight) >= 2:
        return "capturing_race"
    if any(l <= 2 for color in libs_by_color for l in libs_by_color[color]):
        return "life_death"
    return "middle"


# ---------------------------------------------------------------------------
# 提示 / 结论 / 难度 / 级位（规则模板）
# ---------------------------------------------------------------------------


def region_label(coord: str, size: int) -> str:
    """坐标 → 盘面方位词（"左上"…"右下"；正中央返回"中央"）。"""
    x, y = coord_to_xy(coord)
    third = size / 3.0
    col_word = "左" if x < third else ("右" if x >= 2 * third else "中")
    row_word = "下" if y < third else ("上" if y >= 2 * third else "中")
    if col_word == "中" and row_word == "中":
        return "中央"
    return f"{col_word}{row_word}"


def hint_text(theme: str, solver_color: str, center_coord: str, size: int) -> str:
    """规则模板提示（任务书：hint 用规则模板生成，不调 LLM）。"""
    c = COLOR_CN.get(solver_color.upper(), solver_color)
    region = region_label(center_coord, size)
    if theme == "life_death":
        return f"{c}先，{region}的棋形处在生死关口，请找出急所（做活或杀棋）。"
    if theme == "capturing_race":
        return f"{c}先，{region}双方对杀紧气，请算清气数再落子。"
    if theme == "endgame":
        return f"{c}先，请找出{region}价值最大的一手收官。"
    return f"{c}先，请找出当前局面{region}的最佳选点。"


def verdict_text(
    theme: str,
    solver_color: str,
    answer: str,
    best_winrate: float,
    second_coord: Optional[str],
    second_winrate: Optional[float],
) -> str:
    """规则模板结论（verdict 字段；讲解由窗口2 coach/ask 补写）。"""
    c = COLOR_CN.get(solver_color.upper(), solver_color)
    wr = f"{best_winrate:.0%}"
    if theme == "life_death":
        core = f"{answer} 是正解（{c}方胜率 {wr}）：这一手救活/杀死局部关键棋串。"
    elif theme == "capturing_race":
        core = f"{answer} 是正解（{c}方胜率 {wr}）：对杀中抢先紧气是制胜关键。"
    elif theme == "endgame":
        core = f"{answer} 是正解（{c}方胜率 {wr}）：收官阶段价值最大的一手。"
    else:
        core = f"{answer} 是正解（{c}方胜率 {wr}）：当前局面的最佳选点。"
    if second_coord is not None and second_winrate is not None:
        core += (
            f" 次优候选 {second_coord} 只有 {second_winrate:.0%}，"
            f"不能解决问题。"
        )
    return core


def difficulty_from_gap(
    gap: float, target_rank: int
) -> tuple[int, int, int]:
    """正解与次优点胜率差 → 难度档 1~5 与适用级位区间。

    gap 越大说明正解越"显眼"（题越容易）：d=1（容易）~ d=5（难）。
    级位区间：以 target_rank 为中心、按难度上下平移，宽 4 级
    （如 target=-5：d=1 → -9..-5；d=3 → -7..-3；d=5 → -5..-1）。
    """
    if gap >= 0.90:
        d = 1
    elif gap >= 0.85:
        d = 2
    elif gap >= 0.80:
        d = 3
    elif gap >= 0.75:
        d = 4
    else:
        d = 5
    center = int(target_rank) + (d - 3)
    return d, center - 2, center + 2


def default_rank_range(theme: str) -> tuple[int, int]:
    """导入题/种子题的默认适用级位（无目标级位信息时）。"""
    if theme == "life_death":
        return (-15, 0)
    if theme == "capturing_race":
        return (-12, 0)
    return (-10, 3)  # endgame / middle


def normalize_coord(coord: str) -> str:
    """答案坐标归一化："d15"/"D15"→"D15"；"pass"/""→"pass"。"""
    c = (coord or "").strip().upper()
    return "pass" if c in ("", "PASS") else c


def problem_id(setup_sgf: str, theme: str) -> str:
    """题目主键：题面 SGF + 主题的 sha256 前 16 位（幂等生成）。"""
    digest = hashlib.sha256(
        f"{theme}|{setup_sgf}".encode("utf-8")
    ).hexdigest()[:16]
    return f"p{digest}"
