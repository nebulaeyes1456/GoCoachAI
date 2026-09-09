"""批量补写题目讲解脚本（窗口3）。

窗口2 就绪后，为 explanation 为空的题目经 ``coach/ask`` 批量生成讲解
（正解结论 + 理由 + 变化），结果以 JSON 字符串写回 problems.explanation。

- 讲解问题基于题库自身数据（题面 SGF / 正解 / 验证分支），符合 §4.2
  质量红线"只能引用调用方传入的数据"；
- 预算超限（BudgetExceededError）立即停止；单题失败跳过继续；
- 幂等：explanation 非空的题目直接跳过，可重复运行。

用法：
    python scripts/backfill_explanations.py --limit 20
    python scripts/backfill_explanations.py --source imported --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common import db as db_mod  # noqa: E402
from backend.services.coach import service as coach_service  # noqa: E402
from backend.services.problems import store, utils  # noqa: E402

THEME_CN = {
    "life_death": "死活",
    "capturing_race": "对杀",
    "endgame": "官子",
    "middle": "中盘选点",
}


def build_question(problem: dict) -> str:
    """构造传给 coach/ask 的 question 文本（sgf_text 单独传题面 SGF）。"""
    branches = store.branches_json(problem)
    answer_branch = branches.get("answer") or {}
    solver = branches.get("solver") or "B"
    c = utils.COLOR_CN.get(solver, solver)
    winrate = answer_branch.get("winrate")
    wr_text = f"（{c}方胜率 {winrate:.0%}）" if isinstance(winrate, (int, float)) else ""
    return (
        f"请讲解上面这道{THEME_CN.get(problem['theme'], problem['theme'])}练习题："
        f"先给结论，再解释为什么，最后给一条变化。"
        f"本题正解是 {problem['answer']}{wr_text}。"
        f"只允许引用题面与正解信息，不要编造着法。"
    )


def backfill(
    limit: int = 0,
    source: Optional[str] = None,
    dry_run: bool = False,
    db_path=None,
) -> tuple[int, int]:
    """批量补写；返回 (成功数, 跳过数)。"""
    where = "WHERE explanation IS NULL AND status='active'"
    params: list = []
    if source:
        where += " AND source=?"
        params.append(source)
    conn = db_mod.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT {store.PROBLEM_COLUMNS} FROM problems {where}"
            " ORDER BY created_at DESC"
            + (" LIMIT ?" if limit else ""),
            (*params, int(limit)) if limit else tuple(params),
        ).fetchall()
    finally:
        conn.close()

    ok = skipped = 0
    for r in rows:
        problem = store._row_to_problem(r)
        try:
            result = coach_service.ask(
                sgf_text=problem["setup_sgf"],
                question=build_question(problem),
                level=str(max(problem.get("rank_max") or -5, -15)),
                db_path=db_path,
            )
            answer = result.get("answer") or {}
            if not dry_run:
                store.update_problem(
                    problem["id"],
                    {"explanation": json.dumps(answer, ensure_ascii=False)},
                    db_path,
                )
            ok += 1
            print(f"[ok]  {problem['id']} ({problem['theme']}) → "
                  f"{result.get('model')} {result.get('cost')}")
        except coach_service.BudgetExceededError:
            print("[stop] 本月预算已用尽，停止补写")
            break
        except Exception as exc:  # LLM 失败等：跳过继续
            skipped += 1
            print(f"[skip] {problem['id']}: {exc}")
    return ok, skipped


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="批量补写题目 LLM 讲解")
    ap.add_argument("--limit", type=int, default=0, help="最多补写多少题（0=不限）")
    ap.add_argument("--source", default=None,
                    choices=("generated", "library", "imported"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    ok, skipped = backfill(args.limit, args.source, args.dry_run)
    print(f"\n[backfill] 完成：成功 {ok}，跳过 {skipped}"
          + ("（dry-run，未写库）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
