"""教练服务（窗口2）：explain / summary / ask 业务逻辑。

- 复盘数据优先读 ``moves`` 表（窗口1 产出）；开发期（表空）回退到
  ``tests/mock_review.json``（格式与契约 §4.1 ``GET /api/v1/review/{id}`` 一致）；
- 讲解结果落 ``explanations`` 表，同 (review_id, move_number, kind) 命中缓存
  直接返回、不再扣费；summary 的 move_number 固定为 0；
- ask 不落 explanations，落 ``coach_asks`` 旁表（自定，已同步 docs/plan.md）；
- 每月成本经 ``common.cost`` 累计，超 ``budget.token_limit_month`` 抛
  ``BudgetExceededError``（路由层转 402）；
- 质量红线：variation 只保留传入 KataGo PV 中的着法（§4.2）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ...common import cost, db, settings
from . import prompts
from .llm import LLMClient

DEFAULT_LEVEL = "-5"            # 未从 SGF 读到段位时的目标水平（K 级爱好者）
SUMMARY_MOVE_NUMBER = 0         # summary 在 explanations 表中占用的 move_number
MOCK_REVIEW_PATH = settings.BASE_DIR / "tests" / "mock_review.json"

_client: LLMClient | None = None


class BudgetExceededError(RuntimeError):
    """本月预算已用尽（路由层转 HTTP 402）。"""


class ReviewNotFoundError(RuntimeError):
    """复盘数据不存在（路由层转 HTTP 404）。"""


class MoveNotFoundError(RuntimeError):
    """复盘数据中不存在指定手数（路由层转 HTTP 404）。"""


# ---------------------------------------------------------------------------
# LLM 客户端（惰性单例，便于测试注入）
# ---------------------------------------------------------------------------

def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


# ---------------------------------------------------------------------------
# 复盘数据获取（moves 表 → mock fallback）
# ---------------------------------------------------------------------------

def _review_from_db(review_id: str, db_path=None) -> dict | None:
    """从 moves/reviews 表组装 §4.1 结构；无数据返回 None。"""
    conn = db.connect(db_path)
    try:
        rev = conn.execute(
            "SELECT board_size, black, white, status, profile"
            " FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT move_number, color, coord, winrate, score_lead, visits,"
            " category, delta, best_coord, pv"
            " FROM moves WHERE review_id = ? ORDER BY move_number", (review_id,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    curve = []
    for r in rows:
        try:
            pv = json.loads(r["pv"]) if r["pv"] else []
        except (TypeError, json.JSONDecodeError):
            pv = []
        if not isinstance(pv, list):
            pv = []
        curve.append({
            "move": r["move_number"],
            "color": r["color"],
            "coord": r["coord"],
            "winrate": r["winrate"],
            "score_lead": r["score_lead"],
            "visits": r["visits"],
            "category": r["category"] or "normal",
            "delta": r["delta"],
            "best_coord": r["best_coord"],
            "pv": pv,
        })
    key_moves = [m for m in curve if m["category"] != "normal"]
    return {
        "id": review_id,
        "board_size": rev["board_size"] if rev else 19,
        "black": rev["black"] if rev else "",
        "white": rev["white"] if rev else "",
        "profile": rev["profile"] if rev else "fast",
        "status": rev["status"] if rev else "done",
        "winrate_curve": curve,
        "key_moves": key_moves,
        "stats": {
            "blunders": sum(1 for m in curve if m["category"] == "blunder"),
            "questions": sum(1 for m in curve if m["category"] == "question"),
            "good": sum(1 for m in curve if m["category"] == "good"),
        },
    }


def _review_from_mock(review_id: str) -> dict | None:
    """开发期兜底：读 tests/mock_review.json。"""
    if not MOCK_REVIEW_PATH.exists():
        return None
    try:
        data = json.loads(MOCK_REVIEW_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("id") != review_id:
        return None
    return data


def get_review_detail(review_id: str, db_path=None) -> dict:
    """复盘数据：优先 moves 表，开发期 fallback 到 mock。"""
    data = _review_from_db(review_id, db_path) or _review_from_mock(review_id)
    if data is None:
        raise ReviewNotFoundError(f"复盘数据不存在: {review_id}")
    return data


# ---------------------------------------------------------------------------
# 段位 / 限额 / 缓存 / 落库
# ---------------------------------------------------------------------------

_BRWR_RE = re.compile(r"(BR|WR)\s*\[\s*([^\]]*)\]", re.IGNORECASE)


def review_level(review_id: str, db_path=None) -> str:
    """从 reviews.sgf_path 的 SGF 中读 BR/WR 段位；读不到用默认 K 级。"""
    conn = db.connect(db_path)
    try:
        row = conn.execute("SELECT sgf_path FROM reviews WHERE id = ?", (review_id,)).fetchone()
    finally:
        conn.close()
    if row and row["sgf_path"]:
        try:
            text = Path(row["sgf_path"]).read_text(encoding="utf-8", errors="ignore")
            m = _BRWR_RE.search(text)
            if m and m.group(2).strip():
                return m.group(2).strip()
        except OSError:
            pass
    return DEFAULT_LEVEL


def _check_budget(db_path=None) -> None:
    limit = float(settings.get_settings().get("budget", {}).get("token_limit_month", 30) or 0)
    used = cost.month_cost(db_path=db_path)
    if used >= limit:
        raise BudgetExceededError(f"本月预算已用尽（已用 {used:.4f} 元 / 限额 {limit:.0f} 元）")


def _get_cached(review_id: str, move_number: int, kind: str, db_path=None) -> dict | None:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT content, model, cost FROM explanations"
            " WHERE review_id = ? AND move_number = ? AND kind = ?",
            (review_id, move_number, kind),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        content = json.loads(row["content"])
    except (TypeError, json.JSONDecodeError):
        return None  # 视为未命中，重新生成
    if kind == "move":
        return {
            "move_number": move_number, "kind": kind, "content": content,
            "model": row["model"] or "", "cost": float(row["cost"] or 0.0),
        }
    return {
        "kind": kind, "content": content,
        "model": row["model"] or "", "cost": float(row["cost"] or 0.0),
    }


def _save_explanation(review_id: str, move_number: int, kind: str,
                      content: dict, model: str, call_cost: float, db_path=None) -> None:
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO explanations"
            " (review_id, move_number, kind, content, model, cost, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (review_id, move_number, kind,
             json.dumps(content, ensure_ascii=False), model, float(call_cost or 0.0),
             cost.now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def _save_ask(sgf_text: str, question: str, level: str,
              answer: dict, model: str, call_cost: float, db_path=None) -> None:
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO coach_asks (sgf_text, question, level, answer, model, cost, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (sgf_text, question, level,
             json.dumps(answer, ensure_ascii=False), model, float(call_cost or 0.0),
             cost.now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 质量红线：variation 只保留传入 PV 中的着法
# ---------------------------------------------------------------------------

def sanitize_variation(llm_variation: list, kata_pv: list[str]) -> list[str]:
    """variation 与 KataGo PV 取交集（保序）；LLM 未给则直接返回 PV。"""
    pv = [c for c in (kata_pv or []) if isinstance(c, str) and c]
    if not isinstance(llm_variation, list):
        return pv
    kept = [c for c in llm_variation if isinstance(c, str) and c in pv]
    return kept or pv


def sanitize_segments(llm_segments: list, allowed: set[str]) -> list[dict]:
    """同步讲棋分段清洗：每段 variation 每步为 [色, 坐标]（色∈B/W、
    坐标在白名单内，保序）；无文字且无变化的段丢弃。"""
    out: list[dict] = []
    for seg in llm_segments or []:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "").strip()
        var = seg.get("variation")
        kept: list[list[str]] = []
        if isinstance(var, list):
            for step in var:
                if (isinstance(step, list) and len(step) == 2
                        and step[0] in ("B", "W")
                        and isinstance(step[1], str)
                        and step[1].upper() in allowed):
                    kept.append([step[0], step[1].upper()])
        if not text and not kept:
            continue
        out.append({"text": text, "variation": kept})
    return out


# ---------------------------------------------------------------------------
# 三个对外接口
# ---------------------------------------------------------------------------

def explain(review_id: str, move_number: int, db_path=None) -> dict:
    """POST /coach/explain：单手讲解（含缓存与预算检查）。

    content 含同步讲棋分段 segments（每段绑定 variation，前端边讲边摆棋）；
    旧缓存无 segments 时前端降级为纯文本展示。
    """
    review = get_review_detail(review_id, db_path)
    move = next((m for m in review.get("winrate_curve") or []
                 if m.get("move") == move_number), None)
    if move is None:
        raise MoveNotFoundError(f"第 {move_number} 手不在复盘数据中")
    cached = _get_cached(review_id, move_number, "move", db_path)
    if cached is not None:
        return cached
    _check_budget(db_path)
    level = review_level(review_id, db_path)
    messages = prompts.build_explain_messages(review, move_number, level)
    result = get_client().chat_json(
        messages, prompts.EXPLAIN_SCHEMA, kind="move", db_path=db_path)
    data = dict(result.data)
    data["variation"] = sanitize_variation(data.get("variation"), move.get("pv") or [])
    # 同步讲棋分段：白名单 = KataGo PV ∪ 候选点 ∪ 本手 ∪ 最佳点
    allowed = {c.upper() for c in (move.get("pv") or []) if isinstance(c, str)}
    for cand in move.get("candidates") or []:
        if isinstance(cand, dict) and cand.get("move"):
            allowed.add(str(cand["move"]).upper())
    for c in (move.get("coord"), move.get("best_coord")):
        if c:
            allowed.add(str(c).upper())
    data["segments"] = sanitize_segments(data.get("segments"), allowed)
    # 兜底：LLM 全部段落未给变化但 PV 存在时，自动按「最佳点+PV」补一段
    if data["segments"] and not any(s.get("variation") for s in data["segments"]):
        color = str(move.get("color") or "B").upper()
        seq: list[list[str]] = []
        if move.get("best_coord"):
            seq.append([color, str(move["best_coord"]).upper()])
        c2 = "W" if color == "B" else "B"
        for m2 in (move.get("pv") or []):
            mu = str(m2).upper()
            if mu == str(move.get("best_coord") or "").upper():
                continue
            seq.append([c2, mu])
            c2 = "W" if c2 == "B" else "B"
        if seq:
            data["segments"].append(
                {"text": "最佳应对变化演示（KataGo 推演）：", "variation": seq[:8]}
            )
    # 缺字段兜底（保证响应模型字段齐全）
    for key, fallback in (("problem", "本手未见明显问题。"), ("reason", "暂无解释。"),
                          ("recommendation", "维持当前下法即可。"),
                          ("takeaway", "下棋前先看断点与气。"), ("level_note", ""),
                          ("result_type", ""), ("can_tenuki", "")):
        data.setdefault(key, fallback)
    _save_explanation(review_id, move_number, "move", data, result.model, result.cost, db_path)
    return {
        "move_number": move_number, "kind": "move", "content": data,
        "model": result.model, "cost": round(result.cost, 6),
    }


def summary(review_id: str, db_path=None) -> dict:
    """POST /coach/summary：全局总结（含缓存与预算检查）。"""
    review = get_review_detail(review_id, db_path)
    cached = _get_cached(review_id, SUMMARY_MOVE_NUMBER, "summary", db_path)
    if cached is not None:
        return cached
    _check_budget(db_path)
    level = review_level(review_id, db_path)
    messages = prompts.build_summary_messages(review, level)
    result = get_client().chat_json(
        messages, prompts.SUMMARY_SCHEMA, kind="summary", db_path=db_path)
    data = dict(result.data)
    for key in ("opening", "middle", "endgame"):
        data.setdefault(key, "")
    for key in ("strengths", "weaknesses", "suggestions"):
        if not isinstance(data.get(key), list):
            data[key] = []
    _save_explanation(review_id, SUMMARY_MOVE_NUMBER, "summary",
                      data, result.model, result.cost, db_path)
    return {
        "kind": "summary", "content": data,
        "model": result.model, "cost": round(result.cost, 6),
    }


def penalty(review_id: str, move_number: int, db_path=None) -> dict:
    """POST /coach/penalty：坏手后果演示（局部厮杀聚焦 KataGo + 多手连讲）。

    - KataGo 视野用 allowMoves 锁定坏手附近区域，专注局部厮杀
      （不看全盘大场，正是「这手棋会被怎样惩罚」的演算方式）；
    - 惩罚序列交给 LLM 分步讲解：先手 / 好手 / 俗手 / 应对 + 总结；
    - 结果按 (review_id, move_number, penalty) 缓存；旧版无解说缓存视为未命中。
    """
    review = get_review_detail(review_id, db_path)
    curve = review.get("winrate_curve") or []
    move = next((m for m in curve if m.get("move") == move_number), None)
    if move is None:
        raise MoveNotFoundError(f"第 {move_number} 手不在复盘数据中")
    cached = _get_cached(review_id, move_number, "penalty", db_path)
    if cached is not None and (cached.get("content") or {}).get("steps") is not None:
        return cached

    from ..engine.analyze_sgf import _game_to_query, _resolve_paths
    from ..engine.engine import EngineError, KataGoEngine
    from ..problems import utils as putils

    size = int(review.get("board_size") or 19)
    moves = [
        [m.get("color"), m.get("coord") or "pass"]
        for m in curve
        if 0 < m.get("move", 0) <= move_number
        and m.get("color") in ("B", "W")
    ]
    setup = f"(;GM[1]FF[4]SZ[{size}])"
    executable, model, cfg = _resolve_paths()
    engine = KataGoEngine(executable, model, cfg, analysis_threads=1)
    pv: list[str] = []
    punisher_wr = None
    focused = False
    try:
        engine.start()
        n = len(moves)
        # 局部聚焦区域：坏手坐标包围盒外扩 2 格，排除已落子的点（防 Illegal）
        region: list[str] = []
        try:
            bx, by = putils.coord_to_xy(str(move.get("coord")))
            x0, x1 = max(0, bx - 2), min(size - 1, bx + 2)
            y0, y1 = max(0, by - 2), min(size - 1, by + 2)
            occupied = {str(m[1]).upper() for m in moves
                        if m[1] and str(m[1]).lower() != "pass"}
            region = [c for c in (putils.xy_to_coord(x, y, size)
                                  for x in range(x0, x1 + 1)
                                  for y in range(y0, y1 + 1))
                      if c not in occupied]
        except (ValueError, TypeError):
            region = []

        def run_query(extra: dict | None) -> dict | None:
            req = _game_to_query(setup, "standard", [n])
            req["moves"] = moves
            if extra:
                req.update(extra)
            try:
                resp = engine.query(req)
            except EngineError:
                return None
            if resp and resp.get("error"):
                return None
            return resp

        resp = run_query({"allowMoves": [
            {"player": "B", "moves": region, "untilDepth": 100},
            {"player": "W", "moves": region, "untilDepth": 100},
        ]}) if region else None
        focused = bool(region and resp)
        if resp is None:
            # 聚焦失败（区域过小/含非法点）：降级全局查询
            resp = run_query(None)
        root = (resp or {}).get("rootInfo") or {}
        punisher_wr = root.get("winrate")
        infos = (resp or {}).get("moveInfos") or []
        best = next((x for x in infos if x.get("order") == 0), None)
        if best and best.get("move"):
            seq = [str(x).upper() for x in (best.get("pv") or [])]
            if seq and seq[0] == str(best["move"]).upper():
                pv = seq
            elif best.get("move"):
                pv = [str(best["move"]).upper()] + seq
        # 惩罚演示只看前 8 手（惩罚点 + 后续应对），控制播放长度
        pv = pv[:8]
    finally:
        engine.stop()

    punisher = "W" if move.get("color") == "B" else "B"
    steps: list[dict] = []
    summary = ""
    model_name = "kata"
    call_cost = 0.0
    if pv:
        # 多手连讲：LLM 分步标注 先手/好手/俗手/应对 + 总结
        _check_budget(db_path)
        level = review_level(review_id, db_path)
        seq = [
            [punisher if i % 2 == 0 else move.get("color"), coord]
            for i, coord in enumerate(pv)
        ]
        messages = prompts.build_penalty_messages(
            review, move_number, seq, punisher_wr, level)
        result = get_client().chat_json(
            messages, prompts.PENALTY_SCHEMA, kind="penalty", db_path=db_path)
        model_name = result.model
        call_cost = round(result.cost, 6)
        raw = dict(result.data)
        roles = {"先手", "好手", "俗手", "应对"}
        for item in raw.get("steps") or []:
            if not isinstance(item, dict):
                continue
            try:
                step = int(item.get("step"))
            except (TypeError, ValueError):
                continue
            if not 1 <= step <= len(pv):
                continue
            role = str(item.get("role") or "").strip() or "应对"
            if role not in roles:
                role = "应对"
            text = str(item.get("text") or "").strip()
            if text:
                steps.append({"step": step, "role": role, "text": text})
        summary = str(raw.get("summary") or "").strip()
    data = {
        "penalty_pv": pv,
        "punisher": punisher,
        "punisher_winrate": punisher_wr,
        "steps": steps,
        "summary": summary,
        "focused": focused,
    }
    _save_explanation(review_id, move_number, "penalty",
                      data, model_name, call_cost, db_path)
    return {"kind": "penalty", "content": data,
            "model": model_name, "cost": call_cost}



def deep_analysis(review_id: str, db_path=None) -> dict:
    """POST /coach/deep：整盘深度分析报告（缓存 + 预算检查）。"""
    review = get_review_detail(review_id, db_path)
    cached = _get_cached(review_id, -2, "deep", db_path)
    if cached is not None:
        return cached
    _check_budget(db_path)
    level = review_level(review_id, db_path)
    messages = prompts.build_deep_messages(review, level)
    result = get_client().chat_json(
        messages, prompts.DEEP_SCHEMA, kind="deep", db_path=db_path)
    data = dict(result.data)
    for key, fallback in (("title", "整盘深度分析"), ("overview", ""),
                          ("causality", "")):
        data.setdefault(key, fallback)
    for key in ("stages", "key_moves", "strengths", "weaknesses", "homework"):
        if not isinstance(data.get(key), list):
            data[key] = []
    _save_explanation(review_id, -2, "deep",
                      data, result.model, result.cost, db_path)
    return {
        "kind": "deep", "content": data,
        "model": result.model, "cost": round(result.cost, 6),
    }


def ask(sgf_text: str, question: str, level: str = DEFAULT_LEVEL, db_path=None) -> dict:
    """POST /coach/ask：答疑（不落 explanations，落 coach_asks 旁表）。"""
    _check_budget(db_path)
    messages = prompts.build_ask_messages(sgf_text, question, level)
    result = get_client().chat_json(
        messages, prompts.ANSWER_SCHEMA, kind="answer", db_path=db_path)
    data = dict(result.data)
    data.setdefault("conclusion", "暂时无法确定，请补充更多信息。")
    data.setdefault("reasoning", "")
    data.setdefault("variation", [])
    if not isinstance(data.get("kata_winrate"), (int, float)) or isinstance(data.get("kata_winrate"), bool):
        data["kata_winrate"] = None
    _save_ask(sgf_text, question, level, data, result.model, result.cost, db_path)
    return {
        "answer": data, "model": result.model, "cost": round(result.cost, 6),
    }


# 类型提示：返回值与契约 §4.2 的响应模型一致
__all__ = [
    "BudgetExceededError", "ReviewNotFoundError", "MoveNotFoundError",
    "get_client", "get_review_detail", "review_level", "sanitize_variation",
    "explain", "summary", "deep_analysis", "penalty", "ask",
]
