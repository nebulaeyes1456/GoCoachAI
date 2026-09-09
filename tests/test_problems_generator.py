"""错题生成器测试（窗口3）。

- 纯逻辑单测：候选点构造、主题过滤、验证规则（mock verify_position）；
- 端到端：真实 9 路 SGF（tests/fixtures）走复盘流水线后生成 ≥ 3 道
  验证通过的题（需引擎与模型，skipUnless 保护）。
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

from backend.common import db as db_mod  # noqa: E402
from backend.common import sgf_io  # noqa: E402
from backend.services.problems import generator, store, utils  # noqa: E402
from backend.services.engine.verify import VerifyResult  # noqa: E402

FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "review_9x9.sgf"
FIXTURE_B = PROJECT_ROOT / "tests" / "fixtures" / "review_9x9b.sgf"
ENGINE_EXE = PROJECT_ROOT / "engine" / "katago-eigenavx2.exe"
MODEL = PROJECT_ROOT / "engine" / "b10c128.bin.gz"

SGF_10 = (
    "(;GM[1]FF[4]CA[UTF-8]SZ[9]KM[7.5]PB[TestB]PW[TestW]"
    ";B[cc];W[gg];B[gc];W[cg];B[ee];W[ec];B[ed];W[dc];B[fd];W[fe])"
)


def _fake_verify(setup_sgf, candidates, profile="standard"):
    """第一个候选点 0.97，其余 0.1；pass 0.05（满足紧迫性）。"""
    out = []
    for c in candidates:
        if c == "pass":
            out.append(VerifyResult(coord="pass", winrate=0.05, visits=600,
                                    pv=[], best_coord=""))
        else:
            winrate = 0.97 if c == candidates[0] else 0.10
            out.append(VerifyResult(coord=c, winrate=winrate, visits=600,
                                    pv=[c, "E5"], best_coord="E5"))
    return out


class TestBuildCandidates(unittest.TestCase):
    def test_dedupe_and_occupied_filter(self):
        pos = {utils.coord_to_xy("E5"): "B"}
        cands = generator.build_candidates(
            "E4", pos, 9, best_coord="G3", candidate_radius=1,
            max_candidates=12, urgent=False,
        )
        self.assertEqual(len(cands), len(set(cands)))
        self.assertIn("G3", cands)          # best_coord 强制加入
        self.assertIn("E4", cands)          # 失误点
        self.assertNotIn("E5", cands)       # 已占点过滤

    def test_urgent_adds_pass_and_caps(self):
        pos = {}
        cands = generator.build_candidates(
            "E5", pos, 9, best_coord="E5", candidate_radius=1,
            max_candidates=5, urgent=True,
        )
        self.assertIn("pass", cands)
        self.assertEqual(len(set(cands)), len(cands))  # 无重复
        self.assertLessEqual(len(cands), 6)            # 上限 + pass


class TestGenerateMocked(unittest.TestCase):
    """生成器业务逻辑（mock 引擎验证）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "test.db"
        db_mod.init_db(self.db)
        # 伪造复盘记录 + moves 数据
        conn = db_mod.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO reviews (id, sgf_path, board_size, black, white,"
                " created_at, status, profile, progress)"
                " VALUES ('revtest', ?, 9, 'B', 'W', '2026-01-01',"
                " 'done', 'fast', 1.0)",
                (str(self.root / "game.sgf"),),
            )
            for i, (cat, delta) in enumerate(
                [("blunder", -0.12), ("question", -0.05), ("normal", -0.01),
                 ("blunder", -0.2), ("question", -0.04)],
                start=1,
            ):
                conn.execute(
                    "INSERT INTO moves (review_id, move_number, color, coord,"
                    " winrate, category, delta, best_coord, pv)"
                    " VALUES ('revtest', ?, 'B', ?, 0.4, ?, ?, 'E5', '[]')",
                    (i, f"C{i}", cat, delta),
                )
            conn.commit()
        finally:
            conn.close()
        (self.root / "game.sgf").write_text(SGF_10, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_generate_with_mocked_verify(self):
        with mock.patch.object(generator, "verify_position",
                               side_effect=_fake_verify):
            result = generator.generate(
                "revtest", themes=None, max_problems=10, target_rank=-5,
                db_path=self.db,
            )
        self.assertGreaterEqual(len(result["problems"]), 1)
        for brief in result["problems"]:
            self.assertIn(brief["theme"], utils.VALID_THEMES)
            self.assertRegex(brief["id"], r"^p[0-9a-f]{16}$")
            self.assertIsNotNone(brief["hint"])
        # 落库校验
        for brief in result["problems"]:
            p = store.get_problem(brief["id"], self.db)
            self.assertIsNotNone(p)
            branches = json.loads(p["branches"])
            self.assertEqual(branches["answer"]["coord"], p["answer"])

    def test_generate_theme_filter_and_idempotent(self):
        with mock.patch.object(generator, "verify_position",
                               side_effect=_fake_verify), \
             mock.patch.object(utils, "classify_theme",
                               return_value="life_death"):
            r1 = generator.generate(
                "revtest", themes=["life_death"], max_problems=10,
                db_path=self.db,
            )
            r2 = generator.generate(
                "revtest", themes=["life_death"], max_problems=10,
                db_path=self.db,
            )
        self.assertTrue(all(p["theme"] == "life_death" for p in r1["problems"]))
        self.assertEqual(
            [p["id"] for p in r1["problems"]], [p["id"] for p in r2["problems"]]
        )

    def test_generate_verification_reject(self):
        def weak_verify(setup_sgf, candidates, profile="standard"):
            return [
                VerifyResult(coord=c, winrate=0.6 if i == 0 else 0.2,
                             visits=100, pv=[c]) for i, c in enumerate(candidates)
            ]

        with mock.patch.object(generator, "verify_position",
                               side_effect=weak_verify):
            result = generator.generate(
                "revtest", max_problems=10, db_path=self.db
            )
        self.assertEqual(len(result["problems"]), 0)
        self.assertEqual(result["failed"], 4)  # 4 手失误全部正解胜率不足

    def test_generate_review_not_found(self):
        with self.assertRaises(generator.ReviewNotFoundError):
            generator.generate("missing", db_path=self.db)


@unittest.skipUnless(ENGINE_EXE.exists() and MODEL.exists(), "需要引擎与模型")
class TestGenerateEndToEnd(unittest.TestCase):
    """真实 9 路对局（含构造的决胜时刻）→ 复盘 → 生成 ≥ 3 道验证通过的题。"""

    @classmethod
    def setUpClass(cls) -> None:
        db_mod.init_db()  # 端到端走默认库 data/goapp.db，需先建表

    def _review(self, svc, fixture: Path) -> str:
        sgf_text = fixture.read_text(encoding="utf-8")
        review_id = svc.submit(sgf_text, profile="fast")
        deadline = time.time() + 1200
        status = "pending"
        while time.time() < deadline:
            conn = db_mod.connect()
            try:
                row = conn.execute(
                    "SELECT status FROM reviews WHERE id=?", (review_id,)
                ).fetchone()
            finally:
                conn.close()
            status = row["status"] if row else "pending"
            if status in ("done", "failed"):
                break
            time.sleep(1.0)
        self.assertEqual(status, "done", "复盘未完成")
        return review_id

    def test_e2e_generate(self):
        from backend.services.review import service as review_service

        svc = review_service.get_service()
        svc.start()
        # 两盘各含 2 个决胜时刻的对局，累计应能生成 ≥ 3 道题
        total = []
        for fixture in (FIXTURE, FIXTURE_B):
            if not fixture.exists():
                continue
            review_id = self._review(svc, fixture)
            t0 = time.time()
            result = generator.generate(
                review_id, themes=None, max_problems=2, target_rank=-5
            )
            elapsed = time.time() - t0
            print(f"\n[bench] {fixture.name}: 生成 {len(result['problems'])} 题，"
                  f"丢弃 {result['failed']}，耗时 {elapsed:.1f}s")
            total.extend(result["problems"])
        self.assertGreaterEqual(len(total), 3)
        for brief in total:
            p = store.get_problem(brief["id"])
            self.assertIsNotNone(p)
            branches = json.loads(p["branches"])
            self.assertGreater(branches["answer"]["winrate"], 0.95)


if __name__ == "__main__":
    unittest.main()
