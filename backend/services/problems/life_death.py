# -*- coding: utf-8 -*-
"""快速局部死活判定（v1.7.2）。

给一个棋形（摆子局或含手顺的 SGF），**不生成题、不调 LLM**，直接用 KataGo
局部推演给出双方在该局部的死活结论：净活 / 劫活 / 双活 / 净死 / 未定。

口径与 v1.5.0 练习深度讲解（``explainer.py`` 局部推演）、原型
``scripts/classify_result.py`` 完全一致——``allowMoves`` 锁局部 +
``includeOwnership``——只是把「先手方视角的目标区归属」推广成**每一方**在
该区域的控制力，并叠加劫争特征与脱先损失：

1. Q_me：轮到某方走（局部搜索）→ 该区域的平均归属（该方视角，正 = 该方控制
   这块地方 = 自己的棋活了 / 对方的棋死了）；
2. Q_opp：先手方脱先、对手抢到局部要点后 → 同一区域的归属；
3. 劫争：两侧变化里同一坐标被双方先后落子（打劫的典型特征，与 explainer
   的 ``_dup_flags`` 同一判据）；
4. 结论映射（0~1 控制力）：
   - ≥ 0.60 → **活**（该方稳控该区）
   - 0.15~0.60 → **劫活/双活**（劫争特征明确 → 劫活；否则 → 双活/未定）
   - ≤ -0.60 → **死**（该区被对方控住）
   - 其余 → **未定**（双方都未定型）

**已知边界（务必知道）**：空棋盘上的**绝对**死活判定不可靠——19 路摆子局
的评估被「谁朝向空旷盘面」主导（实测：17 子角部实空只有 3 目时判落后 26 目），
所以本模块更适合**封闭局部**（题面、角部定型图、古典死活题的棋形）。若给出
的是空旷盘面上的孤子，结论多为「未定」，这是如实反映而非报错。
"""

from __future__ import annotations

from typing import Optional

from ...common import sgf_io
from ...common.settings import get_settings
from ..engine.analyze_sgf import _game_to_query, _resolve_paths
from ..engine.engine import EngineError, KataGoEngine
from . import chains, utils

# 控制力 → 结论 的阈值（与 classify_result.py 的 0.85/0.5 同源，按区域平均放宽）
ALIVE_MIN = 0.50      # ≥ 该值：该方棋子安定（活）
DEAD_MAX = -0.50      # ≤ 该值：该方棋子被对方控住（死）
KO_MIN = 0.15         # 处在该值与 ALIVE_MIN 之间：疑似劫/双活


def _avg(own: Optional[list], size: int, cells: list[tuple[int, int]], side: str):
    if not own or len(own) != size * size:
        return None
    vals = [own[y * size + x] for x, y in cells]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    return round(avg if side.upper() == "B" else -avg, 4)


def _dup_points(*lines: list[str]) -> list[str]:
    """打劫特征：**同一条变化内**同一坐标被重复落子（回提）。

    注意：不同变化之间出现同一点是常态（双方都争同一个要点），不能算劫
    ——v1.7.2 实测时正是这个误判把每道题都标成了劫。
    """
    out: set[str] = set()
    for line in lines:
        seen: set[str] = set()
        for cd in line:
            c = utils.normalize_coord(cd)
            if not c or c == "pass":
                continue
            if c in seen:
                out.add(c)
            seen.add(c)
    return sorted(out)


def pv_sequence(
    setup_sgf: str, coords: list[str], size: int
) -> list[list[str]]:
    """把引擎给的变化序列整理成可直接提交的 moves 列表。

    关键点：变化里可能出现**已占点**（打劫回提、与摆子重叠），整段重放会
    ``Illegal move``；这类着法必须丢弃，但**丢弃不能打乱黑白交替**——所以
    丢掉的那一手用同色的 ``pass`` 占位（保留轮次，后续着法颜色不变）。
    """
    parsed = sgf_io.parse_sgf(setup_sgf)
    seq: list[list[str]] = [
        [c, "pass" if not p else p] for c, p in parsed.moves
    ]
    color = "W" if len(seq) % 2 else "B"
    occupied = {
        utils.coord_to_xy(c) for _col, c in parsed.setup if c
    }
    occupied |= {
        utils.coord_to_xy(c) for _col, c in parsed.moves
        if c and utils.normalize_coord(c) != "pass"
    }
    for cd in coords:
        c = utils.normalize_coord(cd)
        skip = False
        if not c or c == "pass":
            skip = True
        else:
            try:
                xy = utils.coord_to_xy(c)
            except ValueError:
                skip = True
            else:
                if xy in occupied:
                    skip = True
                else:
                    occupied.add(xy)
        seq.append([color, "pass" if skip else c])
        color = "W" if color == "B" else "B"
    return seq


