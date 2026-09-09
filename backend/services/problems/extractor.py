"""局部死活提取器（M3 核心管线）。

从任意棋局扫描未定型区域（"高变化量"信号）→ 死活/对杀识别 → 多分支验题
→ 入库。与 v1 错题生成器（generator.py）共享验题规则与入库路径：

1. 输入：sgf_text 或 review_id（仅 sgf_text 时先走复盘管线
   ``review.service.get_service().submit`` + 轮询 status 拿 review_id，
   复用缓存与线程模型）；
2. 从 moves 表读每手数据（winrate/delta/best_coord/pv/category），扫描
   信号中心：
   a. category ∈ {blunder, question}（|delta| 大的失误手）；
   b. 相邻两手 delta 符号反转且幅值均 ≥ ``extract_signal_threshold``
      （局面剧烈动荡）；
   c. 终局附近（move_number > ``endgame_move_fraction`` × 总手数）仍存在
      |delta| ≥ ``endgame_swing_threshold`` 的 winrate 波动；
   信号按强度（|delta|；反转信号取 |d1|+|d2|）降序、按手号去重后取前
   max_problems 个中心；
3. 对每个中心：重放该手之前的局面 → 以中心点与 KataGo 推荐点双中心裁剪
   （半径 ``crop_radius``，保留完整棋串，复用 utils.crop_stones）→
   utils.classify_theme 归类 → 候选点 = 中心 + 半径 1 空点 + best_coord
   （死活/对杀加 "pass" 检验紧迫性）→ verify_position 一次调用批量验题
   （0.95/0.3 规则同 v1，life_death/capturing_race 用 fine 档 + pass
   紧迫性）→ 通过则 store.add_problem 入库（题 id = 题面哈希，
   INSERT OR IGNORE 幂等）；
4. 返回 ``{"extracted": [brief...], "failed": n, "skipped": n}``：
   - extracted：本次新入库的题（重复题面不重复计数）；
   - failed：验题未通过（正解 ≤ 0.95 / 次优 ≥ 0.3 / 不紧迫）的中心数；
   - skipped：未验题即跳过的信号数（超出前 N 中心、pass 等无效中心、
     重复题面）。

性能：每中心一次 verify_position（候选点批量）；顺序处理，目标 9 路
e2e ≤ 3 分钟。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from ...common import db as db_mod
from ...common import sgf_io
from ...common.settings import get_settings
from . import store, utils
from .generator import (
    ReviewNotFoundError,
    SgfFileNotFoundError,
    _problem_row,
    _problems_cfg,
    build_candidates,
    verify_candidates,
)

__all__ = [
    "ReviewFailedError",
    "ReviewNotFoundError",
    "ReviewTimeoutError",
    "SgfFileNotFoundError",
    "extract",
    "scan_signals",
]


class ReviewFailedError(RuntimeError):
    """复盘分析失败（路由层转 HTTP 502）。"""


class ReviewTimeoutError(RuntimeError):
    """等待复盘完成超时（路由层转 HTTP 504）。"""


# ---------------------------------------------------------------------------
# 信号扫描（纯函数，便于单元测试）
# ---------------------------------------------------------------------------


def scan_signals(
    moves: list[dict],
    total_moves: int,
    cfg: dict,
) -> list[dict]:
    """从每手数据扫描"未定型"信号中心。

    ``moves`` 为 moves 表行（dict，含 move_number/category/delta）。
    三类信号（见模块 docstring）按手号去重（保留强度最大者），
    返回按强度降序的 ``[{"row", "strength", "kind"}, ...]``：
    - kind="blunder"/"question"：失误手，强度 = |delta|；
    - kind="swing"：相邻两手 delta 符号反转，强度 = |d1| + |d2|；
    - kind="endgame"：终局附近 winrate 波动，强度 = |delta|。
    """
    signal_threshold = float(cfg.get("extract_signal_threshold", 0.06))
    swing_threshold = float(cfg.get("endgame_swing_threshold", 0.12))
    endgame_fraction = float(cfg.get("endgame_move_fraction", 0.8))

    centers: dict[int, dict] = {}

    def add(row: dict, strength: float, kind: str) -> None:
        if strength is None or strength <= 0:
            return
        mn = int(row["move_number"])
        old = centers.get(mn)
        if old is None or strength > old["strength"]:
            centers[mn] = {"row": row, "strength": strength, "kind": kind}

    rows = list(moves)
    for i, m in enumerate(rows):
        d = m.get("delta")
        # a) 失误手（blunder/question）
        if m.get("category") in ("blunder", "question") and d is not None:
            add(m, abs(d), str(m["category"]))
        # b) 相邻两手 delta 符号反转且幅值大
        if i > 0:
            prev = rows[i - 1]
            d0, d1 = prev.get("delta"), d
            if (
                d0 is not None and d1 is not None
                and d0 * d1 < 0
                and abs(d0) >= signal_threshold
                and abs(d1) >= signal_threshold
            ):
                add(m, abs(d0) + abs(d1), "swing")
        # c) 终局附近仍有 winrate 波动（该手前后胜率变化即波动）
        if (
            total_moves > 0
            and int(m["move_number"]) > endgame_fraction * total_moves
            and d is not None
            and abs(d) >= swing_threshold
        ):
            add(m, abs(d), "endgame")
    return sorted(centers.values(), key=lambda s: -s["strength"])


# ---------------------------------------------------------------------------
# 复盘管线复用（sgf_text → review_id）
# ---------------------------------------------------------------------------


def _submit_and_wait(sgf_text: str, db_path, timeout: float, profile: str) -> str:
    """提交复盘并轮询到 done，返回 review_id（复用缓存与线程模型）。"""
    from ..review import service as review_service

    svc = review_service.get_service()
    svc.start()
    review_id = svc.submit(sgf_text, profile)
    deadline = time.time() + timeout
    while True:
        conn = db_mod.connect(db_path)
        try:
            row = conn.execute(
                "SELECT status, error FROM reviews WHERE id=?", (review_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ReviewNotFoundError(f"复盘记录不存在: {review_id}")
        if row["status"] == "done":
            return review_id
        if row["status"] == "failed":
            raise ReviewFailedError(
                f"复盘分析失败: {review_id}（{row['error'] or '未知原因'}）"
            )
        if time.time() >= deadline:
            raise ReviewTimeoutError(
                f"等待复盘完成超时（{timeout:.0f}s）: {review_id}"
            )
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def extract(
    sgf_text: Optional[str] = None,
    review_id: Optional[str] = None,
    max_problems: int = 5,
    target_rank: int = -5,
    review_profile: Optional[str] = None,
    db_path=None,
) -> dict:
    """从任意棋局扫描未定型区域成题（契约 §4.3 POST /problems/extract）。

    返回 ``{"extracted": [brief...], "failed": n, "skipped": n}``；语义见
    模块 docstring。引擎异常向上传播（路由层转 502），不静默吞掉。
    ``review_profile``：仅 sgf_text 路径走复盘时使用的档位；None 用
    ``review.profile`` 配置（fast 档噪声大，生产提取建议 standard）。
    """
    cfg = _problems_cfg()
    crop_radius = int(cfg.get("crop_radius", 4))
    candidate_radius = int(cfg.get("candidate_radius", 1))
    max_candidates = int(cfg.get("max_candidates", 12))
    verify_profile = cfg.get("verify_profile", {}) or {}
    urgent_max = float(cfg.get("urgency_max_winrate", 0.3))
    endgame_fraction = float(cfg.get("endgame_move_fraction", 0.8))
    max_problems = max(1, int(max_problems))

    if not (review_id or "").strip():
        if not (sgf_text or "").strip():
            raise ValueError("sgf_text 与 review_id 至少提供一个")
        rp = review_profile or str(
            (get_settings().get("review") or {}).get("profile", "fast")
        )
        review_id = _submit_and_wait(
            sgf_text,
            db_path,
            float(cfg.get("extract_review_timeout", 900)),
            rp,
        )

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
    parsed = sgf_io.parse_sgf(sgf_path.read_text(encoding="utf-8"))
    size = int(parsed.board_size)
    all_moves = parsed.moves
    total = len(all_moves)
    move_rows = [dict(m) for m in moves]

    # 信号中心：验题通过率有限（0.95/0.3 规则严格 + 分析随机性），
    # 信号池取 max_problems×3（至少 8）个中心，验题通过达到
    # max_problems 即停；池外与达标后跳过的信号计入 skipped。
    signals = scan_signals(move_rows, total, cfg)
    pool_size = max(int(max_problems) * 3, 8)
    centers = signals[:pool_size]
    skipped = len(signals) - len(centers)

    extracted: list[dict] = []
    failed = 0
    seen_ids: set[str] = set()
    for sig in centers:
        if len(extracted) >= int(max_problems):
            skipped += 1  # 已达标，剩余信号不再验题
            continue
        m = sig["row"]
        move_no = int(m["move_number"])
        idx = move_no - 1
        if idx >= total:
            skipped += 1
            continue
        color, coord = all_moves[idx]
        if not coord:
            skipped += 1  # pass 手不作为中心
            continue
        try:
            position = utils.replay_position(all_moves[:idx], size)
            center_xy = utils.coord_to_xy(coord)
            kept = utils.crop_stones(position, center_xy, crop_radius, size)
            best_xy = None
            if m["best_coord"]:
                try:
                    best_xy = utils.coord_to_xy(m["best_coord"])
                except ValueError:
                    best_xy = None
            if best_xy is not None and best_xy != center_xy:
                kept.update(
                    utils.crop_stones(position, best_xy, crop_radius, size)
                )
            theme = utils.classify_theme(
                kept, position, size, move_no, total, endgame_fraction
            )
        except ValueError:
            skipped += 1
            continue

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
        if row["id"] in seen_ids:
            skipped += 1  # 本次运行内重复题面
            continue
        seen_ids.add(row["id"])
        inserted = store.insert_problem(row, db_path)
        brief = {
            "id": row["id"],
            "theme": row["theme"],
            "setup_sgf": row["setup_sgf"],
            "rank_min": row["rank_min"],
            "rank_max": row["rank_max"],
            "hint": row["hint"],
        }
        if inserted:
            extracted.append(brief)
        else:
            skipped += 1  # 库中已有同题（幂等，不重复计数）
    return {"extracted": extracted, "failed": failed, "skipped": skipped}
