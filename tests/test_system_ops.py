"""T0 测试：迁移幂等 + 运维端点（health/cache/clear/db/backup/logs/version）。

- 数据库重定向到临时文件，不污染 data/goapp.db；
- 引擎相关检查用 mock（不依赖真机 GPU / 真实引擎进程）；
- db/backup 必须真实产生文件。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.common import db, serverlog
from backend.common.db import schema_version as db_schema_version
from backend.routers import system as system_mod
from backend.services.engine import backend as engine_backend
from backend.services.engine.backend import BackendChoice


class SystemOpsTestBase(unittest.TestCase):
    """公共脚手架：临时 DB / 日志 / 备份目录 + 引擎 mock。"""

    def setUp(self) -> None:
        engine_backend.reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "goapp.db"
        db.init_db(self.db_path)

        # 数据库重定向
        patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        # 日志重定向
        self.log_path = root / "app.log"
        serverlog.set_log_path(self.log_path)
        self.addCleanup(lambda: serverlog.set_log_path(None))

        # 备份目录重定向
        self.backup_dir = root / "backups"
        patcher = mock.patch.object(system_mod, "BACKUP_DIR", self.backup_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

        # 模型文件：真实存在的临时文件，health 的 model_file 检查指向它
        self.model_file = root / "b10c128.bin.gz"
        self.model_file.write_bytes(b"fake-model")

        # 引擎后端选择 mock：固定 eigenavx2，不发探测、不写 config
        choice = BackendChoice(
            name="eigenavx2",
            mode="auto",
            executable=str(root / "katago-eigenavx2.exe"),
            model=str(self.model_file),
            reason="测试 mock",
            detected=True,
        )
        patcher = mock.patch.object(engine_backend, "choose_backend", return_value=choice)
        patcher.start()
        self.addCleanup(patcher.stop)

        # 引擎存活状态 mock（默认未运行）
        self._alive = False
        patcher = mock.patch.object(
            engine_backend, "engine_alive", side_effect=lambda: self._alive
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            engine_backend,
            "alive_engine_count",
            side_effect=lambda: (1 if self._alive else 0),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        # settings mock：DeepSeek key 已配置（health 第 4 项确定性）
        fake_settings = {
            "coach": {"provider": "deepseek", "api_key": "sk-test"},
            "review": {"profile": "fast"},
            "budget": {"token_limit_month": 30},
            "server": {"port": 8765, "log_file": str(self.log_path)},
        }
        patcher = mock.patch.object(
            system_mod.settings, "get_settings", return_value=fake_settings
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        import backend.main  # noqa: F401  确保路由已注册

        self.client = TestClient(backend.main.app)

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()


class MigrationTest(SystemOpsTestBase):
    def test_init_db_idempotent_with_migration_table(self) -> None:
        db.init_db(self.db_path)
        db.init_db(self.db_path)  # 连跑两次：幂等，不报错
        conn = sqlite3.connect(self.db_path)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            rows = conn.execute(
                "SELECT version, applied_at FROM schema_migrations"
            ).fetchall()
        finally:
            conn.close()
        self.assertIn("schema_migrations", names)
        # 基线 SCHEMA_SQL 记为 v1，MIGRATIONS 从 2 递增
        self.assertEqual(
            [r[0] for r in rows],
            [1] + [v for v, _ in db.MIGRATIONS],
        )
        self.assertTrue(all(r[1] for r in rows))
        self.assertEqual(db.schema_version(self.db_path), len(db.MIGRATIONS) + 1)

    def test_incremental_migration_mechanism(self) -> None:
        """MIGRATIONS 追加后：新库跑全量、旧库只补增量（机制可工作）。"""
        base = db.schema_version(self.db_path)
        migration = (
            base + 1,
            "CREATE TABLE IF NOT EXISTS migration_test_t2 (id INTEGER PRIMARY KEY);",
        )
        db.MIGRATIONS.append(migration)
        try:
            # 旧库只补新追加的 migration
            db.init_db(self.db_path)
            self.assertEqual(db.schema_version(self.db_path), migration[0])
            conn = sqlite3.connect(self.db_path)
            try:
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                conn.close()
            self.assertIn("migration_test_t2", names)
            # 再跑一次：不重复执行（版本已记录）
            db.init_db(self.db_path)
            self.assertEqual(db.schema_version(self.db_path), migration[0])
        finally:
            db.MIGRATIONS.remove(migration)


class HealthEndpointTest(SystemOpsTestBase):
    def test_health_four_checks_structure(self) -> None:
        resp = self.client.get("/api/v1/system/health")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(
            set(body["checks"].keys()),
            {"database", "engine_process", "model_file", "deepseek_key"},
        )
        # 数据库连通
        self.assertTrue(body["checks"]["database"]["ok"])
        # 引擎进程：mock 未运行 → ok=false（按需启动，不算故障）
        self.assertFalse(body["checks"]["engine_process"]["ok"])
        # 模型文件存在（临时文件）
        self.assertTrue(body["checks"]["model_file"]["ok"])
        # DeepSeek key 已配置
        self.assertTrue(body["checks"]["deepseek_key"]["ok"])
        # 三项关键检查 ok → 整体 ok
        self.assertEqual(body["status"], "ok")

    def test_health_engine_process_alive(self) -> None:
        self._alive = True
        resp = self.client.get("/api/v1/system/health")
        body = resp.json()
        self.assertTrue(body["checks"]["engine_process"]["ok"])
        self.assertIn("1", body["checks"]["engine_process"]["detail"])


class CacheClearEndpointTest(SystemOpsTestBase):
    def _seed(self) -> None:
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO reviews (id, sgf_path, board_size, created_at,"
                " status) VALUES ('rev1', 'x.sgf', 19, '2026-08-31T00:00:00',"
                " 'done')"
            )
            conn.execute(
                "INSERT INTO moves (review_id, move_number, color, coord)"
                " VALUES ('rev1', 1, 'B', 'Q16')"
            )
            conn.execute(
                "INSERT INTO explanations (review_id, move_number, kind,"
                " content, created_at)"
                " VALUES ('rev1', 1, 'move', '{}', '2026-08-31T00:00:00')"
            )
            conn.execute(
                "INSERT INTO coach_asks (sgf_text, question, answer, created_at)"
                " VALUES ('(;)', 'q', '{}', '2026-08-31T00:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

    def _count(self, table: str) -> int:
        conn = db.connect()
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def test_clear_coach_only(self) -> None:
        self._seed()
        resp = self.client.post("/api/v1/system/cache/clear", json={"kind": "coach"})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["kind"], "coach")
        self.assertEqual(body["cleared"]["explanations"], 1)
        self.assertEqual(self._count("explanations"), 0)
        self.assertEqual(self._count("coach_asks"), 0)
        self.assertEqual(self._count("moves"), 1)  # review 缓存不动

    def test_clear_review_resets_done_reviews(self) -> None:
        self._seed()
        resp = self.client.post("/api/v1/system/cache/clear", json={"kind": "review"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._count("moves"), 0)
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT status FROM reviews WHERE id='rev1'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self._count("explanations"), 1)

    def test_clear_all(self) -> None:
        self._seed()
        resp = self.client.post("/api/v1/system/cache/clear", json={"kind": "all"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._count("explanations"), 0)
        self.assertEqual(self._count("moves"), 0)

    def test_clear_invalid_kind_400(self) -> None:
        resp = self.client.post("/api/v1/system/cache/clear", json={"kind": "zzz"})
        self.assertEqual(resp.status_code, 400)


class DbBackupEndpointTest(SystemOpsTestBase):
    def test_backup_creates_real_file(self) -> None:
        resp = self.client.post("/api/v1/system/db/backup")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertRegex(body["filename"], r"^goapp-\d{8}-\d{6}(-\d+)?\.db$")
        self.assertGreater(body["size_bytes"], 0)
        self.assertTrue(Path(body["path"]).is_absolute() or "backups" in body["path"])
        # 真实文件已生成
        files = list(self.backup_dir.glob("goapp-*.db"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, body["filename"])
        self.assertGreater(files[0].stat().st_size, 0)

    def test_backup_second_call_distinct_name(self) -> None:
        r1 = self.client.post("/api/v1/system/db/backup").json()
        r2 = self.client.post("/api/v1/system/db/backup").json()
        self.assertNotEqual(r1["filename"], r2["filename"])


class LogsEndpointTest(SystemOpsTestBase):
    def test_logs_returns_tail(self) -> None:
        serverlog.info("[server] 服务启动测试")
        serverlog.info("[engine] 后端选择测试")
        serverlog.info("[llm] move 测试摘要")
        resp = self.client.get("/api/v1/system/logs?lines=2")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["lines"], 2)
        self.assertEqual(len(body["log"]), 2)
        self.assertIn("[llm]", body["log"][-1])
        self.assertTrue(body["path"])

    def test_logs_default_lines(self) -> None:
        resp = self.client.get("/api/v1/system/logs")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json()["log"], list)


class VersionAndInfoEndpointTest(SystemOpsTestBase):
    def test_version_contract(self) -> None:
        resp = self.client.get("/api/v1/system/version")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["version"], "0.9.0")
        self.assertEqual(body["engine_backend"], "eigenavx2")
        # schema 版本随迁移递增：以数据库实际版本为准
        self.assertEqual(body["schema_version"], db_schema_version())
        self.assertEqual(body["api_prefix"], "/api/v1")

    def test_info_has_new_fields_and_bool_engine_ready(self) -> None:
        resp = self.client.get("/api/v1/system/info")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIsInstance(body["engine_ready"], bool)
        self.assertEqual(body["engine_backend"], "eigenavx2")
        self.assertEqual(body["schema_version"], db_schema_version())
        # 旧字段仍在（只增不改）
        for key in ("version", "model_ready", "profile",
                    "token_usage_month", "token_limit_month"):
            self.assertIn(key, body)


class EngineStatusEndpointTest(SystemOpsTestBase):
    def test_engine_status_structure(self) -> None:
        resp = self.client.get("/api/v1/system/engine/status")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["backend"], "eigenavx2")
        self.assertIsInstance(body["running"], bool)
        self.assertEqual(body["process_count"], 0 if not self._alive else 1)
        self.assertEqual(body["last_error"], "")

    def test_engine_restart_returns_ok(self) -> None:
        # mock：无真实引擎进程，仅验证端点存在与返回结构
        resp = self.client.post(
            "/api/v1/system/engine/restart", json={"backend": "cpu"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["backend"], "eigenavx2")

    def test_engine_restart_invalid_backend_400(self) -> None:
        resp = self.client.post(
            "/api/v1/system/engine/restart", json={"backend": "cuda"}
        )
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
