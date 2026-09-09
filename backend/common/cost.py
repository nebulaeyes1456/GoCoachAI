"""成本统计（窗口2）：记录每次 LLM 调用并聚合本月用量。

- 每次成功调用写入 ``coach_calls`` 表（模型、token 数、成本、时间）；
- ``month_cost()`` 供 ``GET /api/v1/system/info`` 的 ``token_usage_month`` 使用；
- 数据结构（自定，已同步 docs/plan.md）：
  coach_calls(kind, model, prompt_tokens, completion_tokens, cost, created_at)。

无外部依赖；表不存在或未初始化时安全返回 0。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from . import db

# created_at 格式 "YYYY-MM-DDTHH:MM:SS"，前 7 位即月份（YYYY-MM）
_MONTH_LEN = 7


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record_call(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float,
    kind: str = "coach",
    db_path: str | Path | None = None,
) -> None:
    """记录一次 LLM 调用（成功后才调用）。"""
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO coach_calls"
            " (kind, model, prompt_tokens, completion_tokens, cost, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (kind, model, int(prompt_tokens or 0), int(completion_tokens or 0),
             float(cost or 0.0), now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def month_cost(
    month: str | None = None,
    db_path: str | Path | None = None,
) -> float:
    """指定月份（YYYY-MM，默认当前月）的累计成本（元）。

    表尚未建立（数据库未初始化）时安全返回 0。
    """
    month = month or datetime.now().strftime("%Y-%m")
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM coach_calls"
            " WHERE substr(created_at, 1, ?) = ?",
            (_MONTH_LEN, month),
        ).fetchone()
        return float(row[0])
    except sqlite3.OperationalError:
        return 0.0
    finally:
        conn.close()


def month_tokens(
    month: str | None = None,
    db_path: str | Path | None = None,
) -> tuple[int, int]:
    """指定月份累计 (prompt_tokens, completion_tokens)。"""
    month = month or datetime.now().strftime("%Y-%m")
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens), 0),"
            " COALESCE(SUM(completion_tokens), 0)"
            " FROM coach_calls WHERE substr(created_at, 1, ?) = ?",
            (_MONTH_LEN, month),
        ).fetchone()
        return int(row[0]), int(row[1])
    except sqlite3.OperationalError:
        return 0, 0
    finally:
        conn.close()
