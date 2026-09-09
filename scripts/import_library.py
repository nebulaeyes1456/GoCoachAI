"""批量导入题库脚本（窗口3）。

解析 ``data/library/*.sgf`` 并按约定入库（格式详见 ``data/library/README.md``）：

- 根节点 ``GM[1]``、``SZ[9|13|19]``；``RE[主题]``（life_death /
  capturing_race / endgame / middle）；``PB[题面描述]``、``PW[答案提示]``；
- 题面 = 根节点 AB/AW 摆子 + 主线全部着法；**终局前最后一手所在方为
  出题方**（即题面摆完后轮到对方行棋，是求解方）；
- 正解不写在文件里：入库时调 ``verify_position`` 在候选点中验证——
  正解胜率 > ``--min-winrate``（默认 0.95）且次优点 < ``--max-second``
  （默认 0.3），否则跳过并报告原因（容错：单文件失败不影响其他文件）；
- 来源合规：只能导入用户自有的合法 SGF（见 README 合规说明）。

用法：
    python scripts/import_library.py                 # 导入 data/library/
    python scripts/import_library.py --dry-run       # 只验证不入库
    python scripts/import_library.py --profile fine --limit 20
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common import sgf_io  # noqa: E402
from backend.services.problems import store, utils  # noqa: E402
from backend.services.engine.verify import verify_position  # noqa: E402

LIBRARY_DIR = ROOT / "data" / "library"
ACCEPTED_SIZES = (9, 13, 19)

# 验题阈值（分主题）：战术题（死活/对杀）按契约 razor 条件；
# 官子/中盘题属局面价值判断，正解胜率天然不高（0.5~0.9），放宽阈值。
# 契约 §4.3 的 0.95/0.3 红线仅针对 generated 来源；导入题自定标准（README 已写明）。
THEME_THRESHOLDS = {
    "life_death": (0.95, 0.3),
    "capturing_race": (0.95, 0.3),
    "endgame": (0.6, 0.5),
    "middle": (0.6, 0.5),
}

VerifyFn = Callable[..., list]


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _root_prop(sgf_text: str, prop: str) -> str:
    m = re.search(rf"{prop}\s*\[([^\]]*)\]", sgf_text or "", re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_library_sgf(sgf_text: str) -> tuple[Optional[dict], Optional[str]]:
    """解析题库 SGF → (info, 跳过原因)。

    info: {theme, hint, size, solver, setup_sgf, last_move, stones}
    """
    text = sgf_text or ""
    if not text.strip().startswith("("):
        return None, "不是合法 SGF（缺少 '('）"
    gm_m = re.search(r"GM\s*\[\s*(\d+)", text, re.IGNORECASE)
    if gm_m is None:
        return None, "缺少 GM[1]（非围棋题谱）"
    if int(gm_m.group(1)) != 1:
        return None, f"GM[{gm_m.group(1)}] 非围棋棋谱（要求 GM[1]）"
    if not re.search(r"SZ\s*\[\s*\d+", text, re.IGNORECASE):
        return None, "缺少 SZ（棋盘大小）"
    theme = _root_prop(text, "RE").lower()
    if theme not in utils.VALID_THEMES:
        return None, f"RE 主题非法或缺失: {_root_prop(text, 'RE')!r}"

    parsed = sgf_io.parse_sgf(text)
    size = parsed.board_size
    if size not in ACCEPTED_SIZES:
        return None, f"SZ[{size}] 不受支持（仅 {ACCEPTED_SIZES}）"

    # 终局前最后一手所在方为出题方 → 求解方为其对手；纯摆子局默认黑先
    last_color = parsed.moves[-1][0] if parsed.moves else ""
    solver = "W" if last_color == "B" else ("B" if last_color == "W" else "B")

    # 题面 = AB/AW + 主线全部着法重放（AB[aa][bb]... 连续多摆子逐枚提取）
    position = utils.replay_position(parsed.moves, size)
    for prop, color in (("AB", "B"), ("AW", "W")):
        for block in re.finditer(
            rf"{prop}((?:\[[a-zA-Z]*\])+)", text, re.IGNORECASE
        ):
            for m in re.finditer(r"\[([a-zA-Z]*)\]", block.group(1)):
                coord = sgf_io.sgf_to_coord(m.group(1), size)
                if not coord:
                    return None, f"{prop} 含非法摆子: {m.group(1)!r}"
                x, y = utils.coord_to_xy(coord)
                if (x, y) in position:
                    return None, f"摆子与主线冲突: {coord}"
                position[(x, y)] = color
    if not position:
        return None, "题面为空（无摆子也无主线着法）"

    hint = _root_prop(text, "PW")
    statement = _root_prop(text, "PB")
    if statement:
        hint = f"{statement}；{hint}" if hint else statement
    if not hint:
        hint = utils.hint_text(
            theme, solver, parsed.moves[-1][1] or "", size
        ) if parsed.moves else utils.hint_text(theme, solver, "", size)

    return {
        "theme": theme,
        "hint": hint,
        "size": size,
        "solver": solver,
        "position": position,
        "last_move": parsed.moves[-1][1] if parsed.moves else "",
    }, None


# ---------------------------------------------------------------------------
# 验证与入库
# ---------------------------------------------------------------------------

def _candidates(info: dict, radius: int = 2, cap: int = 12) -> list[str]:
    """候选点：最后一手（出题方）周围的空点；无主线时取摆子外圈。"""
    size, position = info["size"], info["position"]
    center = None
    if info["last_move"]:
        try:
            center = utils.coord_to_xy(info["last_move"])
        except ValueError:
            center = None
    cands: set[str] = set()
    if center is None:
        # 摆子局：摆子外扩 1 圈的所有空点
        for (x, y), _c in position.items():
            for nx, ny in utils.neighbors(x, y, size):
                if (nx, ny) not in position:
                    cands.add(utils.xy_to_coord(nx, ny, size))
    else:
        cx, cy = center
        for x in range(max(0, cx - radius), min(size - 1, cx + radius) + 1):
            for y in range(max(0, cy - radius), min(size - 1, cy + radius) + 1):
                if (x, y) not in position:
                    cands.add(utils.xy_to_coord(x, y, size))
    if len(cands) > cap:
        cands = set(sorted(cands)[:cap])
    return sorted(cands)


def import_one(
    sgf_text: str,
    verify_fn: VerifyFn = verify_position,
    profile: str = "standard",
    min_winrate: Optional[float] = None,
    max_second: Optional[float] = None,
    dry_run: bool = False,
    db_path=None,
) -> tuple[Optional[dict], Optional[str]]:
    """导入一道题 → (problem_brief, 跳过原因)。

    min_winrate/max_second 不传时按主题取 THEME_THRESHOLDS：
    死活/对杀用契约 razor 条件（0.95/0.3），官子/中盘放宽（0.6/0.5）。
    """
    info, reason = parse_library_sgf(sgf_text)
    if info is None:
        return None, reason
    theme = info["theme"]
    t_min, t_max = THEME_THRESHOLDS.get(theme, (0.95, 0.3))
    min_winrate = t_min if min_winrate is None else min_winrate
    max_second = t_max if max_second is None else max_second
    setup_sgf = utils.setup_sgf(info["position"], info["solver"], info["size"])
    cands = _candidates(info)
    if not cands:
        return None, "无可验证候选点"
    cands.append("pass")  # 紧迫性参考（不参与正解评选）
    results = verify_fn(setup_sgf, cands, profile=profile)
    valid = [
        r for r in results
        if r.error is None and r.coord != "pass" and r.winrate is not None
    ]
    valid.sort(key=lambda r: r.winrate or 0.0, reverse=True)
    if not valid:
        return None, "无有效候选点（全部非法/失败）"
    best, second = valid[0], valid[1] if len(valid) > 1 else None
    if (best.winrate or 0.0) <= min_winrate:
        return None, f"正解 {best.coord} 胜率 {(best.winrate or 0):.0%} ≤ {min_winrate:.0%}"
    if second is not None and (second.winrate or 0.0) >= max_second:
        return None, (
            f"次优点 {second.coord} 胜率 {(second.winrate or 0):.0%} ≥ {max_second:.0%}，"
            f"正解不唯一"
        )

    rank_min, rank_max = utils.default_rank_range(info["theme"])
    branches = {
        "solver": info["solver"],
        "answer": {k: getattr(best, k) for k in
                   ("coord", "winrate", "score_lead", "visits", "pv", "best_coord", "error")},
        "candidates": [
            {k: getattr(r, k) for k in
             ("coord", "winrate", "score_lead", "visits", "pv", "best_coord", "error")}
            for r in results
        ],
        "profile": profile,
        "verified_at": store.utcnow(),
    }
    row = {
        "id": utils.problem_id(setup_sgf, info["theme"]),
        "source": "imported",
        "review_id": None,
        "theme": info["theme"],
        "rank_min": rank_min,
        "rank_max": rank_max,
        "setup_sgf": setup_sgf,
        "answer": best.coord,
        "branches": json.dumps(branches, ensure_ascii=False),
        "verdict": utils.verdict_text(
            info["theme"], info["solver"], best.coord,
            best.winrate or 0.0,
            second.coord if second else None,
            second.winrate if second else None,
        ),
        "hint": info["hint"],
        "explanation": None,
        "status": "active",
        "created_at": store.utcnow(),
    }
    if not dry_run:
        inserted = store.insert_problem(row, db_path)
        if not inserted:
            return None, f"已存在（id={row['id']}，幂等跳过）"
    return {
        "id": row["id"],
        "theme": row["theme"],
        "rank_min": row["rank_min"],
        "rank_max": row["rank_max"],
        "setup_sgf": row["setup_sgf"],
        "hint": row["hint"],
    }, None


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="导入 data/library/*.sgf 题库")
    ap.add_argument("--dir", default=str(LIBRARY_DIR))
    ap.add_argument("--profile", default="standard",
                    choices=("fast", "standard", "fine"))
    ap.add_argument("--min-winrate", type=float, default=None,
                    help="正解胜率下限（默认按主题：战术题 0.95，官子/中盘 0.6）")
    ap.add_argument("--max-second", type=float, default=None,
                    help="次优点胜率上限（默认按主题：战术题 0.3，官子/中盘 0.5）")
    ap.add_argument("--limit", type=int, default=0, help="最多导入多少文件（0=不限）")
    ap.add_argument("--dry-run", action="store_true", help="只验证不入库")
    args = ap.parse_args(argv)

    lib_dir = Path(args.dir)
    files = sorted(lib_dir.glob("*.sgf"))
    if not files:
        print(f"[import] 无 .sgf 文件: {lib_dir}")
        return 1

    ok = skipped = 0
    t0 = time.time()
    for i, path in enumerate(files):
        if args.limit and i >= args.limit:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"[skip] {path.name}: 读取失败 {exc}")
            skipped += 1
            continue
        try:
            brief, reason = import_one(
                text, profile=args.profile,
                min_winrate=args.min_winrate, max_second=args.max_second,
                dry_run=args.dry_run,
            )
        except Exception as exc:  # 引擎不可用等：容错跳过
            print(f"[skip] {path.name}: 异常 {exc}")
            skipped += 1
            continue
        if brief is None:
            print(f"[skip] {path.name}: {reason}")
            skipped += 1
            continue
        ok += 1
        print(f"[ok]   {path.name} → {brief['id']} ({brief['theme']}, "
              f"{brief['rank_min']}~{brief['rank_max']})")
    print(f"\n[import] 完成：成功 {ok}，跳过 {skipped}，"
          f"耗时 {time.time() - t0:.1f}s"
          + ("（dry-run，未入库）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
