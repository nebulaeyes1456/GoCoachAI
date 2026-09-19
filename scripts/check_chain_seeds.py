# -*- coding: utf-8 -*-
"""定式种子体检（v1.7.0 题链工具）。

对 ``data/chains/*.sgf``（或指定文件）逐个跑一遍链生长的验题流程，报告该
种子**能不能长出题目**：

- 局部裁剪（包围盒外扩 1 格 / 超 9×9 丢弃）；
- 候选枚举（局部空邻点 + 角部要点 + pass）；
- 逐候选点胜率（单引擎常驻，比逐题 grow 快得多）；
- 合格判定：正解 > ``problems.chain_answer_min_winrate``（默认 0.95）、
  次优 < ``chain_second_max_winrate``（默认 0.3）、死活/对杀另需
  pass 后胜率 < ``urgency_max_winrate``（默认 0.3）。

**为什么要这个工具**：19 路定式终局是两分局面，双方都没有生死攸关的棋串，
验题阈值（正解 > 0.95 且次优 < 0.3）几乎不可能达标——实测 10 条常见定式链
首轮 grow 均产出 0 题。想长出真正的死活/对杀题，种子的**终局局面本身要带
生死**（角上大龙未活、双方对杀）。改完 SGF 用本工具先体检，再 seed + grow，
可以省下大量引擎时间。

用法：
    python scripts/check_chain_seeds.py                    # 体检 data/chains/
    python scripts/check_chain_seeds.py --file x.sgf       # 体检单个文件
    python scripts/check_chain_seeds.py --profile fine     # 换档位
    python scripts/check_chain_seeds.py --no-allow         # 全盘口径（不加 allowMoves）
    python scripts/check_chain_seeds.py --sgf "(;GM[1]FF[4]SZ[19];B[pd];W[qc])"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common import sgf_io  # noqa: E402
from backend.common.settings import get_settings  # noqa: E402
from backend.services.engine.verify import verify_position  # noqa: E402
from backend.services.problems import chains, life_death, utils  # noqa: E402

DEFAULT_DIR = ROOT / "data" / "chains"


def _cfg() -> dict:
    cfg = get_settings().get("problems", {})
    return cfg if isinstance(cfg, dict) else {}


def check(sgf_text: str, name: str, profile: str, use_allow: bool) -> bool:
    """体检一个种子；返回是否达标（可长出题）。"""
    cfg = _cfg()
    pad = int(cfg.get("chain_crop_pad", 1))
    max_bbox = int(cfg.get("chain_max_bbox", 9))
    max_candidates = int(cfg.get("max_candidates", 12))
    region_pad = int(cfg.get("chain_region_pad", 2))
    urgent_max = float(cfg.get("urgency_max_winrate", 0.3))
    answer_min = float(cfg.get("chain_answer_min_winrate", 0.95))
    second_max = float(cfg.get("chain_second_max_winrate", 0.3))

    parsed = sgf_io.parse_sgf(sgf_text)
    size = int(parsed.board_size)
    stones = chains.setup_stones(parsed, size)
    moves = list(parsed.moves)
    try:
        position = utils.apply_moves(dict(stones), moves, size)
    except ValueError as exc:
        print(f"[X] {name}: 手顺无法重放（{exc}）")
        return False
    kept = chains.crop_local(position, size, pad=pad, max_bbox=max_bbox)
    if kept is None:
        print(f"[X] {name}: 局部裁剪超 {max_bbox}×{max_bbox} 或无子")
        return False
    x0, y0, x1, y1 = utils.bbox_of(kept.keys())
    solver = chains._next_color(moves)
    theme = utils.classify_theme(kept, position, size, 1, 0)
    goal = chains.classify_goal(theme, kept, size, solver)
    setup = utils.setup_sgf(kept, solver, size)
    cands = chains.build_candidates(kept, size, max_candidates=max_candidates)
    allow = chains.region_of(kept, size, region_pad) if use_allow else None

    results = verify_position(setup, cands, profile=profile, allow_moves=allow)
    valid = sorted(
        [r for r in results
         if r.error is None and r.coord != "pass" and r.winrate is not None],
        key=lambda r: r.winrate or 0.0, reverse=True,
    )
    best = valid[0] if valid else None
    second = valid[1] if len(valid) > 1 else None
    pass_r = next((r for r in results if r.coord == "pass"), None)
    pass_wr = pass_r.winrate if pass_r else None
    urgent = theme in ("life_death", "capturing_race")
    ok = (
        best is not None and (best.winrate or 0) > answer_min
        and (second is None or (second.winrate or 0) < second_max)
        and (not urgent or (pass_wr is not None and pass_wr < urgent_max))
    )
    bbox = f"{x1 - x0 + 1}x{y1 - y0 + 1}"
    print(f"{'[OK]' if ok else '[X] '} {name:22s} {solver}先 子={len(kept):2d}"
          f" 包围盒={bbox} {theme}/{goal}")
    print(f"      正解={_fmt(best)} 次优={_fmt(second)} pass={_fmt_wr(pass_wr)}"
          f"  胜率合格线={answer_min}/{second_max}"
          f"{'（需紧迫）' if urgent else ''}")

    # ---- 小棋盘口径（默认 chain_verify_mode=local_board）：局部裁成小棋盘再验 ----
    board_ok = False
    if best is not None:
        sub, mapping, _dx, _dy = chains.corner_board(kept, size, 2, 5)
        if sub < size:
            sub_setup = utils.setup_sgf(mapping, solver, sub)
            sub_cands = chains.build_candidates(mapping, sub, max_candidates)
            sub_region = chains.region_of(mapping, sub, region_pad)
            sub_res = verify_position(sub_setup, sub_cands, profile=profile,
                                      allow_moves=sub_region)
            sub_valid = sorted(
                [r for r in sub_res if r.error is None and r.coord != "pass"
                 and r.winrate is not None],
                key=lambda r: r.winrate or 0.0, reverse=True)
            sb = sub_valid[0] if sub_valid else None
            ss = sub_valid[1] if len(sub_valid) > 1 else None
            sp = next((r.winrate for r in sub_res if r.coord == "pass"), None)
            board_ok = (
                sb is not None and (sb.winrate or 0) > answer_min
                and (ss is None or (ss.winrate or 0) < second_max)
                and (not urgent or (sp is not None and sp < urgent_max))
            )
            print(f"      小棋盘（{sub}路，{len(mapping)}子）：正解={_fmt(sb)} "
                  f"次优={_fmt(ss)} pass={_fmt_wr(sp)}"
                  f"  → {'达标' if board_ok else '不达标'}")

    # ---- 相对口径（relative）：正解 − 次优 / 正解 − 脱先 的差距 ----
    rel_ok = False
    if best is not None and second is not None and pass_wr is not None:
        cfg2 = _cfg()
        gap = (best.winrate or 0) - (second.winrate or 0)
        urg = (best.winrate or 0) - pass_wr
        rel_ok = (gap >= float(cfg2.get("chain_relative_gap", 0.5))
                  and urg >= float(cfg2.get("chain_relative_urgency", 0.3)))
        print(f"      相对判据：正解−次优={gap:.2f} 正解−脱先={urg:.2f}"
              f"  → {'达标' if rel_ok else '不达标'}"
              f"（合格线 {cfg2.get('chain_relative_gap', 0.5)}/"
              f"{cfg2.get('chain_relative_urgency', 0.3)}）")

    # ---- 局部死活口径（v1.5.0 深度讲解同一套推演）：能否达成目标 + 能否脱先 ----
    local_ok = False
    if best is not None and best.winrate is not None and allow:
        cfgd = _cfg()
        target = utils.coord_to_xy(best.coord)
        prof = chains.local_death_profile(
            setup, size, solver, best.coord, list(best.pv or []), allow,
            target,
            profile=str(cfgd.get("chain_death_profile", "fast")),
            pv_len=int(cfgd.get("chain_death_pv_len", 6)),
        )
        own_min = float(cfgd.get("chain_own_min", 0.75))
        tenuki_min = float(cfgd.get("chain_tenuki_min", 0.15))
        own_pv = prof.get("own_pv")
        loss = prof.get("tenuki_loss")
        local_ok = (
            own_pv is not None and own_pv >= own_min
            and (not urgent or (loss is not None and loss >= tenuki_min))
        )
        death = chains.goal_text(goal, prof.get("result_type"), solver)
        print(f"      局部死活：目标区归属={_fmt_wr(own_pv)} 脱先损失="
              f"{_fmt_wr(loss)}（{prof.get('grade') or '-'}）"
              f" 结果={prof.get('result_type') or '-'}"
              f"{' ' + death if death else ''}"
              f"  → {'可出题' if local_ok else '不达标'}"
              f"（合格线 归属≥{own_min} / 脱先损失≥{tenuki_min}）")
    race = life_death.race_analysis(kept, size)
    if race.get("race") or race.get("seki"):
        print(f"      气数分析：{life_death.race_text(race)}")
    print(f"      setup={setup}")
    top = " ".join(
        f"{r.coord}:{r.winrate:.2f}" for r in valid[:5]
    )
    print(f"      候选前五：{top}")
    if not ok and not local_ok:
        why = []
        if best is None or (best.winrate or 0) <= answer_min:
            why.append(f"正解胜率 {_fmt_wr(best.winrate if best else None)} "
                       f"未过 {answer_min}")
        if second is not None and (second.winrate or 0) >= second_max:
            why.append(f"次优 {second.coord} 胜率 {second.winrate:.2f} "
                       f"≥ {second_max}（要点不唯一）")
        if urgent and (pass_wr is None or pass_wr >= urgent_max):
            why.append(f"脱先胜率 {_fmt_wr(pass_wr)} 不紧迫")
        print("      胜率口径未过：" + "；".join(why) + "（19 路定式局面属正常）")
    return ok or local_ok or board_ok or rel_ok


def _fmt(r) -> str:
    if r is None:
        return "-"
    return f"{r.coord}:{_fmt_wr(r.winrate)}"


def _fmt_wr(wr) -> str:
    return "-" if wr is None else f"{wr:.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--file", default="")
    ap.add_argument("--sgf", default="")
    ap.add_argument("--profile", default="")
    ap.add_argument("--no-allow", action="store_true",
                    help="不加 allowMoves（全盘口径），默认局部聚焦")
    args = ap.parse_args()
    cfg = _cfg()
    profile = args.profile or str(cfg.get("verify_profile", {}).get("life_death")
                                  or "standard")

    targets: list[tuple[str, str]] = []
    if args.sgf:
        targets.append(("(命令行 SGF)", args.sgf))
    elif args.file:
        p = Path(args.file)
        targets.append((p.stem, p.read_text(encoding="utf-8")))
    else:
        for p in sorted(Path(args.dir).glob("*.sgf")):
            targets.append((p.stem, p.read_text(encoding="utf-8")))
    if not targets:
        print("[X] 没有可体检的 SGF")
        return
    print(f"档位={profile}  口径={'局部聚焦' if not args.no_allow else '全盘'}"
          f"  合格线={cfg.get('chain_answer_min_winrate', 0.95)}/"
          f"{cfg.get('chain_second_max_winrate', 0.3)}\n")
    ok_n = 0
    for name, text in targets:
        if check(text, name, profile, not args.no_allow):
            ok_n += 1
    print(f"\n达标 {ok_n}/{len(targets)}：达标的种子首轮 grow 能长出题；"
          f"未达标的请把种子终局改成「带生死」的局面（角上大龙未活/对杀）。")


if __name__ == "__main__":
    main()