def _status(control: Optional[float]) -> str:
    """棋子处归属 → 该方状态。

    只做**保守**判定：稳（≥0.5）为活、被对方控住（≤-0.5）为死，其余一律
    未定——劫活/双活需要更多证据（劫争回提、双方气数），由 verdict_text
    结合 ko 标志表述，避免误报（v1.7.2 实测：把中间带直接叫"劫活"会误报）。
    """
    if control is None:
        return "未定"
    if control >= ALIVE_MIN:
        return "活"
    if control <= DEAD_MAX:
        return "死"
    return "未定"


def classify(
    setup_sgf: str,
    profile: str = "fast",
    region_pad: Optional[int] = None,
    pv_len: int = 8,
) -> dict:
    """对棋形做快速局部死活判定（纯 KataGo，无 LLM、无棋盘裁剪副作用）。

    返回 ``{"board_size", "to_move", "stones", "control": {"B","W"},
    "status": {"B","W"}, "ko", "ko_points", "tenuki_loss", "grade",
    "line", "profile", "note"}``。
    """
    cfg = get_settings().get("problems", {}) or {}
    if region_pad is None:
        region_pad = int(cfg.get("chain_region_pad", 2) if isinstance(cfg, dict) else 2)
    parsed = sgf_io.parse_sgf(setup_sgf)
    size = int(parsed.board_size)
    position = utils.apply_moves(
        chains.setup_stones(parsed, size), list(parsed.moves), size
    )
    out: dict = {
        "board_size": size, "to_move": chains._next_color(list(parsed.moves)),
        "stones": len(position), "control": {"B": None, "W": None},
        "status": {"B": "未定", "W": "未定"}, "ko": False, "ko_points": [],
        "tenuki_loss": None, "grade": None, "line": [], "profile": profile,
        "note": "纯局部推演（allowMoves 锁题面区域）；空旷盘面上的绝对死活仅供参考",
    }
    if not position:
        return out
    kept = chains.crop_local(position, size, pad=1, max_bbox=int(
        cfg.get("chain_max_bbox", 9) if isinstance(cfg, dict) else 9))
    if kept is None:
        out["note"] = "局部超过 9×9，未判定（请给出封闭的局部棋形）"
        return out
    region = chains.region_of(kept, size, region_pad)
    # 判定信号 = **各方自己的棋子**处的归属：活棋≈本方（+），死子会翻给对方（-）。
    # （对整个包围盒取平均会被空地摊平——实测那样每道题都读成 未定）
    def _stone_cells(color: str) -> list[tuple[int, int]]:
        return [
            (x, size - 1 - y)             # 界面 y（下为 0）→ ownership 行号（上为 0）
            for (x, y), col in kept.items() if col == color
        ]

    cells_by_side = {"B": _stone_cells("B"), "W": _stone_cells("W")}
    solver = out["to_move"]
    opp = "W" if solver == "B" else "B"
    setup = utils.setup_sgf(kept, solver, size)

    exe, model, cfg_path = _resolve_paths()
    engine = KataGoEngine(exe, model, cfg_path, analysis_threads=1)

    def run(seq: list[list[str]]) -> dict:
        q = _game_to_query(setup, profile, [len(seq)])
        q["moves"] = seq
        q["allowMoves"] = [
            {"player": "B", "moves": region, "untilDepth": 100},
            {"player": "W", "moves": region, "untilDepth": 100},
        ]
        q["includeOwnership"] = True
        return engine.query(q) or {}

    def run_soft(seq: list[list[str]]) -> dict:
        """提交序列；引擎重放校验很严（打劫回提/重复落子会 Illegal move），
        失败时**逐步缩短**序列，保证判定总能给出一个基于可重放局面的读数。"""
        for cut in range(len(seq), -1, -1):     # cut=0 → 提交空序列（初始局面）
            try:
                return run(seq[:cut])
            except EngineError:
                continue
        return {}

    def pv_of(resp: dict) -> list[str]:
        infos = (resp or {}).get("moveInfos") or []
        best = next((m for m in infos if m.get("order") == 0), None)
        return [utils.normalize_coord(str(x))
                for x in ((best or {}).get("pv") or [])][:pv_len]

    try:
        engine.start()
        # 与 verify_position 同口径：白先题面前置的 B[tt] 也算进 moves
        base = [["B", "pass"]] if solver == "W" else []
        # 1) 轮到先手方：其最佳变化（局部 PV）走完后的区域归属
        r1 = run_soft(base)
        pv1 = pv_of(r1)
        r1b = run_soft(pv_sequence(setup, pv1, size)) if pv1 else r1
        # 2) 脱先分支：先手方 pass（占位保持轮次）→ 对手抢要点 → 其后变化
        r2 = run_soft(base + [[solver, "pass"]])
        opp_move = utils.normalize_coord(
            next((m.get("move") for m in ((r2 or {}).get("moveInfos") or [])
                  if m.get("order") == 0), "") or ""
        )
        line2 = ([opp_move] + pv_of(r2)) if opp_move and opp_move != "pass" else []
        seq2 = pv_sequence(setup, ["pass"] + line2, size) if line2 else []
        r2b = run_soft(seq2) if seq2 else r2
        own1 = (r1b or {}).get("ownership")
        own2 = (r2b or {}).get("ownership")
        out["control"]["B"] = _avg(own1, size, cells_by_side["B"], "B")
        out["control"]["W"] = _avg(own1, size, cells_by_side["W"], "W")
        a1 = _avg(own1, size, cells_by_side[solver], solver)
        a2 = _avg(own2, size, cells_by_side[solver], solver)
        if a1 is not None and a2 is not None:
            out["tenuki_loss"] = round(a1 - a2, 4)
            out["grade"] = (
                "紧急" if out["tenuki_loss"] >= 0.4
                else ("半紧急" if out["tenuki_loss"] >= 0.15 else "可脱先")
            )
        dups = _dup_points(pv1, line2)
        out["ko"] = bool(dups)
        out["ko_points"] = dups
        out["line"] = pv1
        out["status"]["B"] = _status(out["control"]["B"])
        out["status"]["W"] = _status(out["control"]["W"])
    except Exception as exc:  # noqa: BLE001 —— 判定失败不该抛给调用方
        out["note"] = f"判定未完成：{exc}"
    finally:
        engine.stop()
    return out


