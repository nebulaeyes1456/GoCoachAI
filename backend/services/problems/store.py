"""题目系统数据层（窗口3）。

problems / attempts 两张表的 CRUD（契约 ``docs/architecture.md`` §3）：

- ``insert_problem``：幂等入库（主键冲突跳过），返回是否真正插入；
- ``list_problems``：按 theme / 适用级位（rank_min <= rank <= rank_max）筛选，
  分页返回（offset/limit）与总数；
- ``add_attempt`` / ``has_correct_attempt``：判题记录累计与"是否已过"查询。

所有函数接受可选 ``db_path``，便于测试注入临时库。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ...common import db as db_mod

PROBLEM_COLUMNS = (
    "id, source, review_id, theme, rank_min, rank_max, setup_sgf,"
    " answer, branches, verdict, hint, explanation, status, goal, created_at"
)


def utcnow() -> str:
    """UTC ISO 时间串（秒精度，与 §3 created_at 一致）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_problem(row: Any) -> dict:
    return {
        "id": row["id"],
        "source": row["source"],
        "goal": row["goal"],
        "review_id": row["review_id"],
        "theme": row["theme"],
        "rank_min": row["rank_min"],
        "rank_max": row["rank_max"],
        "setup_sgf": row["setup_sgf"],
        "answer": row["answer"],
        "branches": row["branches"],
        "verdict": row["verdict"],
        "hint": row["hint"],
        "explanation": row["explanation"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def insert_problem(problem: dict, db_path: str | Path | None = None) -> bool:
    """插入一道题（INSERT OR IGNORE，主键冲突幂等跳过）。

    ``problem`` 需含 §3 problems 全部非空字段；缺少 created_at 时自动补。
    返回是否真正插入（False = 已存在同 id 题目）。
    """
    conn = db_mod.connect(db_path)
    try:
        cur = conn.execute(
            f"""
            INSERT OR IGNORE INTO problems
                (id, source, review_id, theme, rank_min, rank_max,
                 setup_sgf, answer, branches, verdict, hint, explanation,
                 status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                problem["id"],
                problem.get("source") or "generated",
                problem.get("review_id"),
                problem["theme"],
                problem.get("rank_min"),
                problem.get("rank_max"),
                problem["setup_sgf"],
                problem["answer"],
                problem["branches"],
                problem.get("verdict") or "",
                problem.get("hint"),
                problem.get("explanation"),
                problem.get("status") or "active",
                problem.get("created_at") or utcnow(),
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_problem(
    problem_id: str, db_path: str | Path | None = None
) -> Optional[dict]:
    """按 id 取题目；不存在返回 None。"""
    conn = db_mod.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT {PROBLEM_COLUMNS} FROM problems WHERE id=?",
            (problem_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_problem(row) if row else None


def update_problem(
    problem_id: str, fields: dict, db_path: str | Path | None = None
) -> None:
    """更新题目部分字段（白名单列，防注入）。"""
    allowed = {
        "review_id", "theme", "rank_min", "rank_max", "setup_sgf",
        "answer", "branches", "verdict", "hint", "explanation", "status",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    conn = db_mod.connect(db_path)
    try:
        conn.execute(
            f"UPDATE problems SET {sets} WHERE id=?",
            (*updates.values(), problem_id),
        )
        conn.commit()
    finally:
        conn.close()


def _rank_where(rank: Optional[int]) -> tuple[str, list[Any]]:
    """适用级位筛选：rank_min <= rank <= rank_max（K 为负、D 为正）。

    空 rank_min/rank_max 视为全开（-99 / 99）。返回的 SQL 片段不带
    WHERE/AND 前缀（由调用方拼接）。
    """
    if rank is None:
        return "", []
    return "COALESCE(rank_min, -99) <= ? AND COALESCE(rank_max, 99) >= ?", [
        int(rank), int(rank)
    ]


def list_problems(
    theme: Optional[str] = None,
    rank: Optional[int] = None,
    limit: int = 20,
    offset: int = 0,
    source: Optional[str] = None,
    sort: Optional[str] = None,
    db_path: str | Path | None = None,
) -> tuple[list[dict], int]:
    """分页列出 active 题目；返回 (题目列表, 总条数)。

    sort: 'easiest'（从易到难，rank_max 升序）/ 'hardest'（从难到易）；
    默认按入库时间倒序（最新在前）。
    """
    where = ["status='active'"]
    params: list[Any] = []
    if theme:
        where.append("theme=?")
        params.append(theme)
    if source:
        where.append("source=?")
        params.append(source)
    cond, rank_params = _rank_where(rank)
    if cond:
        where.append(cond)
        params.extend(rank_params)
    where_sql = " WHERE " + " AND ".join(where)

    conn = db_mod.connect(db_path)
    try:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM problems{where_sql}", params
            ).fetchone()[0]
        )
        order_sql = {
            "easiest": " ORDER BY rank_max ASC, id",
            "hardest": " ORDER BY rank_max DESC, id",
        }.get(sort, " ORDER BY created_at DESC, id")
        rows = conn.execute(
            f"SELECT {PROBLEM_COLUMNS} FROM problems{where_sql}"
            f"{order_sql} LIMIT ? OFFSET ?",
            (*params, int(limit), int(offset)),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_problem(r) for r in rows], total


def add_attempt(
    problem_id: str,
    answer_coord: str,
    correct: bool,
    db_path: str | Path | None = None,
) -> int:
    """累计一条判题记录，返回 attempts.id。"""
    conn = db_mod.connect(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO attempts (problem_id, answer_coord, correct, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (problem_id, answer_coord, 1 if correct else 0, utcnow()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def has_correct_attempt(
    problem_id: str, db_path: str | Path | None = None
) -> bool:
    """本题是否已有答对记录（"已过"）。"""
    conn = db_mod.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE problem_id=? AND correct=1",
            (problem_id,),
        ).fetchone()
    finally:
        conn.close()
    return bool(row and row[0])


def attempts_for(
    problem_id: str, db_path: str | Path | None = None
) -> list[dict]:
    """某题全部答题记录（按时间倒序）。"""
    conn = db_mod.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, problem_id, answer_coord, correct, created_at"
            " FROM attempts WHERE problem_id=? ORDER BY id DESC",
            (problem_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def branches_json(problem: dict) -> dict:
    """安全解析 branches JSON；坏数据返回空 dict。"""
    try:
        loaded = json.loads(problem.get("branches") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
