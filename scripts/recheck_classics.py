"""古典题全量重算（局部聚焦口径）：更新已入库、捡回被误筛、移除口径不符。

与 import_classic_answers.py 同一验题管线（allowMoves 局部聚焦），
区别：不跳过已入库题——每题强制重算：
- 胜率 ≥ --min-winrate：已入库则 UPDATE，未入库则 INSERT（捡回）；
- 胜率 < --min-winrate：已入库则 DELETE（旧全盘口径误收的移除）。

断点续跑：data/library/_recheck_state.json 记录已处理文件。

用法：
    python scripts/recheck_classics.py --src <目录> --label 玄玄棋经 --auto
    python scripts/recheck_classics.py --src <目录> --label 官子谱
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backend.common import sgf_io  # noqa: E402
from backend.services.problems import store, utils  # noqa: E402
from backend.services.engine.analyze_sgf import _resolve_paths  # noqa: E402
from backend.services.engine.engine import KataGoEngine  # noqa: E402
from import_classic_answers import (  # noqa: E402
    RE_SETUP, build_position, query_turn, read_text, region_of,
    solver_of,
)


def rank_for(number: int, total: int) -> tuple[int, int]:
    """书内编号 → 适用级位三分位（与导入器一致）。"""
    if total <= 0 or number <= total / 3:
        return (-15, -8)
    if number <= total * 2 / 3:
        return (-7, -2)
    return (1, 3)

STATE_FILE = ROOT / "data" / "library" / "_recheck_state.json"
MIN_ANSWER_WINRATE = 0.6


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def problem_exists(pid: str) -> bool:
    from backend.common import db as db_mod

    conn = db_mod.connect()
    try:
        return conn.execute(
            "SELECT 1 FROM problems WHERE id=?", (pid,)
        ).fetchone() is not None
    finally:
        conn.close()


def remove_problem(pid: str) -> None:
    from backend.common import db as db_mod

    conn = db_mod.connect()
    try:
        conn.execute("DELETE FROM problems WHERE id=?", (pid,))
        conn.commit()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="题目 SGF 目录")
    ap.add_argument("--label", default="古典题", help="题面标签前缀")
    ap.add_argument("--auto", action="store_true", help="题面无答案：一选作答案")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--profile", default="standard",
                    choices=("fast", "standard", "fine"))
    ap.add_argument("--min-winrate", type=float, default=MIN_ANSWER_WINRATE)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    src_dir = Path(args.src)
    state = load_state()
    keypfx = f"{args.label}:{src_dir.name}:"

    files = sorted(
        src_dir.glob("*.sgf"),
        key=lambda p: int(p.stem) if p.stem.isdigit() else -1,
    )
    book_total = len(files)
    if args.limit:
        files = files[: args.limit]

    exe, model, cfg = _resolve_paths()
    engine = KataGoEngine(exe, model, cfg, analysis_threads=1)
    engine.start()
    n_new = n_upd = n_rm = n_skip = n_done = 0
    t0 = time.time()
    try:
        for path in files:
            key = keypfx + path.stem
            if key in state:
                n_done += 1
                continue
            text = read_text(path)
            if not re.search(RE_SETUP, text, re.IGNORECASE):
                state[key] = True
                continue
            size = sgf_io.parse_sgf(text).board_size or 19
            pos = build_position(text, size)
            if not pos:
                state[key] = True
                continue

            if args.auto:
                ans_color = solver_of(text, size)
                resp = query_turn(engine, text, args.profile,
                                  region=region_of(pos, size))
                if resp is None or resp.get("_error"):
                    print(f"[err] {path.stem}: {resp and resp.get('_error')}")
                    state[key] = True
                    continue
                infos = resp.get("moveInfos") or []
                best = next((m for m in infos if m.get("order") == 0), None)
                if not best or not best.get("move"):
                    state[key] = True
                    continue
                ans_coord = str(best["move"]).upper()
                wr_raw = best.get("winrate")
                ans_wr = float(wr_raw) if wr_raw is not None else None
                pv = [ans_coord] + [str(m) for m in (best.get("pv") or [])]
                source_answer = f"KataGo 一选 {ans_coord}（{args.profile} 档·局部聚焦）"
            else:
                first = re.search(r";\s*([BW])\s*\[\s*([a-zA-Z]{2})\s*\]", text)
                pl = re.search(r"PL\s*\[\s*([BW])\s*\]", text)
                if not first:
                    state[key] = True
                    continue
                ans_color = first.group(1).upper()
                if pl is not None and pl.group(1).upper() != ans_color:
                    state[key] = True
                    continue
                ans_coord = sgf_io.sgf_to_coord(first.group(2), size)
                if not ans_coord:
                    state[key] = True
                    continue
                setup_sgf = utils.setup_sgf(pos, ans_color, size)
                region = region_of(pos, size)
                if ans_coord not in region:
                    region = region + [ans_coord]  # 答案点必须在允许区域内
                resp = query_turn(engine, setup_sgf, args.profile,
                                  extra=(ans_color, ans_coord),
                                  region=region)
                if resp is None or resp.get("_error"):
                    print(f"[err] {path.stem}: {resp and resp.get('_error')}")
                    state[key] = True
                    continue
                root = resp.get("rootInfo") or {}
                wr_raw = root.get("winrate")
                ans_wr = (1 - float(wr_raw)) if wr_raw is not None else None
                pv = [ans_coord] + [str(m) for m in (root.get("pv") or [])]
                source_answer = f"{ans_color} {ans_coord}（原书主线首手·局部聚焦）"

            setup_sgf = utils.setup_sgf(pos, ans_color, size)
            pid = utils.problem_id(setup_sgf, "life_death")
            exists = problem_exists(pid)

            if ans_wr is not None and ans_wr >= args.min_winrate:
                num = int(path.stem) if path.stem.isdigit() else 0
                rmin, rmax = rank_for(num, book_total)
                cn_color = "白先" if ans_color == "W" else "黑先"
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
                verdict = utils.verdict_text(
                    "life_death", ans_color, ans_coord, ans_wr, None, None
                )
                if exists:
                    if not args.dry_run:
                        store.update_problem(pid, {
                            "answer": ans_coord,
                            "branches": json.dumps(branches, ensure_ascii=False),
                            "verdict": verdict,
                            "hint": hint,
                            "rank_min": rmin,
                            "rank_max": rmax,
                        })
                    n_upd += 1
                    print(f"[upd] {path.stem:>8}: {args.label}第{path.stem}题 "
                          f"{ans_color}→{ans_coord} wr={ans_wr:.3f}")
                else:
                    if not args.dry_run:
                        store.insert_problem({
                            "id": pid,
                            "source": "imported",
                            "review_id": None,
                            "theme": "life_death",
                            "rank_min": rmin,
                            "rank_max": rmax,
                            "setup_sgf": setup_sgf,
                            "answer": ans_coord,
                            "branches": json.dumps(branches, ensure_ascii=False),
                            "verdict": verdict,
                            "hint": hint,
                            "explanation": None,
                            "status": "active",
                            "created_at": store.utcnow(),
                        })
                    n_new += 1
                    print(f"[new] {path.stem:>8}: 捡回 {args.label}第{path.stem}题 "
                          f"{ans_color}→{ans_coord} wr={ans_wr:.3f}")
            else:
                if exists:
                    if not args.dry_run:
                        remove_problem(pid)
                    n_rm += 1
                    print(f"[del] {path.stem:>8}: 口径不符移除 wr="
                          f"{ans_wr if ans_wr is None else ans_wr:.3f}")
                else:
                    n_skip += 1
            state[key] = True
            if (n_new + n_upd + n_rm + n_skip) % 20 == 0:
                save_state(state)
        save_state(state)
        print(f"\n[recheck] {args.label} 完成：新增 {n_new}，更新 {n_upd}，"
              f"移除 {n_rm}，丢弃 {n_skip}，已处理 {n_done}，"
              f"耗时 {time.time() - t0:.1f}s"
              + ("（dry-run）" if args.dry_run else ""))
        return 0
    finally:
        engine.stop()


if __name__ == "__main__":
    sys.exit(main())
