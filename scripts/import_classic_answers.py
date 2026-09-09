"""古典题答案模式通用导入器（长驻引擎，挂机友好）。

模式一（默认·原书答案）：SGF 主线首手=答案 → 验证答案点落子后
    求解方胜率 ≥ --min-winrate（默认 0.6）则入库。
模式二（--auto·KataGo 一选）：题面无答案（tasuki 三书 ;B[tt]/;W[tt]
    修奇偶格式）→ 对题面跑一次分析，取一选（order=0）作答案，
    胜率 ≥ --min-winrate 则入库。

引擎进程只启动一次，每题 1 条 query；断点续跑（已入库 id 跳过）。

用法：
    python scripts/import_classic_answers.py --src <目录> --label 玄玄棋经
    python scripts/import_classic_answers.py --src <目录> --label 碁经众妙 --auto
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common import sgf_io  # noqa: E402
from backend.services.problems import store, utils  # noqa: E402
from backend.services.engine.analyze_sgf import _game_to_query, _resolve_paths  # noqa: E402
from backend.services.engine.engine import KataGoEngine  # noqa: E402

RE_SETUP = r"(AB|AW)((?:\[[a-zA-Z]*\])+)"
RE_PL = r"PL\s*\[\s*([BW])\s*\]"
RE_FIRST = r";\s*([BW])\s*\[\s*([a-zA-Z]{2})\s*\]"
RE_GN = r"GN\s*\[([^\]]*)\]"

MIN_ANSWER_WINRATE = 0.6


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def build_position(text: str, size: int) -> dict:
    pos: dict = {}
    for pm in re.finditer(RE_SETUP, text, re.IGNORECASE):
        kind, block = pm.group(1).upper(), pm.group(2)
        for x in re.findall(r"\[([a-zA-Z]*)\]", block):
            if not x:
                continue
            c = sgf_io.sgf_to_coord(x, size)
            if c:
                px, py = utils.coord_to_xy(c)
                pos[(px, py)] = "B" if kind == "AB" else "W"
    return pos


def existing_ids() -> set:
    import sqlite3

    from backend.common import db as db_mod

    try:
        conn = sqlite3.connect(str(db_mod.DB_PATH))
        try:
            return {r[0] for r in conn.execute("SELECT id FROM problems")}
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return set()


REGION_MARGIN = 2  # 题面包围盒外扩格数（死活变化极少超出 2 格）


def region_of(pos: dict, size: int, margin: int = REGION_MARGIN) -> list[str]:
    """题面摆子包围盒外扩 margin 的矩形区域（界面坐标列表）。

    用于 query.allowMoves：把 KataGo 的搜索树限制在局部厮杀区域，
    避免把算力浪费在全盘大场（古典死活题全是局部题）。
    """
    if not pos:
        return []
    xs = [p[0] for p in pos]
    ys = [p[1] for p in pos]
    x0, x1 = max(0, min(xs) - margin), min(size - 1, max(xs) + margin)
    y0, y1 = max(0, min(ys) - margin), min(size - 1, max(ys) + margin)
    return [
        utils.xy_to_coord(x, y, size)
        for x in range(x0, x1 + 1)
        for y in range(y0, y1 + 1)
    ]


def query_turn(engine: KataGoEngine, sgf_text: str, profile: str,
               extra: Optional[tuple[str, str]] = None,
               region: Optional[list[str]] = None) -> Optional[dict]:
    """对局面跑一次分析；extra=(color, coord) 时追加一手后分析。

    region：局部区域坐标列表，非空时以 allowMoves 限制双方落子范围。
    返回 {"root": {...}, "moveInfos": [...]}（原始响应）。
    """
    parsed = sgf_io.parse_sgf(sgf_text)
    base_moves = [[c, "pass" if not p else p] for c, p in parsed.moves]
    if extra:
        color, coord = extra
        base_moves = [list(m) for m in base_moves] + [[color, coord]]
    n = len(base_moves)
    req = _game_to_query(sgf_text, profile, [n])
    req["moves"] = base_moves
    if region:
        req["allowMoves"] = [
            {"player": "B", "moves": region, "untilDepth": 100},
            {"player": "W", "moves": region, "untilDepth": 100},
        ]
    try:
        return engine.query(req)
    except Exception as exc:  # 引擎异常：返回错误标记，不中断全量
        return {"_error": str(exc)}


def solver_of(text: str, size: int) -> str:
    """题面后轮到谁行棋（tasuki 格式：末手 pass 修奇偶）。"""
    parsed = sgf_io.parse_sgf(text)
    last = parsed.moves[-1][0] if parsed.moves else ""
    return "W" if last == "B" else ("B" if last == "W" else "B")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="题目 SGF 目录")
    ap.add_argument("--label", default="古典题", help="题面标签前缀")
    ap.add_argument("--auto", action="store_true",
                    help="题面无答案：KataGo 一选作答案")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--profile", default="standard",
                    choices=("fast", "standard", "fine"))
    ap.add_argument("--min-winrate", type=float, default=MIN_ANSWER_WINRATE)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    src_dir = Path(args.src)
    done = existing_ids()
    print(f"[import] 断点续跑：库中已有 {len(done)} 题")

    files = sorted(
        src_dir.glob("*.sgf"),
        key=lambda p: int(p.stem) if p.stem.isdigit() else -1,
    )
    book_total = len(files)
    if args.limit:
        files = files[: args.limit]

    def rank_for(number: int) -> tuple[int, int]:
        """书内编号 → 适用级位三分位（古典题集原书顺序从易到难）。"""
        if book_total <= 0 or number <= book_total / 3:
            return (-15, -8)
        if number <= book_total * 2 / 3:
            return (-7, -2)
        return (1, 3)

    executable, model, cfg_path = _resolve_paths()
    engine = KataGoEngine(executable, model, cfg_path, analysis_threads=1)
    engine.start()
    ok = bad = 0
    t0 = time.time()
    try:
        for path in files:
            text = read_text(path)
            if not re.search(RE_SETUP, text, re.IGNORECASE):
                bad += 1
                continue
            size = sgf_io.parse_sgf(text).board_size or 19
            pos = build_position(text, size)
            if not pos:
                bad += 1
                continue

            if args.auto:
                ans_color = solver_of(text, size)
                resp = query_turn(engine, text, args.profile,
                                  region=region_of(pos, size))
                if resp is None or resp.get("_error"):
                    print(f"[skip] {path.stem}: 引擎异常 {resp and resp.get('_error')}")
                    bad += 1
                    continue
                infos = resp.get("moveInfos") or []
                best = next((m for m in infos if m.get("order") == 0), None)
                if not best or not best.get("move"):
                    bad += 1
                    continue
                ans_coord = str(best["move"]).upper()
                wr_raw = best.get("winrate")
                # moveInfos 胜率=落子方视角（SIDETOMOVE），直接取
                ans_wr = float(wr_raw) if wr_raw is not None else None
                pv = [ans_coord] + [str(m) for m in (best.get("pv") or [])]
                source_answer = f"KataGo 一选 {ans_coord}（{args.profile} 档）"
            else:
                first = re.search(RE_FIRST, text)
                pl = re.search(RE_PL, text)
                if not first:
                    bad += 1
                    continue
                ans_color = first.group(1).upper()
                ans_sgf = first.group(2)
                if pl is not None and pl.group(1).upper() != ans_color:
                    bad += 1
                    continue
                ans_coord = sgf_io.sgf_to_coord(ans_sgf, size)
                if not ans_coord:
                    bad += 1
                    continue
                setup_sgf = utils.setup_sgf(pos, ans_color, size)
                region = region_of(pos, size)
                if ans_coord not in region:
                    region = region + [ans_coord]  # 答案点必须在允许区域内
                resp = query_turn(engine, setup_sgf, args.profile,
                                  extra=(ans_color, ans_coord),
                                  region=region)
                if resp is None or resp.get("_error"):
                    print(f"[skip] {path.stem}: 引擎异常 {resp and resp.get('_error')}")
                    bad += 1
                    continue
                root = resp.get("rootInfo") or {}
                wr_raw = root.get("winrate")
                ans_wr = (1 - float(wr_raw)) if wr_raw is not None else None
                pv = [ans_coord] + [str(m) for m in (root.get("pv") or [])]
                source_answer = f"{ans_color} {ans_coord}（原书主线首手）"

            if ans_wr is None or ans_wr < args.min_winrate:
                print(f"[skip] {path.stem}: 答案 {ans_coord} 胜率 "
                      f"{ans_wr if ans_wr is None else ans_wr:.0%} "
                      f"< {args.min_winrate:.0%}")
                bad += 1
                continue

            setup_sgf = utils.setup_sgf(pos, ans_color, size)
            problem_id = utils.problem_id(setup_sgf, "life_death")
            if problem_id in done:
                ok += 1  # 已入库，跳过验题
                continue
            cn_color = "白先" if ans_color == "W" else "黑先"
            num = int(path.stem) if path.stem.isdigit() else 0
            branches = {
                "book": args.label,
                "number": num,
                "book_total": book_total,
                "solver": ans_color,
                "answer": {
                    "coord": ans_coord, "winrate": ans_wr,
                    "score_lead": None, "visits": None,
                    "pv": pv, "best_coord": None, "error": None,
                },
                "source_answer": source_answer,
                "profile": args.profile,
                "verified_at": store.utcnow(),
            }
            hint = f"{cn_color}，{utils.region_label(ans_coord, size)}的古典死活题，请找出最佳一手。"
            rank_min, rank_max = rank_for(num)
            row = {
                "id": problem_id,
                "source": "imported",
                "review_id": None,
                "theme": "life_death",
                "rank_min": rank_min,
                "rank_max": rank_max,
                "setup_sgf": setup_sgf,
                "answer": ans_coord,
                "branches": json.dumps(branches, ensure_ascii=False),
                "verdict": utils.verdict_text(
                    "life_death", ans_color, ans_coord, ans_wr, None, None
                ),
                "hint": hint,
                "explanation": None,
                "status": "active",
                "created_at": store.utcnow(),
            }
            if not args.dry_run:
                if store.insert_problem(row):
                    ok += 1
                    print(f"[ok]  {path.stem:>8}: {args.label}第{path.stem}题 "
                          f"{ans_color}→{ans_coord} wr={ans_wr:.3f}")
                else:
                    bad += 1
            else:
                ok += 1
                print(f"[dry] {path.stem:>8}: {args.label}第{path.stem}题 "
                      f"{ans_color}→{ans_coord} wr={ans_wr:.3f}")
        print(f"\n[import] {args.label} 完成：入库 {ok}，跳过 {bad}，"
              f"耗时 {time.time() - t0:.1f}s"
              + ("（dry-run）" if args.dry_run else ""))
        return 0
    finally:
        engine.stop()


if __name__ == "__main__":
    sys.exit(main())
