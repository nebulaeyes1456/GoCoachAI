"""对弈陪练服务（新手板块）：用户与 KataGo 对弈 + 下错实时提示。

- 用户下一手，KataGo 用一选应一手；
- 用户这手质量 = 与该手落子前局面一选的胜率差（同视角）：
  delta > -6p 静默；-12p~-6p 疑问手；≤-12p 坏手；
- 免费提示由 KataGo 数据直接生成；可选 LLM 讲解（play/tip，付费+缓存）。
"""
from __future__ import annotations

import hashlib
import json
import threading

from ..engine.analyze_sgf import _game_to_query, _resolve_paths
from ..engine.engine import KataGoEngine
from ...common import cost, db
from ..coach import prompts as coach_prompts  # 复用讲解 prompt
from ..coach.llm import LLMClient

BAD_THRESHOLD = 0.12        # 坏手：胜率损失 ≥12 个百分点（新手放宽）
QUESTION_THRESHOLD = 0.06   # 疑问手：6~12 个百分点

# AI 棋力档（humanSL 模型按级位模拟人类水平；visits 联动：低档更弱且更快）
AI_RANK_VISITS = {
    "20k": 60, "15k": 90, "10k": 130, "5k": 220, "1k": 360, "1d": 520,
}


def _apply_ai_rank(req: dict, ai_rank: str | None) -> None:
    """对 AI 应手查询注入 humanSLProfile；判定用户这手的查询绝不能用弱档。"""
    if not ai_rank:
        return
    req["humanSLProfile"] = {"rank": str(ai_rank).lower()}
    v = AI_RANK_VISITS.get(str(ai_rank).lower())
    if v:
        req["maxVisits"] = v

_engine: KataGoEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> KataGoEngine:
    """惰性长驻引擎（桌面单用户串行对弈，复用免启动开销）。"""
    global _engine
    with _engine_lock:
        if _engine is None:
            executable, model, cfg = _resolve_paths()
            _engine = KataGoEngine(executable, model, cfg, analysis_threads=1)
            _engine.start()
        return _engine


def set_engine(engine) -> None:
    """测试注入钩子。"""
    global _engine
    with _engine_lock:
        _engine = engine


def _natural_pos(size: int, coord: str) -> str:
    """坐标 → 方位+第几线的自然描述（新手友好，规避坐标字母数字）。"""
    if not coord or len(coord) < 2 or not coord[0].isalpha() or not coord[1:].isdigit():
        return "棋盘上（看绿圈）"
    col = ord(coord[0].upper()) - 65
    if col >= 8:
        col -= 1  # 跳过 I
    row = int(coord[1:])
    if not (0 <= col < size and 1 <= row <= size):
        return "棋盘上（看绿圈）"
    left, right = col + 1, size - col
    up, down = size - row + 1, row
    line = min(left, right, up, down)
    if line >= 5:
        return "棋盘中央"
    line_s = {1: "一线", 2: "二线", 3: "三线", 4: "四线"}[line]
    h = "左" if left < right else ("右" if right < left else "")
    v = "上" if up < down else ("下" if down < up else "")
    return f"{h}{v}{line_s}"


def _hint_text(level: str, delta: float, user_wr: float | None,
               best_wr: float | None, best: str, size: int = 9) -> dict:
    """KataGo 数据 → 免费提示文案（不用 LLM，棋语化规避坐标）。"""
    pos = _natural_pos(size, best)

    if level == "bad":
        title = "坏手"
        text = (
            f"这手棋掉得有点多（损失 {abs(delta) * 100:.0f} 个百分点）。"
            f"好点在{pos}——棋盘上已用绿圈标出。"
        )
    else:
        title = "疑问手"
        text = (
            f"这手棋略缓（损失 {abs(delta) * 100:.0f} 个百分点）。"
            f"更好的点在{pos}，棋盘上已用绿圈标出。"
        )
    return {"title": title, "text": text}


