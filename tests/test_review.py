"""复盘后端测试（窗口1）。

- 关键手判定单元测试（构造胜率序列，不依赖引擎）；
- build_move_rows 换算逻辑测试（SIDETOMOVE → 当前方 before/after/delta）；
- 端到端：9 路 SGF 分析（需引擎与模型，耗时约 1~3 分钟）；
- 缓存：同 SGF 同 profile 二次 submit 直接命中（< 2 秒）。

运行：python -m unittest tests.test_review -v（项目根目录）
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.common import db as db_mod  # noqa: E402
from backend.services.review.classify import (  # noqa: E402
    Thresholds,
    classify_move,
    is_handicap_game,
)
from backend.services.review.service import build_move_rows, get_service  # noqa: E402
from backend.services.engine.analyze_sgf import TurnInfo  # noqa: E402

# 9 路测试 SGF（20 手，合法序列）
SGF_9 = (
    "(;GM[1]FF[4]CA[UTF-8]SZ[9]KM[7.5]PB[TestB]PW[TestW]"
    ";B[cc];W[gg];B[gc];W[cg];B[ee];W[ec];B[ed];W[dc];B[fd];W[fe]"
    ";B[gd];W[ge];B[hd];W[he];B[ef];W[df];B[de];W[dd];B[cf];W[ce])"
)

ENGINE_EXE = PROJECT_ROOT / "engine" / "katago-eigenavx2.exe"
MODEL = PROJECT_ROOT / "engine" / "b10c128.bin.gz"


class TestClassify(unittest.TestCase):
    """§4.1 阈值规则（阈值放 config，这里用默认值）。"""

    def setUp(self) -> None:
        self.t = Thresholds(blunder=0.12, question=0.06, good=0.06)

    def test_blunder(self) -> None:
        self.assertEqual(classify_move(-0.121, "R4", "Q16", self.t), "blunder")
        self.assertEqual(classify_move(-0.12, "R4", "Q16", self.t), "blunder")

    def test_question(self) -> None:
        self.assertEqual(classify_move(-0.061, "R4", "Q16", self.t), "question")
        self.assertEqual(classify_move(-0.12, "R4", "Q16", self.t), "blunder")
        self.assertEqual(classify_move(-0.06, "R4", "Q16", self.t), "question")

    def test_good_requires_different_best(self) -> None:
        # delta >= +0.06 且最佳点 != 落点 → good
        self.assertEqual(classify_move(+0.07, "R4", "Q16", self.t), "good")
        # 最佳点 == 落点 → normal
        self.assertEqual(classify_move(+0.07, "Q16", "Q16", self.t), "normal")

    def test_normal(self) -> None:
        self.assertEqual(classify_move(-0.02, "R4", "Q16", self.t), "normal")
        self.assertEqual(classify_move(+0.03, "R4", "Q16", self.t), "normal")

    def test_none_delta(self) -> None:
        self.assertEqual(classify_move(None, "R4", "Q16", self.t), "normal")

    def test_handicap_relax(self) -> None:
        t_relaxed = Thresholds(blunder=0.12, question=0.06, good=0.06, relax=1.5)
        # -0.10 在放宽后（0.18 阈值）不再是 blunder，是 question
        self.assertEqual(classify_move(-0.10, "R4", "Q16", t_relaxed), "question")
        self.assertEqual(classify_move(-0.19, "R4", "Q16", t_relaxed), "blunder")

    def test_is_handicap(self) -> None:
        self.assertFalse(is_handicap_game(SGF_9))
        self.assertTrue(is_handicap_game("(;SZ[19]HA[4];B[pd])"))
        self.assertTrue(is_handicap_game("(;SZ[19]AB[pd][dp])"))


class TestBuildMoveRows(unittest.TestCase):
    """SIDETOMOVE 视角 → 每手 before/after/delta 换算。"""

    def _turns(self, winrates: list[float]) -> list[TurnInfo]:
        return [
            TurnInfo(turn_number=i, winrate=w, score_lead=1.0, visits=200,
                     move_infos=[{"move": "D4", "order": 0, "winrate": w,
                                  "scoreLead": 1.0, "visits": 200,
                                  "pv": ["D4", "G5"]}])
            for i, w in enumerate(winrates)
        ]

    def test_delta_conversion(self) -> None:
        # 20 手 SGF；winrate 序列：t0=0.50（黑方）, t1=0.60（白方）...
        winrates = [0.50, 0.60, 0.55, 0.80, 0.30] + [0.50] * 15
        rows = build_move_rows(SGF_9, self._turns(winrates), None)
        self.assertEqual(len(rows), 20)
        # 第 1 手：before=0.50, after=1-0.60=0.40, delta=-0.10 → question（阈值 12p）
        self.assertAlmostEqual(rows[0][3], 0.40, places=6)   # winrate=after
        self.assertAlmostEqual(rows[0][7], -0.10, places=6)  # delta
        self.assertEqual(rows[0][6], "question")
        # 第 2 手：before=0.60, after=1-0.55=0.45, delta=-0.15 → blunder
        self.assertAlmostEqual(rows[1][7], -0.15, places=6)
        self.assertEqual(rows[1][6], "blunder")
        # 第 3 手：before=0.55, after=1-0.80=0.20, delta=-0.35 → blunder
        self.assertEqual(rows[2][6], "blunder")
        # 第 4 手：before=0.80, after=1-0.30=0.70, delta=-0.10 → question（阈值 12p）
        self.assertEqual(rows[3][6], "question")
        # 第 5 手：before=0.30, after=1-0.50=0.50, delta=+0.20，
        # 最佳点 E5 != 落点 → good
        self.assertEqual(rows[4][6], "good")

    def test_last_move_delta_from_final_turn(self) -> None:
        winrates = [0.50] * 20
        rows = build_move_rows(
            SGF_9, self._turns(winrates), {"winrate": 0.40, "score_lead": 0}
        )
        # 最后手：before=0.50, after=1-0.40=0.60, delta=+0.10
        self.assertAlmostEqual(rows[-1][3], 0.60, places=6)
        self.assertAlmostEqual(rows[-1][7], 0.10, places=6)
        # 其余手 after=1-0.50=0.50, delta=0
        self.assertAlmostEqual(rows[0][7], 0.0, places=6)

    def test_last_move_no_final_turn(self) -> None:
        winrates = [0.50] * 20
        rows = build_move_rows(SGF_9, self._turns(winrates), None)
        self.assertIsNone(rows[-1][7])          # delta=None
        self.assertEqual(rows[-1][6], "normal")  # 无 delta 不算关键手

    def test_pv_serialized(self) -> None:
        import json

        rows = build_move_rows(SGF_9, self._turns([0.50] * 20), None)
        pv = json.loads(rows[0][9])
        self.assertIsInstance(pv, list)
        self.assertGreater(len(pv), 0)


@unittest.skipUnless(ENGINE_EXE.exists() and MODEL.exists(), "需要引擎与模型")
class TestEndToEnd(unittest.TestCase):
    """9 路 SGF 端到端：analyze → 状态 → 复盘数据。"""

    @classmethod
    def setUpClass(cls) -> None:
        db_mod.init_db()  # 端到端走默认库 data/goapp.db，需先建表

    def test_full_pipeline(self) -> None:
        from backend.services.engine.analyze_sgf import analyze_sgf

        t0 = time.time()
        result = analyze_sgf(SGF_9, profile="fast")
        elapsed = time.time() - t0
        print(f"\n[bench] 9 路 20 手 fast 档分析耗时 {elapsed:.1f}s")
        turns = result.turns
        self.assertEqual(len(turns), 20)
        # 每局面字段完整
        for t in turns:
            self.assertIsNotNone(t.winrate)
            self.assertIsNotNone(t.score_lead)
            self.assertIsNotNone(t.visits)
            self.assertIsNotNone(t.best_coord)
            self.assertGreaterEqual(t.visits, 1)
            self.assertGreaterEqual(t.pv.__len__(), 0)
        # 最后一局面（turn 20）也应存在，用于最后手 delta
        self.assertIsNotNone(result.final_turn)
        self.assertIsNotNone(result.final_turn.winrate)
        # 坐标一致性：kata 输出 best_coord 为 GTP 风格（9 路列 A~H/J）
        for t in turns:
            if t.best_coord:
                self.assertRegex(t.best_coord, r"^[A-HJ][1-9]$")

        rows = build_move_rows(SGF_9, turns, None)
        self.assertEqual(len(rows), 20)
        for row in rows:
            num, color, coord = row[0], row[1], row[2]
            self.assertIn(color, ("B", "W"))
            self.assertRegex(coord, r"^[A-HJ][1-9]$")
            self.assertIn(row[6], ("blunder", "question", "good", "normal"))

    def test_review_service_and_cache(self) -> None:
        """submit → 轮询 → 数据完整；二次 submit 命中缓存 < 2s。"""
        svc = get_service()
        svc.start()
        review_id = svc.submit(SGF_9, profile="fast")
        self.assertEqual(len(review_id), 16)

        deadline = time.time() + 1200
        status = "pending"
        while time.time() < deadline:
            conn = db_mod.connect()
            try:
                row = conn.execute(
                    "SELECT status, progress FROM reviews WHERE id=?",
                    (review_id,),
                ).fetchone()
            finally:
                conn.close()
            if row:
                status = row["status"]
                if status in ("done", "failed"):
                    break
            time.sleep(1.0)
        self.assertEqual(status, "done", f"分析未完成，状态={status}")

        conn = db_mod.connect()
        try:
            moves = conn.execute(
                "SELECT COUNT(*) AS c FROM moves WHERE review_id=?", (review_id,)
            ).fetchone()["c"]
            row = conn.execute(
                "SELECT progress FROM reviews WHERE id=?", (review_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(moves, 20)
        self.assertEqual(row["progress"], 1.0)

        # 缓存：同 SGF 同 profile 再次提交应立即返回且不重跑
        t0 = time.time()
        rid2 = svc.submit(SGF_9, profile="fast")
        dt = time.time() - t0
        self.assertEqual(rid2, review_id)
        self.assertLess(dt, 2.0, f"缓存未命中，耗时 {dt:.2f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
