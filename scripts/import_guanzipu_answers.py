"""官子谱答案模式批量入库（后台可挂机跑）。

策略（与"唯一正解验题"不同，原因见 README）：
- 古典题摆子图极小，全局胜率无法判定局部唯一性（多点 100%），
  0.95/0.3 唯一性验题对古典题系统性不适用；
- 官子谱原书含答案（主线演示序列），采用"原书答案 + 引擎校验"：
  1) 答案 = 主线首手，行棋方 = PL（PL 与首手颜色一致才采纳，
     不一致的题跳过，宁缺毋滥）；
  2) 校验：verify_position 验证答案点（求解方视角胜率 ≥ 0.6），
     确认题面没有摆错、答案被引擎认同；
  3) 入库 source=imported，branches 保存答案与校验数据，
     practice 判题时仍由 checker 引擎实时判定。
- 每题 1 个候选 query，约 3~5s/题；1476 题约 1.5~2 小时。

用法：.venv\\Scripts\\python.exe scripts\\import_guanzipu_answers.py
     [--limit N] [--profile standard|fine]
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

from backend.common import sgf_io  # noqa: E402
from backend.services.problems import store, utils  # noqa: E402
from backend.services.engine.verify import verify_position  # noqa: E402

SRC = ROOT / "data" / "library" / "_guanzipu_extract" / "guanzipu"

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


def _existing_ids(db_path=None) -> set:
    """库中已存在的题目 id（断点续跑：已入库的题跳过验题）。"""
    import sqlite3

    from backend.common import db as db_mod

    try:
        conn = sqlite3.connect(str(db_path or db_mod.DB_PATH))
        try:
            return {r[0] for r in conn.execute("SELECT id FROM problems")}
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return set()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--profile", default="standard", choices=("fast", "standard", "fine"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--src",
        default=str(SRC),
        help="题目目录（默认官子谱原始目录）",
    )
    ap.add_argument(
        "--label",
        default=None,
        help="题面标签前缀；给定后忽略 SGF 内 GN 标签（如：--label 玄览）",
    )
    args = ap.parse_args(argv)
    src_dir = Path(args.src)

    existing = _existing_ids()
    if existing:
        print(f"[import] 断点续跑：库中已有 {len(existing)} 题，将跳过验题")

    files = sorted(
        src_dir.glob("*.sgf"),
        key=lambda p: int(p.stem) if p.stem.isdigit() else -1,
    )
    ok = bad = 0
    t0 = time.time()
    for i, path in enumerate(files):
        if args.limit and i >= args.limit:
            break
        text = read_text(path)
        if not re.search(RE_SETUP, text, re.IGNORECASE):
            bad += 1
            continue
        first = re.search(RE_FIRST, text)
        pl = re.search(RE_PL, text)
        if not first:
            bad += 1
            continue
        ans_color = first.group(1).upper()
        ans_sgf = first.group(2)
        # 绝大多数题无 PL（仅第 1 题等个别有）；PL 存在且与主线首手
        # 颜色冲突的题跳过（宁缺毋滥），其余以主线首手颜色为答案方。
        if pl is not None and pl.group(1).upper() != ans_color:
            bad += 1
            continue
        pos = build_position(text, 19)
        if not pos:
            bad += 1
            continue
        ans_coord = sgf_io.sgf_to_coord(ans_sgf, 19)
        if not ans_coord:
            bad += 1
            continue
        gn = re.search(RE_GN, text)
        label = (
            f"{args.label}第{path.stem}题"
            if args.label
            else (gn.group(1).strip() if gn else f"第{path.stem}题")
        )
        setup_sgf = utils.setup_sgf(pos, ans_color, 19)
        try:
            rs = verify_position(setup_sgf, [ans_coord], profile=args.profile)
        except Exception as exc:
            print(f"[skip] {path.stem}: 引擎异常 {exc}")
            bad += 1
            continue
        r = rs[0] if rs else None
        if r is None or r.error is not None or r.winrate is None:
            bad += 1
            continue
        if r.winrate < MIN_ANSWER_WINRATE:
            print(f"[skip] {path.stem}: 答案 {ans_coord} 胜率 {r.winrate:.0%} "
                  f"< {MIN_ANSWER_WINRATE:.0%}（题面可疑，跳过）")
            bad += 1
            continue
        cn_color = "白先" if ans_color == "W" else "黑先"
        problem_id = utils.problem_id(setup_sgf, "life_death")
        if problem_id in existing:
            ok += 1  # 断点续跑：已入库，跳过验题
            continue
        branches = {
            "solver": ans_color,
            "answer": {
                "coord": r.coord, "winrate": r.winrate,
                "score_lead": r.score_lead, "visits": r.visits,
                "pv": list(r.pv), "best_coord": r.best_coord, "error": r.error,
            },
            "source_answer": f"{ans_color} {ans_coord}（原书主线首手）",
            "profile": args.profile,
            "verified_at": store.utcnow(),
        }
        hint = f"{cn_color}，{utils.region_label(ans_coord, 19)}的古典死活题，请找出最佳一手。"
        rank_min, rank_max = utils.default_rank_range("life_death")
        row = {
            "id": problem_id,
            "source": "imported",
            "review_id": None,
            "theme": "life_death",
            "rank_min": rank_min,
            "rank_max": rank_max,
            "setup_sgf": setup_sgf,
            "answer": r.coord,
            "branches": json.dumps(branches, ensure_ascii=False),
            "verdict": utils.verdict_text(
                "life_death", ans_color, r.coord, r.winrate or 0.0, None, None
            ),
            "hint": hint,
            "explanation": None,
            "status": "active",
            "created_at": store.utcnow(),
        }
        if not args.dry_run:
            if store.insert_problem(row):
                ok += 1
                print(f"[ok]   {path.stem:>5}: {label} "
                      f"{ans_color}→{ans_coord} wr={r.winrate:.3f}")
            else:
                bad += 1
        else:
            ok += 1
            print(f"[dry]  {path.stem:>5}: {label} "
                  f"{ans_color}→{ans_coord} wr={r.winrate:.3f}")
    print(f"\n[import] 导入完成：入库 {ok}，跳过 {bad}，"
          f"耗时 {time.time() - t0:.1f}s"
          + ("（dry-run）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
