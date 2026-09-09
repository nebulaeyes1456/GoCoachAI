"""判题服务（窗口3）。

``attempt(problem_id, coord)`` 实现契约 §4.3 ``POST /problems/{id}/attempt``：

- 答案坐标与 ``answer`` / ``branches`` 对比：
  - 命中正解 → correct=true，返回祝贺文本 + 正解变化（PV）与 explanation；
  - 未命中 → 优先取 ``branches.candidates`` 中该点的验证结果（不启引擎）；
    没有则调 ``verify_position`` 补查该点后果（胜率/PV），生成规则反馈文字
    （"这步之后胜率仅 X%……正确下法是 A"；死活/对杀题附加"局部会被杀"提示）；
- ``solved``：本题已有任意一次答对记录即为 true（"本题已过"）；
- 累计 attempts；同一正解多次提交幂等：每次都落一条 attempts 记录，
  但响应确定（correct/solved 不变、不重复触发讲解补写等副作用）。
"""

from __future__ import annotations

from typing import Optional

from ..engine.verify import verify_position
from . import store, utils


class ProblemNotFoundError(RuntimeError):
    """题目不存在（路由层转 HTTP 404）。"""


def _branch_result(problem: dict, coord: str) -> Optional[dict]:
    """branches.candidates 中该坐标的验证结果（无则 None）。"""
    branches = store.branches_json(problem)
    for cand in branches.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        if utils.normalize_coord(str(cand.get("coord", ""))) == coord:
            return cand
    answer = branches.get("answer")
    if isinstance(answer, dict) and utils.normalize_coord(
        str(answer.get("coord", ""))
    ) == coord:
        return answer
    return None


def _feedback_text(
    theme: str,
    solver_color: str,
    coord: str,
    winrate: Optional[float],
    answer: str,
    answer_winrate: Optional[float],
) -> str:
    c = utils.COLOR_CN.get(solver_color.upper(), solver_color)
    if winrate is None:
        return f"这步棋无法验证（可能不合法），请换个点再试。本题正解是 {answer}。"
    if winrate >= 0.95:
        return f"这步棋其实也很好（{c}方胜率 {winrate:.0%}），但本题收录的正解是 {answer}。"
    text = f"这步不是正解：{coord} 之后{c}方胜率只有 {winrate:.0%}。"
    if theme in ("life_death", "capturing_race") and winrate < 0.3:
        if theme == "life_death":
            text += " 局部棋形会被杀（或无法做活）。"
        else:
            text += " 对杀气数不够，将被对方吃掉。"
    if answer_winrate is not None:
        text += f" 正确下法是 {answer}（{c}方胜率 {answer_winrate:.0%}）。"
    else:
        text += f" 正确下法是 {answer}。"
    return text


def attempt(problem_id: str, coord: str, db_path=None) -> dict:
    """判题主入口；返回契约 §4.3 结构。

    引擎不可用时退化：仍返回 wrong 反馈（winrate=None 的通用文字），
    不抛 502 —— 判题是交互行为，失败不应阻断做题。
    """
    problem = store.get_problem(problem_id, db_path)
    if problem is None:
        raise ProblemNotFoundError(f"题目不存在: {problem_id}")

    norm = utils.normalize_coord(coord)
    answer_norm = utils.normalize_coord(problem["answer"])
    branches = store.branches_json(problem)
    solver = str(branches.get("solver") or "B").upper()

    correct = norm == answer_norm
    if correct:
        answer_branch = branches.get("answer") or {}
        pv = answer_branch.get("pv") if isinstance(answer_branch, dict) else None
        variation = [str(x) for x in pv] if isinstance(pv, list) else [answer_norm]
        store.add_attempt(problem_id, norm, True, db_path)
        return {
            "correct": True,
            "response": f"正确！{problem['verdict'] or ''}".strip(),
            "variation": variation,
            "solved": True,
            "explanation": problem.get("explanation"),
        }

    # ---- 错误答案：取该点后果 ----
    result = _branch_result(problem, norm)
    if result is None:
        try:
            verifies = verify_position(
                problem["setup_sgf"], [norm], profile="standard"
            )
            if verifies:
                result = {
                    "coord": verifies[0].coord,
                    "winrate": verifies[0].winrate,
                    "score_lead": verifies[0].score_lead,
                    "visits": verifies[0].visits,
                    "pv": list(verifies[0].pv),
                    "best_coord": verifies[0].best_coord,
                    "error": verifies[0].error,
                }
        except Exception:
            result = None
    winrate = result.get("winrate") if isinstance(result, dict) else None
    if isinstance(result, dict) and result.get("error"):
        winrate = None
    answer_branch = branches.get("answer") or {}
    answer_winrate = (
        answer_branch.get("winrate") if isinstance(answer_branch, dict) else None
    )
    variation = []
    if isinstance(result, dict) and isinstance(result.get("pv"), list):
        variation = [str(x) for x in result["pv"]][:15]

    store.add_attempt(problem_id, norm, False, db_path)
    solved = store.has_correct_attempt(problem_id, db_path)
    return {
        "correct": False,
        "response": _feedback_text(
            problem["theme"], solver, norm, winrate,
            problem["answer"], answer_winrate,
        ),
        "variation": variation,
        "solved": solved,
        "explanation": None,
    }
