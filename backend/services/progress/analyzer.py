"""棋力画像引擎（成长视图）。

纯 KataGo 复盘数据（moves 表）→ 画像特征，不调用 LLM：

- 全局：平均每手失误、坏手/疑问手/好手率、平均手数；
- 分阶段：布局（前 20%）/ 中盘 / 官子（后 20%）各自的平均损失与坏手占比；
- 失误类型粗分：方向性失误（一选离本手远）/ 计算失误（高复杂度局面下坏手）；
- 趋势：最近半程 vs 前半程的平均损失对比；
- 棋力区间：按平均每手失误的启发式映射（参考区间，样本越多越准）。

设计原则：只做统计与规则，不假装精确段位；所有数字都能追溯到
KataGo 的 winrate 差（当前行棋方视角），不引入新引擎调用。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ...common import db
from ..problems.utils import coord_to_xy

# 阶段划分（按手数占比）
LAYOUT_FRAC = 0.2
ENDGAME_FRAC = 0.8

# 计算失误判定：局面复杂度（score_stdev）超过该值时出现的坏手
COMPLEX_STDEV = 3.0

# 方向失误判定：坏手与其一选坐标的曼哈顿距离超过该值（跳过了局部急所）
DIRECTION_DIST = 3

# 平均每手失误（胜率差绝对值）→ 参考棋力区间（启发式标定）
_RANK_LADDER: list[tuple[float, str]] = [
    (0.008, "约 1 级 ~ 1 段"),
    (0.018, "约 3 级 ~ 1 级"),
    (0.04, "约 6 级 ~ 3 级"),
    (0.08, "约 10 级 ~ 6 级"),
    (0.15, "约 15 级 ~ 10 级"),
]


def _phase(move_no: int, total: int) -> str:
    if total <= 0:
        return "middle"
    frac = move_no / total
    if frac < LAYOUT_FRAC:
        return "layout"
    if frac >= ENDGAME_FRAC:
        return "endgame"
    return "middle"


def _dist(a: Optional[str], b: Optional[str]) -> Optional[int]:
    try:
        x1, y1 = coord_to_xy(str(a))
        x2, y2 = coord_to_xy(str(b))
    except (ValueError, TypeError):
        return None
    return abs(x1 - x2) + abs(y1 - y2)


def build_insight(games: list[dict], db_path=None) -> dict:
    """games：每个元素 = {review_id, moves_count, blunders, questions, good,
    created_at, move_rows: [...]}；返回画像 dict。

    move_rows 为 moves 表行（含 move_number/delta/category/best_coord/
    coord/score_stdev）。行由调用方（service）按档案批量取。
    """
    n_games = len(games)
    if n_games == 0:
        return {"n_games": 0}

    total_moves = 0
    total_loss = 0.0
    blunders = questions = goods = 0
    phase_loss = {"layout": 0.0, "middle": 0.0, "endgame": 0.0}
    phase_moves = {"layout": 0, "middle": 0, "endgame": 0}
    phase_blunders = {"layout": 0, "middle": 0, "endgame": 0}
    calc_blunders = 0
    calc_total = 0
    direction_blunders = 0
    direction_total = 0

    # 趋势：按时间排序后分前后两半
    ordered = sorted(games, key=lambda g: g.get("created_at") or "")
    losses_per_game: list[tuple[float, int]] = []

    for g in games:
        rows = g.get("move_rows") or []
        g_loss = 0.0
        g_moves = 0
        for m in rows:
            total = g.get("moves_count") or (len(rows))
            if total <= 0:
                total = len(rows) or 1
            mn = int(m["move_number"] or 0)
            delta = m.get("delta")
            cat = m.get("category") or "normal"
            ph = _phase(mn, total)
            loss = abs(float(delta)) if delta is not None else 0.0
            total_moves += 1
            total_loss += loss
            g_loss += loss
            g_moves += 1
            phase_loss[ph] += loss
            phase_moves[ph] += 1
            if cat == "blunder":
                blunders += 1
                phase_blunders[ph] += 1
            elif cat == "question":
                questions += 1
            elif cat == "good":
                goods += 1
            # 计算失误：高复杂度局面中的坏手
            if m.get("score_stdev") is not None and float(m["score_stdev"]) >= COMPLEX_STDEV:
                calc_total += 1
                if cat == "blunder":
                    calc_blunders += 1
            # 方向失误：坏手且一选距离远
            if cat == "blunder" and m.get("best_coord"):
                d = _dist(str(m.get("best_coord")), str(m.get("coord")))
                if d is not None:
                    direction_total += 1
                    if d >= DIRECTION_DIST:
                        direction_blunders += 1
        losses_per_game.append((g_loss, max(g_moves, 1)))

    avg_loss = total_loss / max(total_moves, 1)

    # 趋势：时间前后两半的平均每手损失
    half = max(1, len(ordered) // 2)
    earlier = ordered[:half]
    recent = ordered[half:]

    def avg_loss_of(gs: list[dict]) -> Optional[float]:
        total_l = 0.0
        total_m = 0
        for g in gs:
            for m in (g.get("move_rows") or []):
                delta = m.get("delta")
                if delta is not None:
                    total_l += abs(float(delta))
                    total_m += 1
        return (total_l / total_m) if total_m else None

    earlier_loss = avg_loss_of(earlier)
    recent_loss = avg_loss_of(recent)
    trend = "flat"
    if earlier_loss is not None and recent_loss is not None:
        if recent_loss < earlier_loss * 0.8:
            trend = "improving"
        elif recent_loss > earlier_loss * 1.2:
            trend = "declining"

    # 最弱阶段：平均损失最高的阶段
    phase_avg = {
        ph: (phase_loss[ph] / phase_moves[ph]) if phase_moves[ph] else 0.0
        for ph in phase_loss
    }
    weakest_phase = max(phase_avg, key=phase_avg.get)

    rank = _estimate_rank(avg_loss)
    weakest_cn = {"layout": "布局", "middle": "中盘", "endgame": "官子"}[weakest_phase]
    reading_blunders = max(blunders - calc_blunders, 0)  # 简单局面的坏手≈读棋失误

    return {
        "n_games": n_games,
        "avg_moves": round(total_moves / n_games, 1),
        "avg_loss_per_move": round(avg_loss, 5),
        "blunders": blunders,
        "questions": questions,
        "good_moves": goods,
        "direction_errors": direction_blunders,
        "complexity_errors": calc_blunders,
        "reading_errors": reading_blunders,
        "weakest": weakest_cn,
        "weakest_loss": round(phase_avg[weakest_phase], 5),
        "rates": {
            "blunder": round(blunders / max(total_moves, 1), 4),
            "question": round(questions / max(total_moves, 1), 4),
            "good": round(goods / max(total_moves, 1), 4),
        },
        "phases": {
            ph: {
                "avg_loss": round(phase_avg[ph], 5),
                "moves": phase_moves[ph],
                "blunder_rate": round(
                    phase_blunders[ph] / max(phase_moves[ph], 1), 4
                ),
            }
            for ph in ("layout", "middle", "endgame")
        },
        "weakest_phase": weakest_cn,
        "calc_blunder_share": round(
            calc_blunders / max(calc_total, 1), 4
        ) if calc_total else None,
        "direction_blunder_share": round(
            direction_blunders / max(direction_total, 1), 4
        ) if direction_total else None,
        "trend": trend,
        "earlier_loss": round(earlier_loss, 5) if earlier_loss is not None else None,
        "recent_loss": round(recent_loss, 5) if recent_loss is not None else None,
        "rank_estimate": rank,
    }


def _estimate_rank(avg_loss: float) -> str:
    for threshold, label in _RANK_LADDER:
        if avg_loss < threshold:
            return label
    return "约 20 级 ~ 15 级（建议先做基础死活题）"


def sample_bad_moves(profile_id: str, limit: int = 6, db_path=None) -> list[dict]:
    """档案内典型的坏手样本（每阶段取平均损失最大的 2 手），供 LLM 建议参考。"""
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT m.review_id, m.move_number, m.coord, m.best_coord, m.delta,"
            " m.score_stdev, r.board_size,"
            " (SELECT COUNT(*) FROM moves m2 WHERE m2.review_id = m.review_id) AS n"
            " FROM moves m JOIN reviews r ON r.id = m.review_id"
            " WHERE r.profile_id = ? AND m.category = 'blunder'",
            (profile_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return []
    per_phase: dict[str, list[dict]] = {"layout": [], "middle": [], "endgame": []}
    for r in rows:
        ph = _phase(int(r["move_number"] or 0), int(r["n"] or 1))
        per_phase[ph].append({
            "phase": ph,
            "move_number": int(r["move_number"]),
            "coord": r["coord"],
            "best_coord": r["best_coord"],
            "delta": round(float(r["delta"]), 3) if r["delta"] is not None else None,
            "board_size": int(r["board_size"] or 19),
        })
    picked: list[dict] = []
    for ph in ("layout", "middle", "endgame"):
        lst = sorted(per_phase[ph], key=lambda x: abs(x["delta"] or 0), reverse=True)
        picked.extend(lst[:2])
        if len(picked) >= limit:
            break
    return picked[:limit]
