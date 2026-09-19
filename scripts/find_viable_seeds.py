# -*- coding: utf-8 -*-
"""从题集里批量挑「能长出题的种子」（v1.7.4）。

链生长的验题阈值（无论哪种口径）对**局面本身**有要求：要有「一手定生死」的
唯一急所。真实对局里这种局面很少（实测：定式后续 62 个节点 0 个达标），但
**古典死活题集**里全是——它们本来就是为这个问题构造的。

本工具对题集目录（默认 `data/library/import_classics/`，六本公版棋书）逐个：
1. 局部裁剪 + 候选枚举（与链生长同一套函数）；
2. 单次 `verify_position`（常驻引擎，fast）得到正解/次优/脱先读数；
3. 判定四种口径：winrate（契约）/ relative（相对判据，默认要看）/
   seiketsu（死活画像，可选 --deep）；
4. 命中的打印出来并（可选）写成可直接当链种子的 SGF。

用法：
    python scripts/find_viable_seeds.py --limit 120            # 抽样体检
    python scripts/find_viable_seeds.py --limit 300 --out data/chains/
    python scripts/find_viable_seeds.py --dir data/library/import_classics/guanzipu
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common import sgf_io  # noqa: E402
from backend.common.settings import get_settings  # noqa: E402
from backend.services.engine.analyze_sgf import _resolve_paths  # noqa: E402
from backend.services.engine.engine import KataGoEngine  # noqa: E402
from backend.services.engine.analyze_sgf import _game_to_query  # noqa: E402
from backend.services.engine.verify import VerifyResult, verify_position  # noqa: E402
from backend.services.problems import chains, life_death, utils  # noqa: E402

DEFAULT_LIB = ROOT / "data" / "library" / "import_classics"


def _cfg() -> dict:
    cfg = get_settings().get("problems", {})
    return cfg if isinstance(cfg, dict) else {}


def _verify_with(engine, setup_sgf: str, candidates: list[str],
                 region: list[str], profile: str) -> list:
    """与 ``verify_position`` 同口径，但复用常驻引擎（省掉每题一次模型加载）。

    这条是筛题的速度命门：verify_position 每次自建自停引擎（约 30 秒），
    而筛题要跑成百上千题 —— 直接查常驻进程，单题从 ~46 秒降到 ~5-10 秒。
    """
    parsed = sgf_io.parse_sgf(setup_sgf)
    base = [[c, "pass" if not p else p] for c, p in parsed.moves]
    out = []
    for coord in candidates:
        color = "W" if len(base) % 2 else "B"
        c = coord.strip().upper()
        move = "pass" if c in ("", "PASS") else c
        moves = [list(m) for m in base] + [[color, move]]
        q = _game_to_query(setup_sgf, profile, [len(moves)])
        q["moves"] = moves
        if region:
            q["allowMoves"] = [
                {"player": "B", "moves": region, "untilDepth": 100},
                {"player": "W", "moves": region, "untilDepth": 100},
            ]
        try:
            resp = engine.query(q) or {}
        except Exception as exc:  # noqa: BLE001 —— 非法着点等
            out.append(VerifyResult(coord=move, error=str(exc)))
            continue
        root = resp.get("rootInfo") or {}
        infos = resp.get("moveInfos") or []
        opp_wr = root.get("winrate")
        best = next((m for m in infos if m.get("order") == 0), None)
        raw_pv = (best or {}).get("pv") or []
        pv = [("" if str(p).lower() == "pass" else str(p)) for p in raw_pv]
        bm = str((best or {}).get("move", ""))
        out.append(VerifyResult(
            coord=move,
            winrate=None if opp_wr is None else 1.0 - float(opp_wr),
            score_lead=(None if root.get("scoreLead") is None
                        else -float(root["scoreLead"])),
            visits=root.get("visits"),
            pv=[("" if move == "pass" else move)] + pv,
            best_coord="" if bm.lower() == "pass" else bm,
        ))
    return out


def _book_name(dir_name: str) -> str:
    return {"gokyoshumyo": "碁経衆妙", "guanzipu": "官子谱",
            "xxqj": "玄玄棋经", "hatsuyoron": "发阳论",
            "wangyou": "忘忧清乐集", "xuanlan": "玄览"}.get(dir_name, dir_name)


def make_seed_sgf(f: Path, r: dict) -> tuple[str, str, str]:
    """命中题 → (种子 SGF, 链名, 说明)。摆子局 + 白先奇偶修正。"""
    book = _book_name(f.parent.name)
    name = f"古典死活·{book}·{f.stem}"
    desc = (f"源自公版古典死活《{book}》{f.stem}：{r['solver']}先，"
            f"局部 {r['stones']} 子，正解 {r['best_coord']}"
            f"（正解-次优胜率差 {r['gap2']:.2f}）"
            + (f"；{r['race_text']}" if r["race"] else ""))
    src = sgf_io.parse_sgf(f.read_text(encoding="utf-8"))
    ab = "".join(f"[{sgf_io.coord_to_sgf(c, r['size'])}]"
                 for col, c in src.setup if c and col == "B")
    aw = "".join(f"[{sgf_io.coord_to_sgf(c, r['size'])}]"
                 for col, c in src.setup if c and col == "W")
    tail = (";B[" + chr(ord("a") + r["size"]) * 2 + "]"
            if r["solver"] == "W" else "")
    sgf = (f"(;GM[1]FF[4]CA[UTF-8]SZ[{r['size']}]KM[7.5]"
           f"C[{name}]GN[{name}]RE[mixed]GC[{desc}]"
           + (f"AB{ab}" if ab else "") + (f"AW{aw}" if aw else "")
           + tail + ")")
    return sgf, name, desc


def emit_hit(f: Path, r: dict, out_dir: Path | None = None,
             register: bool = False, grow_profile: str = "standard",
             grow_mode: str = "library") -> None:
    """命中即落盘（+ 可选注册与生长）——放在循环内，长跑中断也不丢。"""
    sgf, name, desc = make_seed_sgf(f, r)
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"classic-{f.parent.name}-{f.stem}.sgf"
        path.write_text(sgf, encoding="utf-8", newline="\n")
        print(f"[写出] {path}")
    if register:
        from backend.services.problems import chains as _chains
        cid = f"chain-classic-{f.parent.name}-{f.stem}"
        _chains.register_chain({"id": cid, "name": name, "theme": "mixed",
                                "root_sgf": sgf, "description": desc})
        try:
            res = _chains.grow_chain(cid, max_depth=2, max_per_level=3,
                                     profile=grow_profile, verify_mode=grow_mode)
            print(f"[注册+生长] {cid}：新增 {res['added']} 题"
                  f"（丢弃 {res['discarded']}）")
        except Exception as exc:  # noqa: BLE001
            print(f"[注册+生长] {cid} 生长失败：{exc}")


def evaluate(engine, sgf_text: str, profile: str) -> dict | None:
    """单个题面 → 各口径读数（不写库）。"""
    cfg = _cfg()
    pad = int(cfg.get("chain_crop_pad", 1))
    max_bbox = int(cfg.get("chain_max_bbox", 9))
    max_candidates = int(cfg.get("max_candidates", 12))
    region_pad = int(cfg.get("chain_region_pad", 2))
    urgent_max = float(cfg.get("urgency_max_winrate", 0.3))
    answer_min = float(cfg.get("chain_answer_min_winrate", 0.95))
    second_max = float(cfg.get("chain_second_max_winrate", 0.3))
    rel_gap = float(cfg.get("chain_relative_gap", 0.5))
    rel_urg = float(cfg.get("chain_relative_urgency", 0.3))

    parsed = sgf_io.parse_sgf(sgf_text)
    size = int(parsed.board_size)
    try:
        position = utils.apply_moves(
            chains.setup_stones(parsed, size), list(parsed.moves), size)
    except ValueError:
        return None
    if not position:
        return None
    kept = chains.crop_local(position, size, pad=pad, max_bbox=max_bbox)
    if kept is None:
        return None
    solver = chains._next_color(list(parsed.moves))
    setup = utils.setup_sgf(kept, solver, size)
    cands = chains.build_candidates(kept, size, max_candidates=max_candidates)
    region = chains.region_of(kept, size, region_pad)
    results = _verify_with(engine, setup, cands, region, profile)
    valid = sorted(
        [r for r in results
         if r.error is None and r.coord != "pass" and r.winrate is not None],
        key=lambda r: r.winrate or 0.0, reverse=True)
    if not valid:
        return None
    best = valid[0]
    second = valid[1] if len(valid) > 1 else None
    pass_wr = next((r.winrate for r in results
                    if r.coord == "pass" and r.winrate is not None), None)
    theme = utils.classify_theme(kept, position, size, 1, 0)
    urgent = theme in ("life_death", "capturing_race")
    best_wr = best.winrate or 0.0
    second_wr = (second.winrate or 0.0) if second else 0.0
    gap2 = best_wr - second_wr
    gap_pass = None if pass_wr is None else best_wr - pass_wr
    race = life_death.race_analysis(kept, size)
    return {
        "setup": setup, "size": size, "solver": solver, "theme": theme,
        "stones": len(kept), "bbox": len(kept),
        "best": best_wr, "best_coord": best.coord,
        "second": second_wr, "second_coord": second.coord if second else None,
        "pass": pass_wr, "gap2": round(gap2, 4),
        "gap_pass": None if gap_pass is None else round(gap_pass, 4),
        "winrate_ok": (best_wr > answer_min and second_wr < second_max
                       and (not urgent or (pass_wr is not None
                                           and pass_wr < urgent_max))),
        "relative_ok": (gap2 >= rel_gap
                        and (gap_pass is None or gap_pass >= rel_urg)),
        "race": bool(race.get("race")), "race_text": life_death.race_text(race),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_LIB))
    ap.add_argument("--limit", type=int, default=120, help="抽样题数（0=全部）")
    ap.add_argument("--stride", type=int, default=0, help="抽样步长（默认按 limit 均分）")
    ap.add_argument("--offset", type=int, default=0,
                    help="抽样起点偏移（分批跑用，避免与上一批重复）")
    ap.add_argument("--profile", default="fast")
    ap.add_argument("--out", default="", help="把命中题写成链种子 SGF 的目录")
    ap.add_argument("--state", default="data/tmp/find_state.json",
                    help="断点状态文件（长跑/分批续跑用；空串=不记）")
    ap.add_argument("--register", action="store_true",
                    help="命中即注册为链并生长（挂机用）")
    ap.add_argument("--grow-profile", default="standard",
                    help="注册后生长用的验题档位")
    ap.add_argument("--grow-mode", default="library",
                    help="注册后生长用的口径")
    ap.add_argument("--bar", default="relative",
                    choices=("strict", "relative", "library"),
                    help="命中判据：strict=契约(0.95/0.3)；relative=相对差值；"
                         "library=题库同尺（正解≥0.6 且 正解-次优≥0.15）")
    args = ap.parse_args()
    cfg = _cfg()
    rel_gap = float(cfg.get("chain_relative_gap", 0.5))

    files = sorted(Path(args.dir).glob("**/*.sgf"))
    if not files:
        print(f"[X] {args.dir} 下没有 SGF")
        return
    if args.offset:
        files = files[args.offset:]
    if args.limit and args.limit < len(files):
        stride = args.stride or max(1, len(files) // args.limit)
        files = files[::stride][: args.limit]
    print(f"体检 {len(files)} 题（档位 {args.profile}，判据 {args.bar}，"
          f"相对线 {rel_gap}）")

    state_path = Path(args.state) if args.state else None
    done: set[str] = set()
    if state_path and state_path.exists():
        try:
            done = set(json.loads(state_path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            done = set()
    if done:
        print(f"断点续跑：已完成 {len(done)} 题，跳过")

    def _flush() -> None:
        if state_path:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(sorted(done), ensure_ascii=False),
                encoding="utf-8", newline="\n",
            )

    out_dir = Path(args.out) if args.out else None
    exe, model, cfgp = _resolve_paths()
    engine = KataGoEngine(exe, model, cfgp, analysis_threads=1)
    engine.start()
    hits: list[tuple[Path, dict]] = []
    stats = {"total": 0, "rel": 0, "wr": 0, "race": 0}
    try:
        for i, f in enumerate(files):
            key = f"{f.parent.name}/{f.name}"
            if key in done:
                continue
            try:
                r = evaluate(engine, f.read_text(encoding="utf-8"), args.profile)
            except Exception as exc:  # noqa: BLE001
                print(f"  [skip] {f.name}: {exc}")
                continue
            if r is None:
                continue
            stats["total"] += 1
            stats["rel"] += 1 if r["relative_ok"] else 0
            stats["wr"] += 1 if r["winrate_ok"] else 0
            stats["race"] += 1 if r["race"] else 0
            r["library_ok"] = (r["best"] >= 0.6 and r["gap2"] >= 0.15)
            ok = {"strict": r["winrate_ok"], "relative": r["relative_ok"],
                  "library": r["library_ok"]}[args.bar]
            if ok:
                hits.append((f, r))
                emit_hit(f, r, out_dir, args.register,
                         args.grow_profile, args.grow_mode)
            done.add(key)
            if (i + 1) % 10 == 0:
                _flush()
            if (i + 1) % 25 == 0:
                print(f"  ..{i + 1}/{len(files)}  命中 {len(hits)}"
                      f"（已记录 {len(done)}）")
    finally:
        _flush()
        engine.stop()

    print(f"\n== 体检完成：{stats['total']} 题，相对判据命中 {stats['rel']}，"
          f"契约阈值命中 {stats['wr']}，含对杀 {stats['race']} ==")
    hits.sort(key=lambda t: -(t[1]["gap2"] or 0))
    for f, r in hits:
        print(f"[命中] {f.parent.name}/{f.name:16s} {r['solver']}先 {r['theme']:14s}"
              f" 子={r['stones']:2d} 正解={r['best_coord']}:{r['best']:.3f}"
              f" 次优={r['second_coord']}:{r['second']:.3f} 差={r['gap2']:.3f}"
              f" 脱先差={r['gap_pass'] if r['gap_pass'] is None else round(r['gap_pass'], 3)}"
              f"{' 对杀' if r['race'] else ''}")
    if not hits:
        print("没有命中：可降低 --limit 之外的判据（problems.chain_relative_gap）再试，"
              "或换题库目录。")


if __name__ == "__main__":
    main()
