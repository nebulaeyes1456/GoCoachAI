"""局部死活提取器测试（M3/T5a）。

- 纯逻辑单测：信号扫描、裁剪选点、验题过滤、幂等入库、extract 端点校验
  （mock verify_position，不依赖引擎）；
- 端到端：真实 9 路 SGF（tests/fixtures）→ 复盘 → 提取 ≥ 1 道验证通过的题
  （需引擎与模型，skipUnless 保护；打印 [bench] 耗时与题数）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.common import db as db_mod  # noqa: E402
from backend.services.engine.verify import VerifyResult  # noqa: E402
from backend.services.problems import extractor, generator, store, utils  # noqa: E402

# 9 路 e2e 专用棋谱：互相打吃 razor（16 手）+ 黑 A9 脱先（第 17 手，
# 大失误信号）+ 提子交换 + 填充。e2e 用 standard 档复盘（fast 档 200v
# 噪声大使 best_coord/主题分类抖动，验题不可复现）。
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "review_9x9_extract.sgf"
ENGINE_EXE = PROJECT_ROOT / "engine" / "katago-eigenavx2.exe"
MODEL = PROJECT_ROOT / "engine" / "b10c128.bin.gz"

SGF_10 = (
    "(;GM[1]FF[4]CA[UTF-8]SZ[9]KM[7.5]PB[TestB]PW[TestW]"
    ";B[cc];W[gg];B[gc];W[cg];B[ee];W[ec];B[ed];W[dc];B[fd];W[fe])"
)


def _fake_verify(setup_sgf, candidates, profile="standard"):
    """第一个候选点 0.97，其余 0.1；pass 0.05（满足紧迫性）。"""
    out = []
    for i, c in enumerate(candidates):
        if c == "pass":
            out.append(VerifyResult(coord="pass", winrate=0.05, visits=600,
                                    pv=[], best_coord=""))
        else:
            winrate = 0.97 if i == 0 else 0.10
            out.append(VerifyResult(coord=c, winrate=winrate, visits=600,
                                    pv=[c, "E5"], best_coord="E5"))
    return out


def _weak_verify(setup_sgf, candidates, profile="standard"):
    return [
        VerifyResult(coord=c, winrate=0.6 if i == 0 else 0.2,
                     visits=100, pv=[c]) for i, c in enumerate(candidates)
    ]


def _row(move_number, category, delta, best_coord="E5"):
    return {
        "move_number": move_number,
        "color": "B" if move_number % 2 else "W",
        "coord": f"C{move_number}",
        "winrate": 0.5,
        "score_lead": 0.0,
        "visits": 100,
        "category": category,
        "delta": delta,
        "best_coord": best_coord,
        "pv": "[]",
    }


# ---------------------------------------------------------------------------
# 信号扫描
# ---------------------------------------------------------------------------


class TestScanSignals(unittest.TestCase):
    def test_three_kinds_and_order(self):
        rows = [
            _row(1, "normal", -0.01),
            _row(2, "blunder", -0.15),
            _row(3, "normal", 0.10),   # 与手2 反转 → swing 0.25
            _row(4, "normal", -0.02),
            _row(5, "question", -0.06),
            _row(6, "normal", 0.02),
            _row(7, "normal", -0.13),  # 终局（>5.6）波动 → endgame 0.13
        ]
        cfg = {
            "extract_signal_threshold": 0.06,
            "endgame_swing_threshold": 0.12,
            "endgame_move_fraction": 0.8,
        }
        signals = extractor.scan_signals(rows, 7, cfg)
        self.assertEqual([s["row"]["move_number"] for s in signals],
                         [3, 2, 7, 5])
        self.assertEqual([s["kind"] for s in signals],
                         ["swing", "blunder", "endgame", "question"])
        self.assertAlmostEqual(signals[0]["strength"], 0.25)

    def test_dedupe_keeps_strongest(self):
        rows = [_row(1, "blunder", -0.15), _row(2, "blunder", -0.30)]
        cfg = {
            "extract_signal_threshold": 0.06,
            "endgame_swing_threshold": 0.12,
            "endgame_move_fraction": 0.8,
        }
        signals = extractor.scan_signals(rows, 2, cfg)
        # 手2 既失误又反转（手1/手2 同为负不反转，仅失误）→ 每手一条
        self.assertEqual(len(signals), 2)
        self.assertEqual(signals[0]["row"]["move_number"], 2)
        self.assertAlmostEqual(signals[0]["strength"], 0.30)

    def test_delta_none_ignored(self):
        rows = [_row(1, "normal", None), _row(2, "blunder", None)]
        cfg = {
            "extract_signal_threshold": 0.06,
            "endgame_swing_threshold": 0.12,
            "endgame_move_fraction": 0.8,
        }
        self.assertEqual(extractor.scan_signals(rows, 2, cfg), [])


# ---------------------------------------------------------------------------
# 提取管线（mock verify_position）
# ---------------------------------------------------------------------------


class TestExtractMocked(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "test.db"
        db_mod.init_db(self.db)
        conn = db_mod.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO reviews (id, sgf_path, board_size, black, white,"
                " created_at, status, profile, progress)"
                " VALUES ('revx', ?, 9, 'B', 'W', '2026-01-01',"
                " 'done', 'fast', 1.0)",
                (str(self.root / "game.sgf"),),
            )
            for i, (cat, delta) in enumerate(
                [
                    ("normal", -0.01), ("blunder", -0.15), ("normal", 0.10),
                    ("blunder", -0.12), ("normal", -0.02), ("normal", 0.02),
                    ("question", -0.05), ("normal", 0.01),
                    ("normal", -0.13), ("normal", -0.01),
                ],
                start=1,
            ):
                conn.execute(
                    "INSERT INTO moves (review_id, move_number, color, coord,"
                    " winrate, category, delta, best_coord, pv)"
                    " VALUES ('revx', ?, 'B', ?, 0.5, ?, ?, 'E5', '[]')",
                    (i, f"C{i}", cat, delta),
                )
            conn.commit()
        finally:
            conn.close()
        (self.root / "game.sgf").write_text(SGF_10, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_extract_pipeline(self):
        with mock.patch.object(generator, "verify_position",
                               side_effect=_fake_verify):
            result = extractor.extract(
                review_id="revx", max_problems=5, target_rank=-5,
                db_path=self.db,
            )
        # 信号：swing@3(0.25) blunder@2(0.15) endgame@9(0.13)
        #       blunder@4(0.12) question@7(0.05) → 前 5 全验通过
        self.assertEqual(len(result["extracted"]), 5)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 0)
        for brief in result["extracted"]:
            self.assertRegex(brief["id"], r"^p[0-9a-f]{16}$")
            self.assertIn(brief["theme"], utils.VALID_THEMES)
            self.assertIsNotNone(brief["hint"])
            p = store.get_problem(brief["id"], self.db)
            self.assertIsNotNone(p)
            self.assertEqual(p["source"], "generated")
            branches = json.loads(p["branches"])
            self.assertEqual(branches["answer"]["coord"], p["answer"])
            self.assertGreater(branches["answer"]["winrate"], 0.95)

    def test_idempotent_rerun(self):
        with mock.patch.object(generator, "verify_position",
                               side_effect=_fake_verify):
            r1 = extractor.extract(
                review_id="revx", max_problems=5, db_path=self.db)
            r2 = extractor.extract(
                review_id="revx", max_problems=5, db_path=self.db)
        self.assertEqual(len(r1["extracted"]), 5)
        # 重复运行：同题面哈希幂等，不重复入库、不重复计数
        self.assertEqual(len(r2["extracted"]), 0)
        self.assertEqual(r2["skipped"], 5)
        items, total = store.list_problems(db_path=self.db)
        self.assertEqual(total, 5)

    def test_verification_reject(self):
        with mock.patch.object(generator, "verify_position",
                               side_effect=_weak_verify):
            result = extractor.extract(
                review_id="revx", max_problems=5, db_path=self.db)
        self.assertEqual(len(result["extracted"]), 0)
        self.assertEqual(result["failed"], 5)
        self.assertEqual(result["skipped"], 0)

    def test_max_problems_cap(self):
        with mock.patch.object(generator, "verify_position",
                               side_effect=_fake_verify):
            result = extractor.extract(
                review_id="revx", max_problems=2, db_path=self.db)
        self.assertEqual(len(result["extracted"]), 2)
        # 其余 3 个信号未验题即跳过
        self.assertEqual(result["skipped"], 3)

    def test_review_not_found(self):
        with self.assertRaises(extractor.ReviewNotFoundError):
            extractor.extract(review_id="missing", db_path=self.db)

    def test_missing_inputs(self):
        with self.assertRaises(ValueError):
            extractor.extract(sgf_text="", review_id=None, db_path=self.db)

    def test_sgf_text_path_uses_review_service(self):
        class FakeReviewSvc:
            def __init__(self) -> None:
                self.submitted = None

            def start(self) -> None:
                pass

            def submit(self, sgf_text, profile):
                self.submitted = (sgf_text, profile)
                return "revx"  # setUp 已预置 done + moves

        fake = FakeReviewSvc()
        with mock.patch(
            "backend.services.review.service.get_service",
            return_value=fake,
        ), mock.patch.object(generator, "verify_position",
                             side_effect=_fake_verify):
            result = extractor.extract(
                sgf_text=SGF_10, max_problems=2, db_path=self.db)
        self.assertEqual(len(result["extracted"]), 2)
        self.assertIsNotNone(fake.submitted)
        self.assertEqual(fake.submitted[0], SGF_10)
        from backend.common.settings import get_settings
        expected_profile = (get_settings().get("review") or {}).get("profile", "fast")
        self.assertEqual(fake.submitted[1], expected_profile)


# ---------------------------------------------------------------------------
# extract 端点（TestClient + mock extractor）
# ---------------------------------------------------------------------------


class TestExtractEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "goapp.db"
        db_mod.init_db(self.db_path)
        patcher = mock.patch.object(db_mod, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(__import__("backend.main", fromlist=["app"]).app)

    def tearDown(self) -> None:
        self.client.close()
        self._tmp.cleanup()

    def test_extract_requires_input(self):
        resp = self.client.post("/api/v1/problems/extract", json={})
        self.assertEqual(resp.status_code, 400)
        resp2 = self.client.post(
            "/api/v1/problems/extract",
            json={"sgf_text": "   ", "review_id": None},
        )
        self.assertEqual(resp2.status_code, 400)

    def test_extract_contract(self):
        brief = {
            "id": "p1234567890abcdef", "theme": "life_death",
            "setup_sgf": "(;GM[1]SZ[9];B[tt])", "rank_min": -8,
            "rank_max": -3, "hint": "黑先",
        }
        with mock.patch.object(
            extractor, "extract",
            return_value={"extracted": [brief], "failed": 1, "skipped": 2},
        ):
            resp = self.client.post(
                "/api/v1/problems/extract",
                json={"review_id": "r1", "max_problems": 3, "target_rank": -5},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["extracted"][0]["id"], brief["id"])
        self.assertEqual(body["extracted"][0]["theme"], "life_death")
        self.assertIn("setup_sgf", body["extracted"][0])
        self.assertIn("hint", body["extracted"][0])
        self.assertEqual(body["failed"], 1)
        self.assertEqual(body["skipped"], 2)

    def test_extract_busy_409(self):
        from backend.routers import problems as problems_router

        with problems_router._extract_lock:
            resp = self.client.post(
                "/api/v1/problems/extract", json={"review_id": "r1"})
        self.assertEqual(resp.status_code, 409)


# ---------------------------------------------------------------------------
# 端到端（真实引擎）
# ---------------------------------------------------------------------------


@unittest.skipUnless(ENGINE_EXE.exists() and MODEL.exists(), "需要引擎与模型")
class TestExtractEndToEnd(unittest.TestCase):
    """真实 9 路对局 → 复盘 → 提取 ≥ 1 道验证通过的题（≤ 5 分钟）。"""

    @classmethod
    def setUpClass(cls) -> None:
        db_mod.init_db()

    def test_e2e_extract_9x9(self):
        from backend.services.review import service as review_service

        svc = review_service.get_service()
        svc.start()
        sgf_text = FIXTURE.read_text(encoding="utf-8")
        # 清空题库：extract 幂等（INSERT OR IGNORE），库中已有同题时
        # extracted 不计入，先清库保证本次断言"新入库 ≥1"可复现。
        conn = db_mod.connect()
        try:
            conn.execute("DELETE FROM problems")
            conn.commit()
        finally:
            conn.close()
        t0 = time.time()
        result = extractor.extract(
            sgf_text=sgf_text, max_problems=1, target_rank=-5,
            review_profile="standard")
        elapsed = time.time() - t0
        print(
            f"\n[bench] 9 路 e2e: 提取 {len(result['extracted'])} 题, "
            f"失败 {result['failed']}, 跳过 {result['skipped']}, "
            f"耗时 {elapsed:.1f}s"
        )
        self.assertGreaterEqual(len(result["extracted"]), 1)
        self.assertLessEqual(elapsed, 300.0)
        for brief in result["extracted"]:
            p = store.get_problem(brief["id"])
            self.assertIsNotNone(p)
            self.assertEqual(p["source"], "generated")
            branches = json.loads(p["branches"])
            self.assertGreater(branches["answer"]["winrate"], 0.95)


if __name__ == "__main__":
    unittest.main()
