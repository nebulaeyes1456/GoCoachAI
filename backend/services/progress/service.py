"""成长视图业务编排：档案、棋谱归档/导入、画像、AI 提高建议。"""
from __future__ import annotations

from typing import Optional

from ...common import db
from ..coach import prompts
from ..coach.llm import LLMClient
from . import analyzer, store

ADVICE_MAX_TOKENS = 900

_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


class ProfileNotFoundError(RuntimeError):
    """档案不存在。"""


class ReviewNotFoundError(RuntimeError):
    """复盘记录不存在。"""


class NoGamesError(RuntimeError):
    """档案还没有棋谱，无法画像。"""


# ---------------------------------------------------------------------------
# 档案 CRUD
# ---------------------------------------------------------------------------

def create(name: str, note: str = "", db_path=None) -> dict:
    return store.create_profile(name, note, db_path)


def list_all(db_path=None) -> list[dict]:
    return store.list_profiles(db_path)


def delete(profile_id: str, db_path=None) -> None:
    store.delete_profile(profile_id, db_path)


def detail(profile_id: str, db_path=None) -> dict:
    profile = store.get_profile(profile_id, db_path)
    if profile is None:
        raise ProfileNotFoundError(f"档案不存在: {profile_id}")
    games = store.profile_games(profile_id, db_path)
    insight = store.get_insight(profile_id, db_path)
    return {"profile": profile, "games": games, "insight": insight}


# ---------------------------------------------------------------------------
# 棋谱归档 / 导入
# ---------------------------------------------------------------------------

def attach(profile_id: str, review_id: str, db_path=None) -> dict:
    if store.get_profile(profile_id, db_path) is None:
        raise ProfileNotFoundError(f"档案不存在: {profile_id}")
    if not store.attach_review(profile_id, review_id, db_path):
        raise ReviewNotFoundError(f"复盘记录不存在: {review_id}")
    # 棋谱变化后画像缓存作废（下次 insight 重算）
    _invalidate_insight(profile_id, db_path)
    return {"profile_id": profile_id, "review_id": review_id, "attached": True}


def import_sgf(profile_id: str, sgf_text: str, review_profile: str = "fast",
               db_path=None) -> dict:
    """导入外部 SGF：提交复盘分析并立即归档（分析在后台进行）。"""
    if store.get_profile(profile_id, db_path) is None:
        raise ProfileNotFoundError(f"档案不存在: {profile_id}")
    from ..review.service import get_service as get_review_service

    review_id = get_review_service().submit(sgf_text, review_profile)
    store.attach_review(profile_id, review_id, db_path)
    return {"review_id": review_id, "status": "pending"}


def _invalidate_insight(profile_id: str, db_path=None) -> None:
    conn = db.connect(db_path)
    try:
        conn.execute("DELETE FROM profile_insights WHERE profile_id=?",
                     (profile_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 画像
# ---------------------------------------------------------------------------

def _games_with_rows(profile_id: str, db_path=None) -> list[dict]:
    games = store.profile_games(profile_id, db_path)
    if not games:
        return []
    ids = [g["review_id"] for g in games]
    conn = db.connect(db_path)
    try:
        qmarks = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT review_id, move_number, coord, delta, category,"
            f" best_coord, score_stdev FROM moves"
            f" WHERE review_id IN ({qmarks})", ids).fetchall()
    finally:
        conn.close()
    per: dict[str, list] = {}
    for r in rows:
        per.setdefault(r["review_id"], []).append(dict(r))
    for g in games:
        g["move_rows"] = per.get(g["review_id"], [])
    return games


def insight(profile_id: str, force: bool = False, db_path=None) -> dict:
    """计算（或读缓存）档案画像。"""
    if store.get_profile(profile_id, db_path) is None:
        raise ProfileNotFoundError(f"档案不存在: {profile_id}")
    cached = None if force else store.get_insight(profile_id, db_path)
    if cached is not None and cached.get("features"):
        return {
            "profile_id": profile_id,
            "insight": {"features": cached["features"],
                        "rank_estimate": cached["rank_estimate"],
                        "advice": cached.get("advice"),
                        "advice_model": cached.get("advice_model")},
        }
    games = _games_with_rows(profile_id, db_path)
    if not games:
        raise NoGamesError("该档案还没有棋谱，先导入或收藏一局棋吧。")
    features = analyzer.build_insight(games, db_path)
    store.save_insight(profile_id, features, db_path)
    return {"profile_id": profile_id,
            "insight": {"features": features,
                        "rank_estimate": features.get("rank_estimate", ""),
                        "advice": None, "advice_model": ""}}


def advice(profile_id: str, force: bool = False, db_path=None) -> dict:
    """生成 / 刷新 AI 提高建议（基于画像特征 + 典型坏手样本）。"""
    profile = store.get_profile(profile_id, db_path)
    if profile is None:
        raise ProfileNotFoundError(f"档案不存在: {profile_id}")
    cached = None if force else store.get_insight(profile_id, db_path)
    if cached is not None and cached.get("advice") and not force:
        return {
            "profile_id": profile_id,
            "insight": {
                "features": cached.get("features") or {},
                "rank_estimate": cached.get("rank_estimate") or "",
                "advice": cached["advice"],
                "advice_model": cached.get("advice_model") or "",
            },
            "model": cached.get("advice_model") or "",
            "cost": 0.0,
        }
    games = _games_with_rows(profile_id, db_path)
    if not games:
        raise NoGamesError("该档案还没有棋谱，先导入或收藏一局棋吧。")
    features = analyzer.build_insight(games, db_path)
    store.save_insight(profile_id, features, db_path)
    samples = analyzer.sample_bad_moves(profile_id, limit=6, db_path=db_path)

    messages = prompts.build_progress_advice_messages(
        name=profile["name"],
        insight=features,
        samples=samples,
    )
    result = get_client().chat_json(
        messages, prompts.PROGRESS_ADVICE_SCHEMA,
        kind="progress_advice", db_path=db_path, max_tokens=ADVICE_MAX_TOKENS)
    advice_data = dict(result.data)
    store.save_advice(profile_id, advice_data, result.model, db_path)
    return {
        "profile_id": profile_id,
        "insight": {"features": features,
                    "rank_estimate": features.get("rank_estimate", ""),
                    "advice": advice_data,
                    "advice_model": result.model},
        "model": result.model,
        "cost": round(result.cost, 6),
    }
