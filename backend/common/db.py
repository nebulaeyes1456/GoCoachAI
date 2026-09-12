"""SQLite 连接管理与建表（窗口0）。

- 数据库文件：``data/goapp.db``（运行时生成，不入库）。
- ``init_db()``：启动时执行基线建表（``docs/architecture.md`` §3，记为
  schema version 1）→ 按 ``MIGRATIONS`` 列表依次执行未应用的增量迁移，
  幂等可重复调用。
- ``get_db()``：FastAPI 依赖，每次请求一个连接，结束时提交并关闭。
- ``schema_version()``：读取当前 schema 版本（供 /system/version 展示）。

**迁移约定（T0，见 architecture.md §3）**：后续任何改表结构只允许在
``MIGRATIONS`` 末尾追加（version 递增），禁止手改 ``SCHEMA_SQL``；
迁移脚本须写成可重试形式（``IF NOT EXISTS`` / 先查后改），因为
``executescript`` 中途失败时不会记录版本，下次启动会整体重跑。

契约来源：``docs/architecture.md`` §3。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .settings import DATA_DIR

DB_PATH = DATA_DIR / "goapp.db"

# §3 数据模型（SQLite 表）
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY,
  sgf_path TEXT NOT NULL,
  board_size INT NOT NULL,
  black TEXT, white TEXT,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  profile TEXT NOT NULL DEFAULT 'fast',
  progress REAL NOT NULL DEFAULT 0,
  error TEXT
);

CREATE TABLE IF NOT EXISTS moves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  review_id TEXT NOT NULL REFERENCES reviews(id),
  move_number INT NOT NULL,
  color TEXT NOT NULL,
  coord TEXT NOT NULL,
  winrate REAL,
  score_lead REAL,
  visits INT,
  category TEXT,
  delta REAL,
  best_coord TEXT,
  pv TEXT,
  UNIQUE(review_id, move_number)
);

CREATE TABLE IF NOT EXISTS explanations (
  review_id TEXT NOT NULL,
  move_number INT NOT NULL,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  model TEXT, cost REAL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (review_id, move_number, kind)
);

CREATE TABLE IF NOT EXISTS problems (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  review_id TEXT,
  theme TEXT NOT NULL,
  rank_min INT, rank_max INT,
  setup_sgf TEXT NOT NULL,
  answer TEXT NOT NULL,
  branches TEXT NOT NULL,
  verdict TEXT NOT NULL,
  hint TEXT,
  explanation TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  problem_id TEXT NOT NULL REFERENCES problems(id),
  answer_coord TEXT NOT NULL,
  correct INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

-- 窗口2 追加：LLM 调用统计（成本记账，供 system/info 月度聚合）
CREATE TABLE IF NOT EXISTS coach_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,               -- move/summary/answer
  model TEXT NOT NULL,
  prompt_tokens INT NOT NULL DEFAULT 0,
  completion_tokens INT NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,     -- 元
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coach_calls_created
  ON coach_calls (substr(created_at, 1, 7));

-- 窗口2 追加：答疑记录旁表（ask 不落 explanations）
CREATE TABLE IF NOT EXISTS coach_asks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sgf_text TEXT NOT NULL,
  question TEXT NOT NULL,
  level TEXT,
  answer TEXT NOT NULL,             -- 结构化 JSON（§6 answer）
  model TEXT, cost REAL,
  created_at TEXT NOT NULL
);
"""

# ---------------------------------------------------------------------------
# schema 迁移（T0：打补丁的地基）
# ---------------------------------------------------------------------------

# 增量迁移列表：[(version, sql), ...]。基线（SCHEMA_SQL）为 version 1。
# 后续改表结构只在这里追加（version 严格递增），禁止手改上面的 SCHEMA_SQL。
# 迁移 SQL 必须可重试：executescript 中途失败不会记录版本，下次启动重跑。
MIGRATIONS: list[tuple[int, str]] = [
    # 例：(2, "ALTER TABLE reviews ADD COLUMN engine_backend TEXT;")
    # v2：moves 表加选点推荐（KataGo 候选点 JSON）
    (2, "ALTER TABLE moves ADD COLUMN candidates TEXT NOT NULL DEFAULT '[]';"),
    # v3：复杂度（目差不确定度）+ 终局目数热图
    (3, "ALTER TABLE moves ADD COLUMN score_stdev REAL;"),
    (4, "ALTER TABLE reviews ADD COLUMN final_ownership TEXT;"),
    # v5：每手当时的目数归属（KataGo ownership，随播放逐手热图）
    (5, "ALTER TABLE moves ADD COLUMN ownership TEXT;"),
    # v6：题目目标分类（做活/杀棋/对杀/逃棋筋/吃棋筋，规则+AI 兜底）
    (6, "ALTER TABLE problems ADD COLUMN goal TEXT;"),
    # v7：棋手档案（棋谱库 + 水平画像）
    (7, "CREATE TABLE IF NOT EXISTS player_profiles ("
        "id TEXT PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " note TEXT NOT NULL DEFAULT '',"
        " created_at TEXT,"
        " updated_at TEXT);"),
    # v8：复盘记录关联档案（NULL=未归档）
    (8, "ALTER TABLE reviews ADD COLUMN profile_id TEXT;"),
    # v9：档案画像缓存（features/rank 计算结果 + LLM 建议）
    (9, "CREATE TABLE IF NOT EXISTS profile_insights ("
        "profile_id TEXT PRIMARY KEY,"
        " games_count INTEGER NOT NULL DEFAULT 0,"
        " features TEXT,"
        " rank_estimate TEXT,"
        " advice TEXT,"
        " advice_model TEXT,"
        " updated_at TEXT);"),
]


def _record_migration(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at)"
        " VALUES (?, ?)",
        (version, datetime.now().isoformat(timespec="seconds")),
    )


def _schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0])


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开连接（row_factory=Row，外键约束开启）。"""
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path | None = None) -> None:
    """基线建表 + 增量迁移（幂等，可重复调用）。

    - 执行 §3 基线 SQL（version 1）并记录到 ``schema_migrations``；
    - 按 ``MIGRATIONS`` 顺序执行未应用的迁移并逐条记录版本；
    - 旧库（已有表但无版本表）自动按 version 1 基线补记，不重复建表。
    """
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        # 1) 基线（version 1）：全部 CREATE TABLE IF NOT EXISTS，幂等
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY,"
            " applied_at TEXT NOT NULL"
            ")"
        )
        if _schema_version(conn) < 1:
            _record_migration(conn, 1)
        # 2) 增量迁移
        for version, sql in MIGRATIONS:
            if version <= _schema_version(conn):
                continue
            conn.executescript(sql)
            _record_migration(conn, version)
        conn.commit()
    finally:
        conn.close()


def schema_version(db_path: str | Path | None = None) -> int:
    """当前 schema 版本（未初始化时为 0）。"""
    conn = connect(db_path)
    try:
        return _schema_version(conn)
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：请求级连接，正常结束时提交，始终关闭。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
