"""判题单测（窗口3，mock 引擎）。

覆盖：正解/错解反馈结构、分支缓存命中（不启引擎）、错误答案补查、
幂等与 solved 语义。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.common import db as db_mod  # noqa: E402
from backend.services.problems import checker, store  # noqa: E402
from backend.services.engine.verify import VerifyResult  # noqa: E402

BRANCHES = {
    "solver": "B",
    "answer": {"coord": "E5", "winrate": 0.97, "pv": ["E5", "F5", "G5"]},
    "candidates": [
        {"coord": "E5", "winrate": 0.97, "pv": ["E5", "F5"]},
        {"coord": "D4", "winrate": 0.12, "pv": ["D4", "E5"]},
        {"coord": "pass", "winrate": 0.05, "pv": []},
    ],
}


def _sample(**overrides) -> dict:
    row = {
        "id": "pcheck1",
        "source": "generated",
        "review_id": "rev-1",
        "theme": "life_death",
        "rank_min": -9,
        "rank_max": -5,
        "setup_sgf": "(;GM[1]SZ[9]AB[ee]AW[dd])",
        "answer": "E5",
        "branches": json.dumps(BRANCHES),
        "verdict": "正解 E5",
        "hint": "黑先，做活",
        "explanation": None,
        "status": "active",
        "created_at": "2026-08-28T00:00:00+00:00",
    }
    row.update(overrides)
    return row


class TestChecker(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "test.db"
        db_mod.init_db(self.db)
        store.insert_problem(_sample(), self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_correct_answer(self):
        result = checker.attempt("pcheck1", " e5 ", db_path=self.db)
        self.assertTrue(result["correct"])
        self.assertTrue(result["solved"])
        self.assertIn("正解", result["response"])
        self.assertEqual(result["variation"], ["E5", "F5", "G5"])
        self.assertIsNone(result["explanation"])
        self.assertEqual(len(store.attempts_for("pcheck1", self.db)), 1)

    def test_wrong_answer_branch_hit_no_engine(self):
        # D4 在 branches.candidates 里 → 不调引擎
        with mock.patch.object(checker, "verify_position") as vp:
            result = checker.attempt("pcheck1", "D4", db_path=self.db)
        vp.assert_not_called()
        self.assertFalse(result["correct"])
        self.assertIn("12%", result["response"])
        self.assertIn("E5", result["response"])
        self.assertEqual(result["variation"], ["D4", "E5"])

    def test_wrong_answer_fallback_verify(self):
        fake = [VerifyResult(coord="F6", winrate=0.25, visits=600,
                             pv=["F6", "E5"])]
        with mock.patch.object(checker, "verify_position",
                               return_value=fake) as vp:
            result = checker.attempt("pcheck1", "F6", db_path=self.db)
        vp.assert_called_once()
        self.assertFalse(result["correct"])
        self.assertIn("25%", result["response"])
        self.assertEqual(result["variation"], ["F6", "E5"])
        self.assertFalse(result["solved"])

    def test_wrong_answer_engine_error_degrades(self):
        with mock.patch.object(checker, "verify_position",
                               side_effect=RuntimeError("engine down")):
            result = checker.attempt("pcheck1", "F6", db_path=self.db)
        self.assertFalse(result["correct"])
        self.assertIn("E5", result["response"])
        self.assertFalse(result["solved"])

    def test_idempotent_repeat_correct(self):
        r1 = checker.attempt("pcheck1", "E5", db_path=self.db)
        r2 = checker.attempt("pcheck1", "E5", db_path=self.db)
        self.assertEqual(r1, r2)  # 同一正解多次提交：响应幂等
        self.assertEqual(len(store.attempts_for("pcheck1", self.db)), 2)  # 仍累计

    def test_solved_persists_after_wrong(self):
        checker.attempt("pcheck1", "E5", db_path=self.db)
        result = checker.attempt("pcheck1", "D4", db_path=self.db)
        self.assertTrue(result["solved"])  # 已过题再答错仍为已过

    def test_missing_problem(self):
        with self.assertRaises(checker.ProblemNotFoundError):
            checker.attempt("missing", "E5", db_path=self.db)

    def test_explanation_returned_on_correct(self):
        store.update_problem(
            "pcheck1",
            {"explanation": json.dumps({"conclusion": "先做眼"},
                                       ensure_ascii=False)},
            self.db,
        )
        result = checker.attempt("pcheck1", "E5", db_path=self.db)
        self.assertIn("先做眼", result["explanation"])


if __name__ == "__main__":
    unittest.main()
