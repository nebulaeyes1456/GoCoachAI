"""偏离惩罚查询（T4a，M2「每手一选讲解」前置）。

给定"某手落下后的局面"（``setup_sgf`` + 该手之前的行棋序列 ``moves``）与该手
KataGo 的 PV（行棋方视角，如 ``["F5","D5","E3"]``），对 PV 每一步计算"偏离惩罚"：
若轮到行棋的一方不按 PV[k-1] 走、改走次优点，会损失多少胜率与目数。

实现（复用 analysis 引擎协议，不改 engine.py/analyze_sgf.py/verify.py 签名）：
- 对 PV 第 k 步，构造"局面 + moves + PV 前 k-1 步"的查询（``analyzeTurns=[n]``，
  n=末手序号），从响应 ``moveInfos`` 取：
  - 最优手：``order == 0``（应以引擎返回为准，通常等于 pv[k-1]）；
  - 偏离手：``order == 1`` 的次优手；若缺失则取列表首个非最优手；
- 口径：cfg 固定 ``reportAnalysisWinratesAs = SIDETOMOVE``，moveInfos 的
  ``winrate``/``scoreLead`` 均为"当前行棋方"视角（与 verify.py 口径一致）：
  - ``winrate_loss = 最优手 winrate − 偏离手 winrate``（>0 表示偏离更差）；
  - ``score_loss = 最优手 scoreLead − 偏离手 scoreLead``（目数差）；
- PV 中 pass 步跳过（pass 仍计入后续步骤的局面演进）；
- 单步查询失败记入该步的 ``error`` 字段并继续后续步骤，不整体抛异常；
- 一次调用内串行查询，复用单引擎实例；visits 默认 200（由 ``max_visits`` 控制）。

契约签名（供 T4b 与上层讲解服务调用）：

    from backend.services.engine.deviate import query_deviation, DeviationPenalty

    query_deviation(setup_sgf: str,
                    moves: list[list[str]],      # 该手之前的行棋序列 [[color, coord], ...]
                    pv: list[str],               # 该手 KataGo 后续变化（行棋方视角）
                    profile: str = "standard",
                    max_visits: int | None = 200,
                    timeout: float = 600.0,
                    ) -> list[DeviationPenalty]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .analyze_sgf import _game_to_query, _resolve_paths
from .engine import EngineError, KataGoEngine


@dataclass
class DeviationPenalty:
    """PV 一步的偏离惩罚。

    - ``pv_step``：PV 序号（1 基，对应输入 pv 的第 k 项）；
    - ``pv_move``：该步 PV 着点（界面坐标大写；pass 步不产出）；
    - ``deviation_move``：偏离手（引擎次优，界面坐标大写；pass 记 ""）；
    - ``winrate_loss``：最优 − 偏离的胜率差（行棋方视角 0~1，正=偏离更差）；
    - ``score_loss``：最优 − 偏离的目数差；
    - ``visits``：该查询搜索量（rootInfo.visits）；
    - ``error``：该步查询失败或无偏离手的原因（None 表示正常）。
    """

    pv_step: int
    pv_move: str
    deviation_move: Optional[str] = None
    winrate_loss: Optional[float] = None
    score_loss: Optional[float] = None
    visits: Optional[int] = None
    error: Optional[str] = None


def _norm_move(move: object) -> str:
    """着点归一化：pass/空 → "pass"；否则大写界面坐标。"""
    c = str(move or "").strip()
    if not c or c.lower() == "pass":
        return "pass"
    return c.upper()


def _num(value: object) -> Optional[float]:
    return float(value) if value is not None else None


def _parse_step(k: int, pv_move: str, resp: dict) -> DeviationPenalty:
    """从单步响应解析偏离惩罚（最优手 vs 次优手）。"""
    move_infos = (resp or {}).get("moveInfos") or []
    root = (resp or {}).get("rootInfo") or {}
    best = next((m for m in move_infos if m.get("order") == 0), None)
    if best is None and move_infos:
        best = move_infos[0]
    if best is None:
        return DeviationPenalty(pv_step=k, pv_move=pv_move, error="moveInfos 为空")
    # 次优手：优先 order==1（以引擎返回为准），否则列表首个非最优手
    deviation = next((m for m in move_infos if m.get("order") == 1), None)
    if deviation is None or deviation is best:
        deviation = next((m for m in move_infos if m is not best), None)
    if deviation is None:
        return DeviationPenalty(
            pv_step=k,
            pv_move=pv_move,
            visits=root.get("visits") or best.get("visits"),
            error="无偏离手（moveInfos 仅 1 项）",
        )
    best_wr, dev_wr = _num(best.get("winrate")), _num(deviation.get("winrate"))
    best_sc, dev_sc = _num(best.get("scoreLead")), _num(deviation.get("scoreLead"))
    winrate_loss = (
        best_wr - dev_wr
        if best_wr is not None and dev_wr is not None
        else None
    )
    score_loss = (
        best_sc - dev_sc
        if best_sc is not None and dev_sc is not None
        else None
    )
    dev_raw = str(deviation.get("move") or "").strip()
    deviation_move = "" if dev_raw.lower() == "pass" else dev_raw.upper()
    return DeviationPenalty(
        pv_step=k,
        pv_move=pv_move,
        deviation_move=deviation_move,
        winrate_loss=winrate_loss,
        score_loss=score_loss,
        visits=root.get("visits") or best.get("visits"),
    )


def query_deviation(
    setup_sgf: str,
    moves: list[list[str]],
    pv: list[str],
    profile: str = "standard",
    max_visits: Optional[int] = 200,
    timeout: float = 600.0,
) -> list[DeviationPenalty]:
    """对 PV 每步计算偏离惩罚，返回 ``list[DeviationPenalty]``（不含 pass 步）。

    - ``setup_sgf``：含 AB/AW 摆子/棋盘信息的局面 SGF；
    - ``moves``：该手之前的行棋序列（与 ``_game_to_query`` 格式一致，
      ``[[color, coord], ...]``，pass 记 "pass"）；
    - ``pv``：该手 KataGo 后续变化（行棋方视角，如 ``["F5","D5","E3"]``）；
    - 对 PV 第 k 步：局面按 moves + PV 前 k-1 步演进，查询该局面后取
      最优手与次优手的 winrate/scoreLead 差；
    - pass 步跳过；单步查询失败记入该步 ``error``，不整体抛异常；
    - 串行查询，单次调用复用同一引擎实例。
    """
    if not pv:
        return []
    executable, model, cfg_path = _resolve_paths()
    engine = KataGoEngine(executable, model, cfg_path, analysis_threads=1)
    results: list[DeviationPenalty] = []
    seq: list[list[str]] = [list(m) for m in moves]
    try:
        engine.start()
        for k, raw in enumerate(pv, start=1):
            pv_move = _norm_move(raw)
            color = "B" if len(seq) % 2 == 0 else "W"
            if pv_move == "pass":
                # pass 步不产出惩罚，但计入后续步骤的局面演进
                seq.append([color, "pass"])
                continue
            # 查询局面 = 局面 + moves + PV 前 k-1 步（不含 PV[k-1] 本身），
            # analyzeTurns=[n] 对应查询 moves 的末手序号（与 verify.py 同法）。
            moves_k = [list(m) for m in seq]   # 快照：查询提交后不再被修改
            req = _game_to_query(setup_sgf, profile, [len(moves_k)])
            req["moves"] = moves_k
            if max_visits:
                req["maxVisits"] = int(max_visits)
            try:
                resp = engine.query(req, timeout=timeout)
            except EngineError as exc:
                results.append(
                    DeviationPenalty(
                        pv_step=k, pv_move=pv_move, error=str(exc)
                    )
                )
            else:
                results.append(_parse_step(k, pv_move, resp))
            # 无论查询成败，局面都按 PV 演进，供下一步使用
            seq.append([color, pv_move])
    finally:
        engine.stop()
    return results
