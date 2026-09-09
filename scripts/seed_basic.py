"""基础死活题种子生成脚本（窗口3）。

用 ``verify_position`` 批量**程序生成**基础题（随机局部 + 验证），
目标入库 ≥ 30 道可用基础题（验收标准之一）。

生成策略（全部原创、程序随机，不含任何受版权保护的题集）：
- 在 9 路棋盘随机搜索"相邻互相打吃"局部：黑、白两条 4~6 子棋串各仅 1 气
  （且任一方提掉对方后己方棋串获得 ≥3 气，提子具有终局性），黑先——
  黑提白串即胜（胜率 > 0.95），脱先则被提（胜率 < 0.3），正解唯一；
  该结构经 KataGo 实测满足契约验题规则（见 docs/plan.md 窗口3 记录）；
- 主题按 ``utils.classify_theme`` 归类（capturing_race / life_death）；
- 入库：source='generated'（review_id 为空，区别于对局生成），
  适用级位 -15 ~ -6（基础题，面向 K 级爱好者）。

用法：
    python scripts/seed_basic.py --attempts 80 --target 30
    python scripts/seed_basic.py --dry-run --attempts 6
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.problems import store, utils  # noqa: E402
from backend.services.engine.verify import verify_position  # noqa: E402

SIZE = 9
SEED_RANK = (-15, -6)  # 基础题适用级位


# ---------------------------------------------------------------------------
# 随机局部构造（纯函数，便于单测）
# ---------------------------------------------------------------------------

def random_chain(
    rng: random.Random,
    size: int,
    length: int,
    avoid: Optional[set] = None,
) -> set:
    """随机游走生成一条 length 子的连通棋串（避开 avoid 点）。"""
    avoid = avoid or set()
    free = [(x, y) for x in range(size) for y in range(size)
            if (x, y) not in avoid]
    rng.shuffle(free)
    if not free:
        return set()
    chain = {free[0]}
    for _ in range(length - 1):
        growth = sorted(
            {(nx, ny) for x, y in chain
             for nx, ny in utils.neighbors(x, y, size)
             if (nx, ny) not in avoid} - chain
        )
        if not growth:
            break
        chain.add(rng.choice(growth))
    return chain


def find_mutual_atari(
    rng: random.Random, b_len: int = 5, w_len: int = 5, tries: int = 20000
) -> Optional[tuple[utils.Position, tuple, tuple]]:
    """搜索黑先的相邻互相打吃 → (position, b_lib, w_lib) 或 None。

    - 黑串/白串各仅 1 气，两串相邻（提子互保，防交换对冲）；
    - 任一方提掉对方后己方棋串获得 ≥3 气（终局性）。
    """
    for _ in range(tries):
        b = random_chain(rng, SIZE, b_len)
        w = random_chain(rng, SIZE, w_len, set(b))
        if len(b) < 2 or len(w) < 2 or b & w:
            continue
        adjacent = any(
            n in w for x, y in b for n in utils.neighbors(x, y, SIZE)
        )
        if not adjacent:
            continue
        pos = {pt: "B" for pt in b}
        pos.update({pt: "W" for pt in w})
        b_neigh = sorted({n for x, y in b for n in utils.neighbors(x, y, SIZE)}
                         - set(pos))
        w_neigh = sorted({n for x, y in w for n in utils.neighbors(x, y, SIZE)}
                         - set(pos))
        rng.shuffle(b_neigh)
        rng.shuffle(w_neigh)
        for pt in w_neigh[: max(0, len(w_neigh) - 1)]:
            pos[pt] = "B"
        for pt in b_neigh:
            if len(utils.group_liberties(b, pos, SIZE)) <= 1:
                break
            if pt in pos and pos[pt] == "W":
                continue
            pos[pt] = "W"
        b_group = utils.group_at(pos, next(iter(b))[0], next(iter(b))[1], SIZE)
        w_group = utils.group_at(pos, next(iter(w))[0], next(iter(w))[1], SIZE)
        b_libs = utils.group_liberties(b_group, pos, SIZE)
        w_libs = utils.group_liberties(w_group, pos, SIZE)
        if len(b_libs) != 1 or len(w_libs) != 1:
            continue
        b_lib, w_lib = next(iter(b_libs)), next(iter(w_libs))
        if b_lib == w_lib:
            continue
        if any(not utils.group_liberties(g, pos, SIZE)
               for g in utils.all_groups(pos, SIZE)):
            continue
        # 终局性检查（提掉对方后己方 ≥3 气）
        f_b_takes = {pt: c for pt, c in pos.items() if pt not in w_group}
        f_b_takes[w_lib] = "B"
        b_after = utils.group_at(f_b_takes, next(iter(b))[0],
                                 next(iter(b))[1], SIZE)
        if len(utils.group_liberties(b_after, f_b_takes, SIZE)) < 3:
            continue
        f_w_takes = {pt: c for pt, c in pos.items() if pt not in b_group}
        f_w_takes[b_lib] = "W"
        w_after = utils.group_at(f_w_takes, next(iter(w))[0],
                                 next(iter(w))[1], SIZE)
        if len(utils.group_liberties(w_after, f_w_takes, SIZE)) < 3:
            continue
        return pos, b_lib, w_lib
    return None


def seed_candidates(pos: utils.Position, b_lib, w_lib, cap: int = 12) -> list:
    """候选点：正解点（提白串点）+ 其余空邻点 + pass。

    与生成器候选逻辑一致：不含黑方自救点 b_lib（黑扩展己方棋串的
    "次优解"不参与正解唯一性验证，用户答它时由判题接口如实反馈）。
    """
    cands = sorted({w_lib} | {
        n for pt in pos for n in utils.neighbors(pt[0], pt[1], SIZE)
        if n not in pos
    } - {b_lib})
    return [utils.xy_to_coord(x, y, SIZE) for x, y in cands[:cap]] + ["pass"]


# ---------------------------------------------------------------------------
# 验证与入库
# ---------------------------------------------------------------------------

def seed_one(
    rng: random.Random,
    profile: str,
    dry_run: bool = False,
    db_path=None,
) -> tuple[Optional[dict], Optional[str]]:
    """生成并验证一道种子题 → (brief, 跳过原因)。"""
    found = find_mutual_atari(rng, b_len=4, w_len=6)
    if found is None:
        return None, "未找到合法互相打吃局部"
    pos, b_lib, w_lib = found
    theme = utils.classify_theme(pos, pos, SIZE, 0, 0)
    setup_sgf = utils.setup_sgf(pos, "B", SIZE)
    cands = seed_candidates(pos, b_lib, w_lib)
    results = verify_position(setup_sgf, cands, profile=profile)
    valid = [
        r for r in results
        if r.error is None and r.coord != "pass" and r.winrate is not None
    ]
    valid.sort(key=lambda r: r.winrate or 0.0, reverse=True)
    if not valid:
        return None, "无有效候选点"
    best, second = valid[0], valid[1] if len(valid) > 1 else None
    if (best.winrate or 0.0) <= 0.95:
        return None, f"正解胜率不足: {(best.winrate or 0):.0%}"
    if second is not None and (second.winrate or 0.0) >= 0.3:
        return None, f"次优点胜率过高: {(second.winrate or 0):.0%}"
    pass_r = next(
        (r for r in results
         if r.coord == "pass" and r.error is None and r.winrate is not None),
        None,
    )
    if pass_r is None or (pass_r.winrate or 1.0) >= 0.3:
        return None, "局面不紧迫（pass 后胜率仍高）"

    branches = {
        "solver": "B",
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
    center_coord = utils.xy_to_coord(b_lib[0], b_lib[1], SIZE)
    row = {
        "id": utils.problem_id(setup_sgf, theme),
        "source": "generated",
        "review_id": None,
        "theme": theme,
        "rank_min": SEED_RANK[0],
        "rank_max": SEED_RANK[1],
        "setup_sgf": setup_sgf,
        "answer": best.coord,
        "branches": json.dumps(branches, ensure_ascii=False),
        "verdict": utils.verdict_text(
            theme, "B", best.coord, best.winrate or 0.0,
            second.coord if second else None,
            second.winrate if second else None,
        ),
        "hint": utils.hint_text(theme, "B", center_coord, SIZE),
        "explanation": None,
        "status": "active",
        "created_at": store.utcnow(),
    }
    if not dry_run:
        inserted = store.insert_problem(row, db_path)
        if not inserted:
            return None, "已存在（幂等跳过）"
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
    ap = argparse.ArgumentParser(description="程序生成基础死活题种子")
    ap.add_argument("--attempts", type=int, default=80,
                    help="随机尝试次数（含丢弃）")
    ap.add_argument("--target", type=int, default=30, help="目标入库题数")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（复现用）")
    ap.add_argument("--profile", default="standard",
                    choices=("fast", "standard", "fine"))
    ap.add_argument("--dry-run", action="store_true", help="只验证不入库")
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    ok = i = 0
    t0 = time.time()
    while i < args.attempts and ok < args.target:
        i += 1
        try:
            brief, reason = seed_one(rng, args.profile, args.dry_run)
        except Exception as exc:  # 引擎不可用等
            print(f"[{i:3d}] 异常: {exc}")
            return 2
        if brief is None:
            print(f"[{i:3d}] 丢弃: {reason}")
            continue
        ok += 1
        print(f"[{i:3d}] → {brief['id']} ({brief['theme']})")
    elapsed = time.time() - t0
    print(f"\n[seed] 完成：成功 {ok} / 目标 {args.target}，尝试 {i} 次，"
          f"耗时 {elapsed:.1f}s，平均 {elapsed / max(1, i):.1f}s/题"
          + ("（dry-run，未入库）" if args.dry_run else ""))
    return 0 if ok >= args.target else 1


if __name__ == "__main__":
    raise SystemExit(main())