def analyze_move(size: int, moves: list[list[str]], komi: float = 6.5,
                  ai_rank: str | None = None) -> dict:
    """对弈一手分析。

    moves：完整行棋序列，最后一步必须是用户刚下的一手；
    ai_rank：AI 棋力档（20k~1d，None=最强全强度）；只作用于 AI 应手查询。
    返回 AI 应手（一选）+ 用户这手的评价（delta / level / 免费提示）。
    """
    if not moves:
        raise ValueError("moves 为空")
    n = len(moves)
    setup = f"(;GM[1]FF[4]SZ[{size}]KM[{komi}])"
    engine = get_engine()

    # Query A：用户刚下完的完整局面 → AI 应手（轮到 AI，按棋力档模拟）
    req = _game_to_query(setup, "fast", [n])
    req["moves"] = moves
    _apply_ai_rank(req, ai_rank)
    resp = engine.query(req)
    root = (resp or {}).get("rootInfo") or {}
    infos = (resp or {}).get("moveInfos") or []
    ai_move = None
    ai_winrate = root.get("winrate")
    score_lead = root.get("scoreLead")
    best_a = next((x for x in infos if x.get("order") == 0), None)
    if best_a and best_a.get("move"):
        ai_move = str(best_a["move"]).upper()
        ai_winrate = best_a.get("winrate")
        if ai_winrate is None:
            ai_winrate = root.get("winrate")

    # Query B：用户落子前的局面 → 用户这手 vs 一选（同为用户视角）
    best = None
    best_wr = None
    user_wr = None
    if n >= 2:
        req2 = _game_to_query(setup, "fast", [n - 1])
        req2["moves"] = moves[:-1]
        resp2 = engine.query(req2)
        infos2 = (resp2 or {}).get("moveInfos") or []
        best_b = next((x for x in infos2 if x.get("order") == 0), None)
        if best_b and best_b.get("move"):
            best = str(best_b["move"]).upper()
            best_wr = best_b.get("winrate")
        user_coord = str(moves[-1][1] or "").upper()
        hit = next((x for x in infos2
                    if str(x.get("move") or "").upper() == user_coord), None)
        if hit is not None and hit.get("winrate") is not None:
            user_wr = hit.get("winrate")
        elif infos2:
            # 弱手被剪枝：取候选里最低胜率作保守估计
            user_wr = min(float(x["winrate"]) for x in infos2
                          if x.get("winrate") is not None)

    delta = None
    if user_wr is not None and best_wr is not None:
        delta = round(user_wr - best_wr, 4)
    level = "ok"
    if delta is not None:
        if delta <= -BAD_THRESHOLD:
            level = "bad"
        elif delta <= -QUESTION_THRESHOLD:
            level = "question"

    hint = None
    if level != "ok" and best:
        hint = _hint_text(level, delta, user_wr, best_wr, best, size)

    return {
        "size": size,
        "ai_rank": ai_rank,
        "ai_move": ai_move,
        "ai_winrate": ai_winrate,          # AI 视角
        "user_winrate": None if user_wr is None else round(user_wr, 4),
        "best": best,
        "best_winrate": best_wr,
        "delta": delta,
        "level": level,
        "score_lead": score_lead,
        "hint": hint,
        "move_number": n,
    }


# ---------------------------------------------------------------------------
# LLM 讲解（付费，缓存：key=sha256(局面|坐标) 前 16 位落 explanations 表）
# ---------------------------------------------------------------------------

_client: LLMClient | None = None


def _cache_key(size: int, moves: list[list[str]], coord: str) -> str:
    raw = f"{size}|{'|'.join(c + ':' + (m or '') for c, m in moves)}|{coord}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _get_tip_cached(key: str, db_path=None) -> dict | None:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT content, model, cost FROM explanations"
            " WHERE review_id=? AND kind='play_tip'",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        content = json.loads(row["content"])
    except (TypeError, json.JSONDecodeError):
        return None
    return {"content": content, "model": row["model"] or "",
            "cost": float(row["cost"] or 0.0)}


def explain_tip(size: int, moves: list[list[str]], coord: str, best: str,
                delta: float | None, level: str, db_path=None) -> dict:
    """坏手/疑问手的 LLM 讲解：错在哪、常规下法、原理、口诀。"""
    key = _cache_key(size, moves, coord)
    cached = _get_tip_cached(key, db_path)
    if cached is not None:
        return {"kind": "play_tip", **cached}

    from ...common.cost import month_cost, record_call
    from ...common.settings import get_settings
    limit = float(get_settings().get("budget", {}).get("token_limit_month", 30) or 0)
    if month_cost(db_path=db_path) >= limit:
        raise BudgetExceeded("本月预算已用尽")

    global _client
    if _client is None:
        _client = LLMClient()
    messages = coach_prompts.build_play_tip_messages(
        size, moves, coord, best, delta, level)
    result = _client.chat_json(
        messages, coach_prompts.PLAY_TIP_SCHEMA, kind="play_tip", db_path=db_path)
    data = dict(result.data)
    for key_name, fallback in (("problem", "这手棋有问题。"), ("reason", "暂无解释。"),
                               ("recommendation", f"常规应下 {best}。"), ("proverb", "")):
        data.setdefault(key_name, fallback)
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO explanations"
            " (review_id, move_number, kind, content, model, cost, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (key, 0, "play_tip", json.dumps(data, ensure_ascii=False),
             result.model, float(result.cost or 0.0), "now"),
        )
        conn.commit()
    finally:
        conn.close()
    return {"kind": "play_tip", "content": data, "model": result.model,
            "cost": round(result.cost, 6)}


class BudgetExceeded(RuntimeError):
    """预算用尽（路由层转 402）。"""
