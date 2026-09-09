"""数据库建表测试：幂等 + 5 张表存在。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.common import db


class InitDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "goapp.db"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_init_db_creates_all_tables(self):
        db.init_db(self.db_path)
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
        # AUTOINCREMENT 会产生内部表 sqlite_sequence，此处只断言 5 张业务表齐全
        self.assertTrue(
            {"reviews", "moves", "explanations", "problems", "attempts"} <= names
        )

    def test_init_db_is_idempotent(self):
        db.init_db(self.db_path)
        db.init_db(self.db_path)  # 第二次不应报错
        db.init_db(self.db_path)  # 第三次同样安全

    def test_foreign_keys_enabled(self):
        conn = db.connect(self.db_path)
        try:
            row = conn.execute("PRAGMA foreign_keys").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 1)


if __name__ == "__main__":
    unittest.main()
