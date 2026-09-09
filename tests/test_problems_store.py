"""题目数据层单测（窗口3，不依赖引擎）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.common import db as db_mod  # noqa: E402
from backend.services.problems import store  # noqa: E402


def _sample(**overrides) -> dict:
    row = {
        "id": "p000000000000001",
        "source": "generated",
        "review_id": "rev-1",
        "theme": "life_death",
        "rank_min": -9,
        "rank_max": -5,
        "setup_sgf": "(;GM[1]SZ[9];B[tt])",
        "answer": "E5",
        "branches": json.dumps({"solver": "B", "answer": {"coord": "E5"}}),
        "verdict": "正解 E5",
        "hint": "黑先，做活",
        "explanation": None,
        "status": "active",
        "created_at": "2026-08-28T00:00:00+00:00",
    }
    row.update(overrides)
    return row


class TestStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "test.db"
        db_mod.init_db(self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_insert_get_idempotent(self):
        self.assertTrue(store.insert_problem(_sample(), self.db))
        self.assertFalse(store.insert_problem(_sample(), self.db))  # 幂等
        p = store.get_problem("p000000000000001", self.db)
        self.assertEqual(p["theme"], "life_death")
        self.assertEqual(p["answer"], "E5")
        self.assertIsNone(store.get_problem("missing", self.db))

    def test_update_and_list_filters(self):
        store.insert_problem(_sample(id="p1", theme="life_death",
                                     rank_min=-9, rank_max=-5), self.db)
        store.insert_problem(_sample(id="p2", theme="middle",
                                     rank_min=-10, rank_max=3), self.db)
        store.insert_problem(_sample(id="p3", theme="life_death",
                                     rank_min=-5, rank_max=-1,
                                     status="inactive"), self.db)
        store.update_problem("p1", {"hint": "新提示"}, self.db)
        self.assertEqual(store.get_problem("p1", self.db)["hint"], "新提示")

        items, total = store.list_problems(theme="life_death", db_path=self.db)
        self.assertEqual(total, 1)  # p3 非 active 不计
        self.assertEqual(items[0]["id"], "p1")

        # 级位筛选：rank=-7 落在 p1(-9..-5) 内，不在 p2(-10..3) 外
        items, total = store.list_problems(rank=-7, db_path=self.db)
        ids = {p["id"] for p in items}
        self.assertIn("p1", ids)
        self.assertIn("p2", ids)

    def test_attempts(self):
        store.insert_problem(_sample(), self.db)
        pid = "p000000000000001"
        store.add_attempt(pid, "E5", True, self.db)
        store.add_attempt(pid, "D4", False, self.db)
        self.assertTrue(store.has_correct_attempt(pid, self.db))
        self.assertEqual(len(store.attempts_for(pid, self.db)), 2)
        self.assertFalse(store.has_correct_attempt("missing", self.db))

    def test_branches_json_bad_data(self):
        store.insert_problem(_sample(branches="不是JSON"), self.db)
        p = store.get_problem("p000000000000001", self.db)
        self.assertEqual(store.branches_json(p), {})


if __name__ == "__main__":
    unittest.main()
