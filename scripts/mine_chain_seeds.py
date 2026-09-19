# -*- coding: utf-8 -*-
"""定式种子半自动挖掘（v1.7.3）。

思路：定式终局是**两分**局面，没有「一手定生死」的棋串，直接当种子长不出题
（实测 10 条链全部 0 题）。本工具从定式起点出发，**沿局部实战往前走**，
用便宜的规则（气数分析）筛掉没戏的方向，只在「有紧张棋形」的节点上花引擎钱，
再用**与 grow 完全相同的验题路径**判定：能长出题目的局面才算种子。

- 规则预筛（不启引擎）：`life_death.liberty_groups` / `race_analysis`——
  双方各有一个 ≤3 气的棋串（对杀苗头）、或任一方有 ≤2 气的棋串（急所苗头）；
- 引擎推进：每个待展开节点问一次 KataGo 的局部前 K 手（常驻引擎，快）；
- 种子判定：把该局面注册成临时链，`chains.grow_chain(max_depth=1)` 跑一遍
  ——`added ≥ 1` 即「可长出题」的种子（与线上同一条代码路径，不会出现
  「挖出来能过、真 grow 过不了」的偏差）；
- 产出：种子 SGF（含完整手顺，链 provenance 保留）+ 可选 `--register`
  直接注册并生长进正式库。

用法：
    # 从某条已注册链的 root_sgf 出发挖种子（先试跑，不写库）
    python scripts/mine_chain_seeds.py --from-chain chain-dafei-33 --max-tests 6
    # 从任意定式片段出发
    python scripts/mine_chain_seeds.py --sgf "(;GM[1]FF[4]SZ[19];B[dp];W[cn])"
    # 找到后直接注册 + 生长（写入 data/chains/ 与正式库）
    python scripts/mine_chain_seeds.py --from-chain chain-yaodao --register
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common import db as db_mod  # noqa: E402
from backend.common import sgf_io  # noqa: E402
from backend.common.settings import get_settings  # noqa: E402
from backend.services.engine.analyze_sgf import _game_to_query, _resolve_paths  # noqa: E402
from backend.services.engine.engine import KataGoEngine  # noqa: E402
from backend.services.problems import chains, life_death, store, utils  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "chains"


def _cfg() -> dict:
    cfg = get_settings().get("problems", {})
    return cfg if isinstance(cfg, dict) else {}


def tension(position: utils.Position, size: int) -> tuple[bool, str]:
    """规则预筛：这个局面有没有「死活/对杀」苗头（不启引擎，毫秒级）。"""
    if not position:
        return False, ""
    groups = life_death.liberty_groups(position, size, min_libs=3)
    if not groups:
        return False, ""
    tight = [g for g in groups if g["liberties"] <= 2]
    if tight:
        side = tight[0]["color"]
        return True, f"{'黑' if side == 'B' else '白'}方有 {tight[0]['liberties']} 气棋串"
    race = life_death.race_analysis(position, size)
    if race.get("race"):
        return True, life_death.race_text(race)
    if len(groups) >= 2:
        return True, f"双方各有紧气串（{len(groups)} 处 ≤3 气）"
    return False, ""


def local_moves(
    engine: KataGoEngine, setup_sgf: str, moves: list[tuple[str, str]],
    size: int, breadth: int, profile: str,
) -> list[str]:
    """常驻引擎给出当前局面的前 K 个局部候选（界面坐标）。"""
    seq = [["B", "pass"]] if chains._next_color(list(moves)) == "W" else []
    for c, m in moves:
        seq.append([c, m if m else "pass"])
    q = _game_to_query(setup_sgf, profile, [len(seq)])
    q["moves"] = seq
    resp = engine.query(q) or {}
    out: list[str] = []
    for info in (resp.get("moveInfos") or [])[: max(1, breadth) * 3]:
        mv = str(info.get("move") or "")
        if mv.lower() == "pass":
            continue
        coord = mv
        try:
            utils.coord_to_xy(coord)
        except ValueError:
            continue
        if coord not in out:
            out.append(coord)
        if len(out) >= breadth:
            break
    return out


def _moves_sgf(moves: list[tuple[str, str]], size: int, name: str,
               theme: str, desc: str) -> str:
    body = "".join(
        f";{c}[{sgf_io.coord_to_sgf(m, size)}]" for c, m in moves if m
    )
    return (f"(;GM[1]FF[4]CA[UTF-8]SZ[{size}]KM[7.5]"
            f"C[{name}]GN[{name}]RE[{theme}]GC[{desc}]{body})")


def probe(engine: KataGoEngine, stones: utils.Position, size: int,
          solver: str, profile: str) -> dict:
    """用常驻引擎快测：正解/次优/脱先 三个读数（诊断「为什么不成题」）。"""
    setup = utils.setup_sgf(stones, solver, size)
    cands = chains.build_candidates(stones, size, max_candidates=12)
    region = chains.region_of(stones, size, 2)
    seq0 = [["B", "pass"]] if solver == "W" else []
    out = {"best": None, "second": None, "pass": None, "best_coord": None}
    for coord in cands:
        color = "W" if len(seq0) % 2 else "B"
        moves = list(seq0) + [[color, "pass" if coord == "pass" else coord]]
        q = _game_to_query(setup, profile, [len(moves)])
        q["moves"] = moves
        q["allowMoves"] = [{"player": pl, "moves": region, "untilDepth": 100}
                           for pl in "BW"]
        try:
            r = engine.query(q) or {}
        except Exception:  # noqa: BLE001
            continue
        wr = ((r.get("rootInfo") or {}).get("winrate"))
        if wr is None:
            continue
        val = 1.0 - float(wr)
        if coord == "pass":
            out["pass"] = round(val, 4)
        else:
            wrs = out.setdefault("_list", [])
            wrs.append((coord, val))
    wrs = sorted(out.pop("_list", []), key=lambda t: t[1], reverse=True)
    if wrs:
        out["best_coord"], out["best"] = wrs[0][0], round(wrs[0][1], 4)
        if len(wrs) > 1:
            out["second"] = round(wrs[1][1], 4)
    return out


def accepts_as_seed(
    stones: utils.Position, size: int, solver: str, mode: str, profile: str,
) -> list[dict]:
    """用真实验题路径判定：这个局面能不能长出题（临时链 + grow 1 层）。"""
    sgf = utils.setup_sgf(stones, solver, size)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mine.db"
        db_mod.init_db(db)
        chains.register_chain(
            {"id": "chain-mine", "name": "挖掘", "root_sgf": sgf}, db
        )
        try:
            res = chains.grow_chain(
                "chain-mine", max_depth=1, max_per_level=1,
                profile=profile, verify_mode=mode, db_path=db,
            )
        except Exception:  # noqa: BLE001
            return []
        if res.get("added"):
            return [dict(p) for p in res.get("problems") or []]
        return []


def mine(
    opening: str, mode: str, profile: str, max_depth: int, breadth: int,
    max_tests: int, name: str, target: int,
) -> list[dict]:
    parsed = sgf_io.parse_sgf(opening)
    size = int(parsed.board_size)
    base_moves = list(parsed.moves)
    base_stones = chains.setup_stones(parsed, size)
    exe, model, cfgp = _resolve_paths()
    engine = KataGoEngine(exe, model, cfgp, analysis_threads=1)
    seeds: list[dict] = []
    tests = 0
    nodes = 0
    engine.start()
    try:
        queue: list[list[tuple[str, str]]] = [base_moves]
        seen: set[str] = set()
        while queue and tests < max_tests and len(seeds) < target:
            moves = queue.pop(0)
            key = "".join(f"{c}{m}" for c, m in moves)
            if key in seen:
                continue
            seen.add(key)
            nodes += 1
            try:
                position = utils.apply_moves(dict(base_stones), moves, size)
            except ValueError:
                continue
            kept = chains.crop_local(position, size, pad=1, max_bbox=9)
            if kept is None:
                continue
            depth = len(moves) - len(base_moves)
            hot, why = tension(kept, size)
            solver = chains._next_color(moves)
            if hot and depth >= 2:
                tests += 1
                diag = probe(engine, kept, size, solver, profile)
                problems = accepts_as_seed(kept, size, solver, mode, profile)
                g2 = (None if diag["best"] is None or diag["second"] is None
                      else round(diag["best"] - diag["second"], 2))
                gp = (None if diag["best"] is None or diag["pass"] is None
                      else round(diag["best"] - diag["pass"], 2))
                print(f"  [测试 {tests:2d}] 深度{depth} 子={len(kept)} {why}"
                      f" | 正解={diag['best']}({diag['best_coord']})"
                      f" 次优={diag['second']} 脱先={diag['pass']}"
                      f" 差={g2}/{gp}"
                      f" → {'可出题 ' + str(len(problems)) + ' 道' if problems else '不达标'}")
                if problems:
                    seeds.append({
                        "moves": list(moves),
                        "stones": dict(kept),
                        "solver": solver,
                        "why": why,
                        "problems": problems,
                    })
                    continue
            if depth >= max_depth or tests >= max_tests:
                continue
            for mv in local_moves(engine, opening, moves, size, breadth, profile):
                try:
                    utils.apply_moves(dict(base_stones), moves + [(solver, mv)], size)
                except ValueError:
                    continue
                queue.append(moves + [(solver, mv)])
            if nodes % 10 == 0:
                print(f"  ..遍历 {nodes} 节点 / 测试 {tests} / 队列 {len(queue)}")
    finally:
        engine.stop()
    print(f"遍历 {nodes} 节点、验题 {tests} 次，找到 {len(seeds)} 个种子")
    return seeds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-chain", default="", help="用某条已注册链的 root_sgf 起步")
    ap.add_argument("--sgf", default="", help="直接给定式 SGF 片段")
    ap.add_argument("--file", default="", help="从文件读定式 SGF")
    ap.add_argument("--mode", default="", help="验收口径（默认取 config）")
    ap.add_argument("--profile", default="")
    ap.add_argument("--max-depth", type=int, default=6)
    ap.add_argument("--breadth", type=int, default=3)
    ap.add_argument("--max-tests", type=int, default=6)
    ap.add_argument("--target", type=int, default=2)
    ap.add_argument("--name", default="", help="种子链名（默认沿用来源名 + 挖掘）")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--register", action="store_true",
                    help="找到后写入 data/chains/ 并注册 + 生长进正式库")
    args = ap.parse_args()

    cfg = _cfg()
    mode = args.mode or str(cfg.get("chain_verify_mode", "local_board"))
    profile = args.profile or str(
        (cfg.get("verify_profile") or {}).get("life_death") or "fast")
    if args.sgf:
        opening, name = args.sgf, (args.name or "挖掘定式")
    elif args.file:
        p = Path(args.file)
        opening, name = p.read_text(encoding="utf-8"), (args.name or p.stem)
    elif args.from_chain:
        chain = chains.get_chain(args.from_chain)
        if chain is None:
            print(f"[X] 链不存在: {args.from_chain}")
            return
        opening, name = chain["root_sgf"], (args.name or chain["name"])
    else:
        print("[X] 需要 --from-chain / --sgf / --file 之一")
        return

    print(f"起点定式：{name}  口径={mode}  档位={profile}")
    t0 = time.time()
    seeds = mine(opening, mode, profile, args.max_depth, args.breadth,
                 args.max_tests, name, args.target)
    print(f"耗时 {time.time() - t0:.0f}s")
    if not seeds:
        print("未找到可达标种子：可以加大 --max-depth/--max-tests，"
              "或换一条「终局本身带生死」的定式起点。")
        return

    out_dir = Path(args.out)
    for i, seed in enumerate(seeds, 1):
        stem = f"mined-{name}-{i}"
        desc = (f"由 scripts/mine_chain_seeds.py 从「{name}」挖掘："
                f"{seed['why']}；生长出 {len(seed['problems'])} 道题（口径 {mode}）")
        theme = "mixed"
        if seed["problems"]:
            themes = {p["theme"] for p in seed["problems"]}
            theme = ("capturing_race" if "capturing_race" in themes else
                     ("life_death" if "life_death" in themes else "mixed"))
        sgf = _moves_sgf(seed["moves"], 19, f"{name}·死活苗头", theme, desc)
        print(f"\n[种子 {i}] {seed['why']}  手顺 {len(seed['moves'])} 手")
        print("  " + sgf)
        if args.register:
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{stem}.sgf"
            path.write_text(sgf, encoding="utf-8", newline="\n")
            chains.register_chain({
                "id": f"chain-{stem}", "name": f"{name}·死活苗头",
                "theme": theme, "root_sgf": sgf, "description": desc,
            })
            res = chains.grow_chain(f"chain-{stem}")
            print(f"  [注册] {path.name} → chain-{stem}：新增 {res['added']} 题"
                  f"（丢弃 {res['discarded']}）")
            for p in store.list_chain_problems(f"chain-{stem}"):
                print(f"    #{p['chain_step']} {p['theme']}/{p['goal']} "
                      f"正解={p['answer']} {p['hint'][:34]}")


if __name__ == "__main__":
    main()
