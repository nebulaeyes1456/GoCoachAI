"""import_library 容错单测（窗口3，mock 引擎）。

覆盖：SGF 约定解析（GM/SZ/RE/PB/PW/出题方）、坏文件跳过原因、
验证不通过跳过、幂等入库。
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
from backend.services.engine.verify import VerifyResult  # noqa: E402
from backend.services.problems import store  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import import_library  # noqa: E402

FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "library"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fake_verify(setup_sgf, candidates, profile="standard"):
    out = []
    for i, c in enumerate(candidates):
        if c == "pass":
            out.append(VerifyResult(coord="pass", winrate=0.05, visits=600))
        else:
            out.append(VerifyResult(coord=c, winrate=0.97 if i == 0 else 0.1,
                                    visits=600, pv=[c]))
    return out


class TestParseLibrarySgf(unittest.TestCase):
    def test_good(self):
        info, reason = import_library.parse_library_sgf(_read("good_life_death.sgf"))
        self.assertIsNone(reason, reason)
        self.assertEqual(info["theme"], "life_death")
        self.assertEqual(info["solver"], "B")  # 最后一手 W[fe] → 出题方白 → 黑先
        self.assertIn("黑先杀白", info["hint"])
        self.assertIn("先紧住白棋的气", info["hint"])
        self.assertEqual(info["size"], 9)

    def test_bad_no_theme(self):
        info, reason = import_library.parse_library_sgf(_read("bad_no_theme.sgf"))
        self.assertIsNone(info)
        self.assertIn("主题", reason)

    def test_bad_size(self):
        info, reason = import_library.parse_library_sgf(_read("bad_size.sgf"))
        self.assertIsNone(info)
        self.assertIn("SZ", reason)

    def test_bad_garbage(self):
        info, reason = import_library.parse_library_sgf(_read("bad_garbage.sgf"))
        self.assertIsNone(info)
        self.assertIn("SGF", reason)

    def test_missing_gm_and_sz(self):
        info, reason = import_library.parse_library_sgf("(;RE[middle])")
        self.assertIsNone(info)
        self.assertIn("GM", reason)
        info2, reason2 = import_library.parse_library_sgf("(;GM[1]RE[middle])")
        self.assertIsNone(info2)
        self.assertIn("SZ", reason2)


class TestImportOne(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "test.db"
        db_mod.init_db(self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_import_and_idempotent(self):
        text = _read("good_life_death.sgf")
        brief, reason = import_library.import_one(
            text, verify_fn=_fake_verify, db_path=self.db
        )
        self.assertIsNone(reason, reason)
        self.assertEqual(brief["theme"], "life_death")
        p = store.get_problem(brief["id"], self.db)
        self.assertEqual(p["source"], "imported")
        self.assertEqual(p["answer"], _candidates_first(text))
        # 二次导入幂等
        brief2, reason2 = import_library.import_one(
            text, verify_fn=_fake_verify, db_path=self.db
        )
        self.assertIsNone(brief2)
        self.assertIn("已存在", reason2)

    def test_verify_fail_skipped(self):
        def weak_verify(setup_sgf, candidates, profile="standard"):
            return [VerifyResult(coord=c, winrate=0.5, visits=100)
                    for c in candidates]

        brief, reason = import_library.import_one(
            _read("good_life_death.sgf"), verify_fn=weak_verify, db_path=self.db
        )
        self.assertIsNone(brief)
        self.assertIn("胜率", reason)

    def test_second_best_rejected(self):
        def race_verify(setup_sgf, candidates, profile="standard"):
            return [VerifyResult(coord=c, winrate=0.97, visits=100)
                    for c in candidates if c != "pass"]

        brief, reason = import_library.import_one(
            _read("good_life_death.sgf"), verify_fn=race_verify, db_path=self.db
        )
        self.assertIsNone(brief)
        self.assertIn("不唯一", reason)


def _candidates_first(text: str) -> str:
    info, _ = import_library.parse_library_sgf(text)
    return import_library._candidates(info)[0]


if __name__ == "__main__":
    unittest.main()
