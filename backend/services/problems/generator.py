"""错题生成器（窗口3）。

输入 review_id + 主题列表 + 目标级位，从用户对局的失误中生成针对性题目：

1. 取 ``moves`` 表中 category ∈ {blunder, question} 的手（按 |delta| 降序）；
2. 重放该手之前的局面 → 以该手与 KataGo 推荐点**双中心**裁剪局部
   （各半径 ``crop_radius``，取并集并保留完整棋串）→ 构造题面 SGF
   （轮到失误方行棋，即"此时怎么下最好"）；
3. 调 ``verify_position`` 一次调用批量验证候选点：
   - 候选 = 失误点 + 半径 1 的空邻点 + 复盘记录中的 KataGo 推荐点
     （best_coord），life_death/capturing_race 额外加 "pass" 检验紧迫性；
   - **验题规则（契约 §4.3）**：正解（候选点中胜率最高）> 0.95 且
     次优点 < 0.3，否则丢弃；死活/对杀题还要求 pass 后胜率 <
     ``urgency_max_winrate``（即"必须现在处理"，否则不算死活/对杀题）；
4. 主题归类（务实策略，见 utils.classify_theme 注释）：
   - life_death / capturing_race 用 fine 档深度验证；
   - endgame：对局末段（手数 > 80%）；middle：其余选点失误；
5. 难度分级：正解与次优点的胜率差（gap）分 5 档，映射到级位区间
   （K 为负、D 为正，见 utils.difficulty_from_gap）；
6. 题面 SGF 为 AB/AW 摆子局（白先时前置一手黑 pass 修正奇偶，
   见 utils.setup_sgf 注释）；题 id 为题面哈希 → 重复生成幂等；
7. hint / verdict 用规则模板；explanation 先置 null，待窗口2 就绪后由
   ``scripts/backfill_explanations.py`` 经 coach/ask 批量补写。

性能说明：每题一次 verify_position 调用（引擎常驻进程内批量完成所有
候选点）；目标 ≤ 30 秒/题（fine 档），实测记录见 docs/plan.md。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ...common import db as db_mod
from ...common import sgf_io
from ...common.settings import get_settings
from ..engine.verify import VerifyResult, verify_position
from . import store, utils


class ReviewNotFoundError(RuntimeError):
    """复盘记录或 moves 数据不存在（路由层转 HTTP 404）。"""


class SgfFileNotFoundError(RuntimeError):
    """复盘对应的 SGF 文件缺失（路由层转 HTTP 404/502）。"""


def _problems_cfg() -> dict:
    cfg = get_settings().get("problems", {})
    if not isinstance(cfg, dict):
        return {}
    return cfg


# ---------------------------------------------------------------------------
# 候选点构造
# ---------------------------------------------------------------------------


def build_candidates(
    mistake_coord: str,
    position: utils.Position,
    size: int,
    best_coord: Optional[str],
    candidate_radius: int,
    max_candidates: int,
    urgent: bool,
) -> list[str]:
    """构造待验证候选点：失误点 + 半径 candidate_radius 的空点 + best_coord。

    urgent（死活/对杀）时加 "pass" 用于检验局面紧迫性。
    返回去重后的界面坐标列表（pass 记 "pass"）。
    """
    cands: list[str] = []
    seen: set[str] = set()

    def add(c: Optional[str]) -> None:
        if not c:
            return
        n = utils.normalize_coord(c)
        if n == "pass":
            return
        if n in seen:
            return
        # 已占用的点过滤掉（verify 也会报错，这里提前减少无谓查询）
        try:
            x, y = utils.coord_to_xy(n)
        except ValueError:
            return
        if (x, y) in position:
            return
        seen.add(n)
        cands.append(n)

    add(mistake_coord)
    try:
        cx, cy = utils.coord_to_xy(mistake_coord)
        for x in range(max(0, cx - candidate_radius),
                       min(size - 1, cx + candidate_radius) + 1):
            for y in range(max(0, cy - candidate_radius),
                           min(size - 1, cy + candidate_radius) + 1):
                add(utils.xy_to_coord(x, y, size))
    except ValueError:
        pass
    add(best_coord)
    if len(cands) > max_candidates:
        # 优先保留：best_coord > 失误点 > 邻点（去重）
        keep: list[str] = []
        for pref in (best_coord, mistake_coord):
            n = utils.normalize_coord(pref) if pref else "pass"
            if n in cands and n not in keep:
                keep.append(n)
        for c in cands:
            if c not in keep and len(keep) < max_candidates:
                keep.append(c)
        cands = keep
    if urgent and "pass" not in cands:
        cands.append("pass")  # 紧迫性检验点始终保留
    return cands


# ---------------------------------------------------------------------------
# 验证结果 → 题目行
# ---------------------------------------------------------------------------


def _result_dict(r: VerifyResult) -> dict:
    return {
        "coord": r.coord,
        "winrate": r.winrate,
        "score_lead": r.score_lead,
        "visits": r.visits,
        "pv": list(r.pv),
        "best_coord": r.best_coord,
        "error": r.error,
    }


def verify_candidates(
    setup_sgf: str,
    candidates: list[str],
    profile: str,
    urgent_max_winrate: float,
) -> tuple[Optional[VerifyResult], Optional[VerifyResult], list[VerifyResult], bool]:
    """一次 verify_position 调用验证全部候选点。

    返回 (best, second, valid_results, pass_ok)：
    - best / second：去掉 pass 与 error 后胜率最高/次高；
    - pass_ok：是否满足紧迫性条件（无 pass 候选时为 True，
      即非死活/对杀题不检验紧迫性）。
    """
    results = verify_position(setup_sgf, candidates, profile=profile)
    valid = [
        r for r in results
        if r.error is None and r.coord != "pass" and r.winrate is not None
    ]
    valid.sort(key=lambda r: r.winrate or 0.0, reverse=True)
    best = valid[0] if valid else None
    second = valid[1] if len(valid) > 1 else None
    pass_ok = True
    if "pass" in candidates:
        pass_r = next(
            (r for r in results
             if r.coord == "pass" and r.error is None and r.winrate is not None),
            None,
        )
        pass_ok = pass_r is not None and (pass_r.winrate or 1.0) < urgent_max_winrate
    return best, second, results, pass_ok


def _problem_row(
    review_id: str,
    theme: str,
    setup_sgf: str,
    solver: str,
    center_coord: str,
    best: VerifyResult,
    second: Optional[VerifyResult],
    results: list[VerifyResult],
    profile: str,
    target_rank: int,
    sgf_size: int,
) -> dict:
    gap = (best.winrate or 0.0) - (second.winrate if second and second.winrate is not None else 0.0)
    _, rank_min, rank_max = utils.difficulty_from_gap(gap, target_rank)
    branches = {
        "solver": solver,
        "answer": _result_dict(best),
        "candidates": [_result_dict(r) for r in results],
        "profile": profile,
        "verified_at": store.utcnow(),
    }
    return {
        "id": utils.problem_id(setup_sgf, theme),
        "source": "generated",
        "review_id": review_id,
        "theme": theme,
        "rank_min": rank_min,
        "rank_max": rank_max,
        "setup_sgf": setup_sgf,
        "answer": best.coord,
        "branches": json.dumps(branches, ensure_ascii=False),
        "verdict": utils.verdict_text(
            theme, solver, best.coord,
            best.winrate or 0.0,
            second.coord if second else None,
            second.winrate if second else None,
        ),
        "hint": utils.hint_text(theme, solver, center_coord, sgf_size),
        "explanation": None,  # TODO(窗口2 就绪后): 经 coach/ask 批量补写
        "status": "active",
        "created_at": store.utcnow(),
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def generate(
    review_id: str,
    themes: Optional[list[str]] = None,
    max_problems: int = 6,
    target_rank: int = -5,
    db_path=None,
) -> dict:
    """按契约 §4.3 POST /problems/generate 生成错题。

    返回 {"problems": [brief...], "failed": n}；同名题目重复生成幂等
    （题目 id 为题面哈希，INSERT OR IGNORE）。
    """
    cfg = _problems_cfg()
    crop_radius = int(cfg.get("crop_radius", 4))
    candidate_radius = int(cfg.get("candidate_radius", 1))
    max_candidates = int(cfg.get("max_candidates", 12))
    verify_profile = cfg.get("verify_profile", {}) or {}
    urgent_max = float(cfg.get("urgency_max_winrate", 0.3))

    conn = db_mod.connect(db_path)
    try:
        rev = conn.execute(
            "SELECT id, sgf_path, board_size, black, white, status"
            " FROM reviews WHERE id=?", (review_id,)
        ).fetchone()
        moves = conn.execute(
            "SELECT move_number, color, coord, winrate, score_lead, visits,"
            " category, delta, best_coord, pv"
            " FROM moves WHERE review_id=? ORDER BY move_number",
            (review_id,),
        ).fetchall()
    finally:
        conn.close()
    if rev is None or not moves:
        raise ReviewNotFoundError(f"复盘记录或分析数据不存在: {review_id}")

    sgf_path = Path(rev["sgf_path"])
    if not sgf_path.exists():
        raise SgfFileNotFoundError(f"SGF 文件缺失: {sgf_path}")
    sgf_text = sgf_path.read_text(encoding="utf-8")
    parsed = sgf_io.parse_sgf(sgf_text)
    size = int(parsed.board_size)
    all_moves = parsed.moves
    total = len(all_moves)

    # 候选失误手：blunder/question，按 |delta| 降序
    cand_moves = sorted(
        [m for m in moves if m["category"] in ("blunder", "question")],
        key=lambda m: abs(m["delta"]) if m["delta"] is not None else -1.0,
        reverse=True,
    )
    allowed = {t for t in (themes or []) if t in utils.VALID_THEMES} or None

    problems: list[dict] = []
    failed = 0
    for m in cand_moves:
        if len(problems) >= int(max_problems):
            break
        move_no = int(m["move_number"])
        idx = move_no - 1
        if idx >= total:
            failed += 1
            continue
        color, coord = all_moves[idx]
        if not coord:
            continue  # pass 手不生成
        try:
            position = utils.replay_position(all_moves[:idx], size)
            mistake_xy = utils.coord_to_xy(coord)
            # 双中心裁剪：以失误点与 KataGo 推荐点各取半径 crop_radius 的
            # 局部，取并集（保留完整棋串）。覆盖两类失误：
            # - 局部失误：推荐点就在失误点旁，并集 ≈ 单中心裁剪；
            # - 脱先型失误：推荐点远离失误点，两处局部都纳入题面。
            kept = utils.crop_stones(position, mistake_xy, crop_radius, size)
            best_xy = None
            if m["best_coord"]:
                try:
                    best_xy = utils.coord_to_xy(m["best_coord"])
                except ValueError:
                    best_xy = None
            if best_xy is not None and best_xy != mistake_xy:
                kept.update(
                    utils.crop_stones(position, best_xy, crop_radius, size)
                )
            theme = utils.classify_theme(
                kept, position, size, move_no, total,
                endgame_fraction=float(cfg.get("endgame_move_fraction", 0.8)),
            )
        except ValueError:
            failed += 1
            continue
        if allowed is not None and theme not in allowed:
            continue  # 主题被过滤，不计入 failed（不满足请求主题）

        urgent = theme in ("life_death", "capturing_race")
        profile = verify_profile.get(theme) or ("fine" if urgent else "standard")
        setup_sgf = utils.setup_sgf(kept, color, size)
        candidates = build_candidates(
            coord, position, size, m["best_coord"],
            candidate_radius, max_candidates, urgent,
        )
        if not candidates:
            failed += 1
            continue
        # 引擎异常向上传播（路由层转 502），不静默吞掉
        best, second, results, pass_ok = verify_candidates(
            setup_sgf, candidates, profile, urgent_max
        )
        if best is None or (best.winrate or 0.0) <= 0.95:
            failed += 1
            continue
        if second is not None and (second.winrate or 0.0) >= 0.3:
            failed += 1
            continue
        if not pass_ok:
            failed += 1  # 死活/对杀题不紧迫：pass 后胜率仍高 → 丢弃
            continue

        row = _problem_row(
            review_id, theme, setup_sgf, color, coord,
            best, second, results, profile, int(target_rank), size,
        )
        store.insert_problem(row, db_path)
        problems.append({
            "id": row["id"],
            "theme": row["theme"],
            "setup_sgf": row["setup_sgf"],
            "rank_min": row["rank_min"],
            "rank_max": row["rank_max"],
            "hint": row["hint"],
        })
    return {"problems": problems, "failed": failed}


# ---------------------------------------------------------------------------
# 单题截取（复盘讲解面板「收录本题」）：把某一手所在局部截为题目
# ---------------------------------------------------------------------------

class MoveNotFoundError(RuntimeError):
    """复盘数据中不存在指定手数。"""


class VerifyFailedError(RuntimeError):
    """局部验题未通过（正解不唯一或不紧迫），无法收录。"""


def extract_one(review_id: str, move_number: int, db_path=None) -> dict:
    """把复盘第 move_number 手所在局部截取为题目（复用 generate 的验题管线）。

    题面 = 该手前局面以「本手 + KataGo 推荐点」双中心裁剪的局部；
    验题通过（正解 ≥0.95 / 次优 ≤0.3 / 死活对杀紧迫）才入库，
    否则抛 VerifyFailedError。同题面重复收录幂等（created=False）。
    """
    cfg = _problems_cfg()
    crop_radius = int(cfg.get("crop_radius", 4))
    candidate_radius = int(cfg.get("candidate_radius", 1))
    max_candidates = int(cfg.get("max_candidates", 12))
    verify_profile = cfg.get("verify_profile", {}) or {}
    urgent_max = float(cfg.get("urgency_max_winrate", 0.3))

    conn = db_mod.connect(db_path)
    try:
        rev = conn.execute(
            "SELECT id, sgf_path, board_size FROM reviews WHERE id=?",
            (review_id,),
        ).fetchone()
        row = conn.execute(
            "SELECT move_number, color, coord, best_coord FROM moves"
            " WHERE review_id=? AND move_number=?",
            (review_id, move_number),
        ).fetchone()
    finally:
        conn.close()
    if rev is None or row is None:
        raise MoveNotFoundError(f"复盘记录或第 {move_number} 手不存在: {review_id}")

    sgf_path = Path(rev["sgf_path"])
    if not sgf_path.exists():
        raise SgfFileNotFoundError(f"SGF 文件缺失: {sgf_path}")
    sgf_text = sgf_path.read_text(encoding="utf-8")
    parsed = sgf_io.parse_sgf(sgf_text)
    size = int(parsed.board_size)
    all_moves = parsed.moves
    total = len(all_moves)
    idx = move_number - 1
    if idx < 0 or idx >= total:
        raise MoveNotFoundError(f"第 {move_number} 手超出棋谱范围")

    color, coord = all_moves[idx]
    if not coord:
        raise VerifyFailedError("该手是停一手，无法截取为题目。")
    best_coord = row["best_coord"]

    try:
        position = utils.replay_position(all_moves[:idx], size)
        center_xy = utils.coord_to_xy(coord)
        kept = utils.crop_stones(position, center_xy, crop_radius, size)
        best_xy = None
        if best_coord:
            try:
                best_xy = utils.coord_to_xy(best_coord)
            except ValueError:
                best_xy = None
        if best_xy is not None and best_xy != center_xy:
            kept.update(utils.crop_stones(position, best_xy, crop_radius, size))
        theme = utils.classify_theme(
            kept, position, size, move_number, total,
            endgame_fraction=float(cfg.get("endgame_move_fraction", 0.8)),
        )
    except ValueError as exc:
        raise VerifyFailedError(f"局部裁剪失败: {exc}") from exc

    urgent = theme in ("life_death", "capturing_race")
    profile = verify_profile.get(theme) or ("fine" if urgent else "standard")
    setup_sgf = utils.setup_sgf(kept, color, size)
    candidates = build_candidates(
        coord, position, size, best_coord,
        candidate_radius, max_candidates, urgent,
    )
    if not candidates:
        raise VerifyFailedError("没有可验证的候选点。")
    best, second, results, pass_ok = verify_candidates(
        setup_sgf, candidates, profile, urgent_max
    )
    if best is None or (best.winrate or 0.0) <= 0.95:
        raise VerifyFailedError("正解胜率不足，无法收录（正解不唯一）。")
    if second is not None and (second.winrate or 0.0) >= 0.3:
        raise VerifyFailedError("存在胜率接近的次优解，无法收录。")
    if not pass_ok:
        raise VerifyFailedError("该局部不紧迫（脱先也无碍），无法收录。")

    problem_row = _problem_row(
        review_id, theme, setup_sgf, color, coord,
        best, second, results, profile, -5, size,
    )
    existed = store.get_problem(problem_row["id"], db_path) is not None
    store.insert_problem(problem_row, db_path)
    return {
        "problem_id": problem_row["id"],
        "theme": theme,
        "answer": problem_row["answer"],
        "setup_sgf": setup_sgf,
        "created": not existed,
    }