def verdict_text(report: dict) -> str:
    """判定报告 → 一句话中文结论。"""
    st = report.get("status") or {}
    ctl = report.get("control") or {}
    b, w = st.get("B", "未定"), st.get("W", "未定")

    def _num(k: str) -> str:
        v = ctl.get(k)
        return "—" if v is None else f"{v:.2f}"

    if b == "活" and w == "死":
        core = f"黑方活棋、白方死棋（黑棋子归属 {_num('B')} / 白 {_num('W')}）"
    elif w == "活" and b == "死":
        core = f"白方活棋、黑方死棋（白棋子归属 {_num('W')} / 黑 {_num('B')}）"
    elif b == "活" and w == "活":
        core = f"双方各自安定（黑 {_num('B')} / 白 {_num('W')}）"
    elif b == "死" and w == "死":
        core = f"双方均被对方控住（黑 {_num('B')} / 白 {_num('W')}）——多为一决生死的对杀"
    else:
        core = (f"局部未定型（黑 {_num('B')} / 白 {_num('W')}）"
                f"——两分或需要继续推演")
    loss = report.get("tenuki_loss")
    if loss is not None:
        core += f"；脱先损失 {loss:.2f}（{report.get('grade')}）"
    if report.get("ko"):
        core += f"；劫争坐标 {','.join(report.get('ko_points') or [])}"
    return core
