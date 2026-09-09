"""局面验证接口（窗口1 实现，供窗口3 题目系统复用）。

功能：给定局面 SGF + 候选着点列表，返回每个候选点**搜索后**的胜率与 PV。

实现：对每个候选点，构造"局面 + 候选着法"的查询（analysis 引擎协议），
分析候选着法落下后的局面（analyzeTurns=[n]，n=候选点序号），从响应中取：
- ``winrate``：候选方胜率 = 1 - rootInfo.winrate（rootInfo 为对手视角）；
- ``pv``：后续变化（rootInfo 对应局面 order=0 moveInfo 的 pv 前缀加候选点）；
- ``score_lead``：候选方目数领先 = -rootInfo.scoreLead；
- ``visits``：搜索量。

接口签名（写入 docs/architecture.md §4.3 附注，窗口3 按此调用）：

    from backend.services.engine.verify import verify_position

    verify_position(setup_sgf: str,
                    candidate_coords: list[str],      # 界面坐标，如 ["D15","E16"]
                    profile: str = "standard",
                    max_visits: int | None = None,
                    ) -> list[VerifyResult]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ...common import sgf_io
from ...common.settings import get_settings
from .analyze_sgf import _game_to_query, _resolve_paths
from .engine import EngineError, KataGoEngine


@dataclass
class VerifyResult:
    """一个候选点的验证结果。"""

    coord: str                                  # 候选点（界面坐标；pass 为 "pass"）
    winrate: Optional[float] = None             # 候选方胜率 0~1（搜索后）
    score_lead: Optional[float] = None          # 候选方目数领先
    visits: Optional[int] = None
    pv: list[str] = field(default_factory=list)  # 含候选点在内的变化序列
    best_coord: Optional[str] = None            # 对手最佳应手（pv 第二手）
    error: Optional[str] = None                 # 非法着点/搜索失败原因


def verify_position(
    setup_sgf: str,
    candidate_coords: list[str],
    profile: str = "standard",
    max_visits: Optional[int] = None,
    timeout: float = 600.0,
) -> list[VerifyResult]:
    """对候选点列表逐个搜索验证，返回每个候选点的胜率与 PV。

    候选点逐一提交（引擎内部 numAnalysisThreads 并行度由 cfg 决定，
    这里单次只查一个局面，线程安全且顺序可预测）。
    """
    if not candidate_coords:
        return []
    executable, model, cfg_path = _resolve_paths()
    engine = KataGoEngine(executable, model, cfg_path, analysis_threads=1)
    results: list[VerifyResult] = []
    try:
        engine.start()
        parsed = sgf_io.parse_sgf(setup_sgf)
        size = parsed.board_size
        base_moves = [[c, "pass" if not p else p] for c, p in parsed.moves]
        for coord in candidate_coords:
            color = "W" if len(base_moves) % 2 else "B"
            c = coord.strip().upper()
            move = "pass" if c in ("", "PASS") else c
            moves = [list(m) for m in base_moves] + [[color, move]]
            n = len(moves)
            req = _game_to_query(setup_sgf, profile, [n])
            req["moves"] = moves
            if max_visits:
                req["maxVisits"] = max_visits
            try:
                resp = engine.query(req, timeout=timeout)
            except EngineError as exc:
                # 非法着点（已占用/自杀）等：返回 error 标记而非抛异常
                results.append(VerifyResult(coord=move, error=str(exc)))
                continue
            root = (resp or {}).get("rootInfo") or {}
            move_infos = (resp or {}).get("moveInfos") or []
            opp_winrate = root.get("winrate")
            winrate = (1.0 - float(opp_winrate)) if opp_winrate is not None else None
            score_lead = root.get("scoreLead")
            score_lead = -float(score_lead) if score_lead is not None else None
            best = next((m for m in move_infos if m.get("order") == 0), None)
            pv: list[str] = []
            best_coord: Optional[str] = None
            if best is not None:
                raw_pv = best.get("pv") or []
                pv = [
                    ("" if str(p).lower() == "pass" else str(p)) for p in raw_pv
                ]
                bm = str(best.get("move", ""))
                best_coord = "" if bm.lower() == "pass" else bm
            full_pv = [("" if move == "pass" else move)] + pv
            results.append(
                VerifyResult(
                    coord=move,
                    winrate=winrate,
                    score_lead=score_lead,
                    visits=root.get("visits"),
                    pv=full_pv,
                    best_coord=best_coord,
                )
            )
    finally:
        engine.stop()
    return results
