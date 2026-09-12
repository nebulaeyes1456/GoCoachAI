"""棋手档案存储（成长视图）。

- player_profiles：档案（id 用 sha1 时间戳前缀，可多档案切换）；
- reviews.profile_id：已归档棋谱的档案归属；
- profile_insights：画像与建议缓存。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from ...common import db


def new_profile_id(name: str) -> str:
    raw = f"{name}|{time.time_ns()}"
    return "pp" + hashlib.sha1(raw.encode()).hexdigest()[:14]


def create_profile(name: str, note: str = "", db_path=None) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    pid = new_profile_id(name)
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO player_profiles (id, name, note, created_at, updated_at)"
            " VALUES (?,?,?,?,?)",
            (pid, name.strip(), (note or "").strip(), now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": pid, "name": name.strip(), "note": (note or "").strip(),
            "games_count": 0, "created_at": now, "updated_at": now}


def list_profiles(db_path=None) -> list[dict]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT p.id, p.name, p.note, p.created_at, p.updated_at,"
            " (SELECT COUNT(*) FROM reviews r WHERE r.profile_id = p.id) AS n"
            " FROM player_profiles p ORDER BY p.updated_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r["id"], "name": r["name"], "note": r["note"] or "",
         "games_count": int(r["n"] or 0),
         "created_at": r["created_at"], "updated_at": r["updated_at"]}
        for r in rows
    ]


def get_profile(profile_id: str, db_path=None) -> Optional[dict]:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT p.id, p.name, p.note, p.created_at, p.updated_at,"
            " (SELECT COUNT(*) FROM reviews r WHERE r.profile_id = p.id) AS n"
            " FROM player_profiles p WHERE p.id = ?", (profile_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"], "note": row["note"] or "",
            "games_count": int(row["n"] or 0),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def rename_profile(profile_id: str, name: str, note: str = "", db_path=None) -> bool:
    conn = db.connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE player_profiles SET name=?, note=?, updated_at=? WHERE id=?",
            (name.strip(), (note or "").strip(),
             time.strftime("%Y-%m-%dT%H:%M:%S"), profile_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_profile(profile_id: str, db_path=None) -> None:
    """删除档案（棋谱解除关联，不删除复盘数据）。"""
    conn = db.connect(db_path)
    try:
        conn.execute("UPDATE reviews SET profile_id = NULL WHERE profile_id = ?",
                     (profile_id,))
        conn.execute("DELETE FROM profile_insights WHERE profile_id = ?",
                     (profile_id,))
        conn.execute("DELETE FROM player_profiles WHERE id = ?", (profile_id,))
        conn.commit()
    finally:
        conn.close()


def attach_review(profile_id: str, review_id: str, db_path=None) -> bool:
    conn = db.connect(db_path)
    try:
        rev = conn.execute("SELECT id FROM reviews WHERE id=?", (review_id,)).fetchone()
        if rev is None:
            return False
        conn.execute("UPDATE reviews SET profile_id=? WHERE id=?",
                     (profile_id, review_id))
        conn.execute(
            "UPDATE player_profiles SET updated_at=? WHERE id=?",
            (time.strftime("%Y-%m-%dT%H:%M:%S"), profile_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def detach_review(review_id: str, db_path=None) -> None:
    conn = db.connect(db_path)
    try:
        conn.execute("UPDATE reviews SET profile_id=NULL WHERE id=?", (review_id,))
        conn.commit()
    finally:
        conn.close()


def profile_games(profile_id: str, db_path=None) -> list[dict]:
    """档案内棋谱摘要（含每局坏手/疑问/好手统计）。"""
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT r.id, r.board_size, r.black, r.white, r.status, r.created_at,"
            " r.profile_id,"
            " (SELECT COUNT(*) FROM moves m WHERE m.review_id = r.id) AS n,"
            " (SELECT COUNT(*) FROM moves m WHERE m.review_id = r.id"
            "   AND m.category='blunder') AS b,"
            " (SELECT COUNT(*) FROM moves m WHERE m.review_id = r.id"
            "   AND m.category='question') AS q,"
            " (SELECT COUNT(*) FROM moves m WHERE m.review_id = r.id"
            "   AND m.category='good') AS g"
            " FROM reviews r WHERE r.profile_id = ?"
            " ORDER BY r.created_at DESC",
            (profile_id,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({
            "review_id": r["id"],
            "black": r["black"] or "",
            "white": r["white"] or "",
            "board_size": int(r["board_size"] or 19),
            "moves_count": int(r["n"] or 0),
            "status": r["status"] or "done",
            "created_at": r["created_at"],
            "blunders": int(r["b"] or 0),
            "questions": int(r["q"] or 0),
            "good": int(r["g"] or 0),
            "imported": True,  # 归档到档案的棋谱都视为纳入成长分析
        })
    return out


def save_insight(profile_id: str, insight: dict, db_path=None) -> None:
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO profile_insights"
            " (profile_id, games_count, features, rank_estimate, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(profile_id) DO UPDATE SET"
            " games_count=excluded.games_count, features=excluded.features,"
            " rank_estimate=excluded.rank_estimate,"
            " updated_at=excluded.updated_at",
            (profile_id, int(insight.get("n_games") or 0),
             json.dumps(insight, ensure_ascii=False),
             str(insight.get("rank_estimate") or ""),
             time.strftime("%Y-%m-%dT%H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()


def save_advice(profile_id: str, advice: dict, model: str, db_path=None) -> None:
    conn = db.connect(db_path)
    try:
        conn.execute(
            "UPDATE profile_insights SET advice=?, advice_model=?, updated_at=?"
            " WHERE profile_id=?",
            (json.dumps(advice, ensure_ascii=False), model,
             time.strftime("%Y-%m-%dT%H:%M:%S"), profile_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_insight(profile_id: str, db_path=None) -> Optional[dict]:
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT features, rank_estimate, advice, advice_model, games_count,"
            " updated_at FROM profile_insights WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        features = json.loads(row["features"]) if row["features"] else {}
    except json.JSONDecodeError:
        features = {}
    try:
        advice = json.loads(row["advice"]) if row["advice"] else None
    except json.JSONDecodeError:
        advice = None
    return {
        "features": features,
        "rank_estimate": row["rank_estimate"] or "",
        "advice": advice,
        "advice_model": row["advice_model"] or "",
        "games_count": int(row["games_count"] or 0),
        "updated_at": row["updated_at"],
    }
