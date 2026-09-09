"""练习深度讲解（窗口3）：局部死活同步讲棋。

``explain_problem(problem_id)`` 生成一道题的深度讲解（同步讲棋分段）：

1. KataGo 局部推演（fast 档 + allowMoves 锁定题面包围盒+正解点）：
   - Q_solve：正解变化 + 正解点周围归属网格 + 对手最强抵抗 + 对手剩余胜率；
   - Q_pass ：先手方脱先（pass）→ 对手局部最佳应对变化 + 脱先后归属网格；
   - 劫争信号：变化中重复落子坐标、对手剩余胜率 10%~40%、归属网格均势格。
2. LLM 把证据翻译成同步讲棋分段（result_type / can_tenuki / segments / takeaway）。
3. 幻觉防线：segments 的 variation 坐标白名单清洗（只保留证据中出现过的坐标）。

结果按 (problem_id, kind=problem_explain) 缓存在 explanations 表（move_number=0），
命中缓存不扣费。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from ...common import db, settings
from ...common.cost import month_cost, record_call
from ...common.sgf_io import sgf_to_coord
from ..coach import prompts
from ..coach.llm import LLMClient
from ..engine.analyze_sgf import _game_to_query, _resolve_paths
from ..engine.engine import KataGoEngine
from . import store, utils

DB = settings.BASE_DIR / "data" / "goapp.db"

MAX_TOKENS = 1200          # 深度讲解输出上限（受限调用，控制成本）
CACHE_KIND = "problem_explain"
MOVE_NUMBER = 0            # 练习讲解在 explanations 表中占用的 move_number


class ProblemNotFoundError(RuntimeError):
    """题目不存在。"""


class BudgetExceededError(RuntimeError):
    """本月预算已用尽。"""


_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


# ---------------------------------------------------------------------------
# 题面解析 / 区域 / 归属网格
# ---------------------------------------------------------------------------

def parse_setup(setup_sgf: str) -> tuple[int, list[tuple[int, int, int]]]:
    """SGF 题面 → (size, [(color, x, y)])，color 1=黑 2=白，y=0 顶行。"""
    size = 19
    m = re.search(r"SZ\s*\[\s*(\d+)", setup_sgf or "", re.IGNORECASE)
    if m:
        size = int(m.group(1))
    stones: list[tuple[int, int, int]] = []
    for prop, color in (("AB", 1), ("AW", 2)):
        for block in re.finditer(
            rf"{prop}((?:\[[a-zA-Z]*\])+)", setup_sgf or "", re.IGNORECASE
        ):
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


def _xy_to_gtp(x: int, y: int, size: int) -> str:
    col = chr(65 + x + (1 if x >= 8 else 0))
    return col + str(size - y)


def region_of(size: int, stones: list[tuple[int, int, int]],
              extra: Optional[list[str]] = None) -> list[str]:
    """allowMoves 区域 = 题面棋子包围盒（不外扩，防远处空旷点噪音）+ 额外点。"""
    moves: list[str] = []
    if stones:
        xs = [s[1] for s in stones]
        ys = [s[2] for s in stones]
        for x in range(min(xs), max(xs) + 1):
            for y in range(min(ys), max(ys) + 1):
                moves.append(_xy_to_gtp(x, y, size))
    for cd in extra or []:
        if cd and cd not in moves:
            moves.append(cd)
    return moves


def own_grid(own: Optional[list[float]], size: int, cx: int, cy: int,
             half: int = 2) -> str:
    """(cx,cy) 周围 ±half 的 ownership 文字网格。X=黑 O=白 .=均势。"""
    if not own or len(own) != size * size:
        return "（无归属数据）"
    lines = []
    for y in range(cy - half, cy + half + 1):
        row = []
        for x in range(cx - half, cx + half + 1):
            if 0 <= x < size and 0 <= y < size:
                v = own[y * size + x]
                row.append("X" if v > 0.25 else ("O" if v < -0.25 else "."))
            else:
                row.append(" ")
        lines.append("".join(row))
    return "\n".join(lines)


def board_grid(size: int, stones: list[tuple[int, int, int]]) -> str:
    """题面局部文字网格（X=黑 O=白 .=空点）。"""
    if not stones:
        return "（无棋子）"
    xs = [s[1] for s in stones]
    ys = [s[2] for s in stones]
    grid = [["." for _ in range(min(xs), max(xs) + 1)]
            for _ in range(min(ys), max(ys) + 1)]
    for color, x, y in stones:
        grid[y - min(ys)][x - min(xs)] = "X" if color == 1 else "O"
    return "\n".join("".join(row) for row in grid)


def _seq_line(seq: list[list[str]]) -> str:
    """[[色,坐标]...] → 「黑F17；白F18；黑D19」文字。"""
    return "；".join(
        f"{'黑' if c == 'B' else '白'}{m}" for c, m in seq
    ) or "（无有效应对）"


def _dup_flags(*groups: list[str]) -> set[str]:
    """多组坐标中重复出现的坐标（劫争标志）。"""
    seen: set[str] = set()
    dups: set[str] = set()
    for g in groups:
        for m in g:
            if m in seen:
                dups.add(m)
            seen.add(m)
    return dups


def _coord_xy(coord: str, size: int) -> tuple[int, int]:
    col = ord(coord[0].upper()) - 65
    if col >= 8:
        col -= 1
    return col, size - int(coord[1:])


# ---------------------------------------------------------------------------
# 白名单清洗
# ---------------------------------------------------------------------------

def _allowed_coords(setup_sgf: str, size: int, answer: str,
                    groups: list[list[str]]) -> set[str]:
    """证据白名单 = 题面棋子坐标 ∪ 正解 ∪ 各变化坐标。"""
    allowed: set[str] = set()
    for mm in re.finditer(r"\[([a-zA-Z][a-zA-Z])\]", setup_sgf or ""):
        cd = sgf_to_coord(mm.group(1), size)
        if cd:
            allowed.add(cd.upper())
    if answer:
        allowed.add(answer.upper())
    for g in groups:
        for m in g:
            if m:
                allowed.add(str(m).upper())
    return allowed


def sanitize_segments(segments: list, allowed: set[str]) -> list[dict]:
    """清洗 LLM 分段：variation 每步 [色, 坐标]，坐标白名单校验；
    空段（无文字且无变化）删除。"""
    out: list[dict] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "").strip()
        var = seg.get("variation")
        kept: list[list[str]] = []
        if isinstance(var, list):
            for step in var:
                if (isinstance(step, list) and len(step) == 2
                        and step[0] in ("B", "W")
                        and isinstance(step[1], str)
                        and step[1].upper() in allowed):
                    kept.append([step[0], step[1].upper()])
        if not text and not kept:
            continue
        out.append({"text": text, "variation": kept})
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _get_cached(problem_id: str, db_path=None) -> dict | None:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT content, model, cost FROM explanations"
            " WHERE review_id=? AND move_number=? AND kind=?",
            (problem_id, MOVE_NUMBER, CACHE_KIND),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        content = json.loads(row["content"])
    except (TypeError, json.JSONDecodeError):
        return None
    return {"content": content, "model": row["model"] or "",
            "cost": float(row["cost"] or 0.0)}


def _save(problem_id: str, content: dict, model: str, cost: float,
          db_path=None) -> None:
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO explanations"
            " (review_id, move_number, kind, content, model, cost, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (problem_id, MOVE_NUMBER, CACHE_KIND,
             json.dumps(content, ensure_ascii=False), model, float(cost or 0.0),
             store.utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def explain_problem(problem_id: str, db_path=None) -> dict:
    """生成练习深度讲解（同步讲棋分段）。"""
    problem = store.get_problem(problem_id, db_path)
    if problem is None:
        raise ProblemNotFoundError(f"题目不存在: {problem_id}")

    cached = _get_cached(problem_id, db_path)
    if cached is not None:
        return {"kind": CACHE_KIND, "problem_id": problem_id, **cached}

    from ...common.settings import get_settings
    limit = float(get_settings().get("budget", {}).get("token_limit_month", 30) or 0)
    if month_cost(db_path=db_path) >= limit:
        raise BudgetExceededError("本月预算已用尽")

    branches = store.branches_json(problem)
    solver = str(branches.get("solver") or "B").upper()
    opp = "W" if solver == "B" else "B"
    answer = (branches.get("answer") or {}).get("coord") or problem.get("answer")
    answer_wr = (branches.get("answer") or {}).get("winrate")
    pv = list((branches.get("answer") or {}).get("pv") or [])
    setup = problem["setup_sgf"]
    size, stones = parse_setup(setup)
    allow = region_of(size, stones, extra=[answer] if answer else None)
    if not allow:
        return _fallback(problem_id, problem, db_path)

    executable, model, cfg = _resolve_paths()
    engine = KataGoEngine(executable, model, cfg, analysis_threads=1)
    first = [["B", "pass"]] if solver == "W" else [["W", "pass"]]

    def run_query(seq: list[list[str]]) -> dict:
        q = _game_to_query(setup, "fast", [len(seq)])
        q["moves"] = seq
        q["allowMoves"] = [
            {"player": "B", "moves": allow, "untilDepth": 100},
            {"player": "W", "moves": allow, "untilDepth": 100},
        ]
        q["includeOwnership"] = True
        return engine.query(q) or {}

    try:
        engine.start()
        # 正解变化（跳过与 answer 重复的首手；seq 去重防 Illegal move）
        eff_pv = [m for i, m in enumerate(pv[:6]) if not (i == 0 and m == answer)]
        occupied = {_xy_to_gtp(x, y, size) for _, x, y in stones}
        seq: list[list[str]] = [first[0]]
        c = solver
        for m in eff_pv:
            mu = str(m).upper()
            if mu and mu not in occupied:
                seq.append([c, mu])
                occupied.add(mu)
                c = "W" if c == "B" else "B"
        r_solve = run_query(seq)

        # 脱先演示：先手方 pass → 对手最佳应对
        seq_pass = first + [[solver, "pass"]]
        r_pass = run_query(seq_pass)
    finally:
        engine.stop()

    # ---- 证据组装 ----
    infos_s = (r_solve or {}).get("moveInfos") or []
    best_res = next((x for x in infos_s if x.get("order") == 0), None)
    res_pv = list(best_res.get("pv") or [])[:5] if best_res else []
    res_wr = (r_solve or {}).get("rootInfo", {}).get("winrate")

    infos_p = (r_pass or {}).get("moveInfos") or []
    best_opp = next((x for x in infos_p if x.get("order") == 0), None)
    opp_pv = list(best_opp.get("pv") or [])[:5] if best_opp else []

    resist_seq: list[list[str]] = []
    c2 = opp
    for m in res_pv:
        mu = str(m).upper()
        resist_seq.append([c2, mu])
        c2 = "W" if c2 == "B" else "B"
    tenuki_seq: list[list[str]] = []
    c3 = opp
    for m in opp_pv:
        mu = str(m).upper()
        tenuki_seq.append([c3, mu])
        c3 = "W" if c3 == "B" else "B"

    ax, ay = _coord_xy(answer, size) if answer else (0, 0)
    own_solve = own_grid((r_solve or {}).get("ownership"), size, ax, ay)
    own_pass = own_grid((r_pass or {}).get("ownership"), size, ax, ay)
    dups = _dup_flags(eff_pv, [m for _, m in resist_seq], [m for _, m in tenuki_seq])
    ko_line = (
        f"注意：变化中坐标 {','.join(sorted(dups))} 被重复落子"
        f"（同一坐标先后被双方下过），这是打劫的典型特征。"
        if dups else "变化中无重复落子。"
    )
    level = "15K"

    messages = prompts.build_problem_explain_messages(
        size=size,
        goal=problem.get("goal") or "死活",
        solver=solver,
        answer=answer or "",
        answer_wr=answer_wr,
        board_grid=board_grid(size, stones),
        own_solve=own_solve,
        resist_line=_seq_line(resist_seq),
        res_wr=res_wr,
        ko_line=ko_line,
        tenuki_line=_seq_line(tenuki_seq),
        own_pass=own_pass,
        level=level,
    )
    result = get_client().chat_json(
        messages, prompts.PROBLEM_EXPLAIN_SCHEMA,
        kind=CACHE_KIND, db_path=db_path, max_tokens=MAX_TOKENS)

    allowed = _allowed_coords(
        setup, size, answer or "",
        [eff_pv, [m for _, m in resist_seq], [m for _, m in tenuki_seq]])
    content = dict(result.data)
    content["segments"] = sanitize_segments(content.get("segments"), allowed)
    # 兜底：LLM 全部段落未给变化时，自动按「正解+变化」补一段
    if content["segments"] and not any(s.get("variation") for s in content["segments"]):
        seq: list[list[str]] = []
        if answer:
            seq.append([solver, str(answer).upper()])
        c2 = "W" if solver == "B" else "B"
        for m in eff_pv:
            mu = str(m).upper()
            if mu == str(answer or "").upper():
                continue
            seq.append([c2, mu])
            c2 = "W" if c2 == "B" else "B"
        if seq:
            content["segments"].append(
                {"text": "正解变化演示（KataGo 推演）：", "variation": seq[:8]}
            )
    for key, fallback in (("result_type", "普通"), ("can_tenuki", "不适用"),
                          ("takeaway", "先看急所，再想脱先。")):
        content.setdefault(key, fallback)

    _save(problem_id, content, result.model, result.cost, db_path)
    return {
        "kind": CACHE_KIND,
        "problem_id": problem_id,
        "content": content,
        "model": result.model,
        "cost": round(result.cost, 6),
    }


def _fallback(problem_id: str, problem: dict, db_path=None) -> dict:
    """题面无法构造局部区域时的降级响应（不扣费）。"""
    content = {
        "result_type": "普通",
        "can_tenuki": "不适用",
        "segments": [{"text": problem.get("explanation") or "本题暂无深度讲解。",
                      "variation": []}],
        "takeaway": "",
    }
    return {"kind": CACHE_KIND, "problem_id": problem_id, "content": content,
            "model": "fallback", "cost": 0.0}
