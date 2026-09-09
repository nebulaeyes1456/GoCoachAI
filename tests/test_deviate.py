"""偏离惩罚查询测试（T4a）。

- 单元测试：mock KataGoEngine.query 构造响应，验证 winrate/score 差的方向
  （SIDETOMOVE 行棋方视角）、pass 跳过与 pv_step 映射、单步失败降级、
  maxVisits 覆盖、order 0 不在列表首位时"以引擎返回为准"；
- 真实引擎轻量用例：9 路 SGF 一手 PV 2~3 步，端到端单次 <30s。

运行：python -m unittest tests.test_deviate -v（项目根目录）
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.common import sgf_io  # noqa: E402
from backend.services.engine import deviate  # noqa: E402
from backend.services.engine.analyze_sgf import (  # noqa: E402
    _game_to_query,
    _resolve_paths,
)
from backend.services.engine.engine import EngineError, KataGoEngine  # noqa: E402

# 9 路测试 SGF（与 tests/test_review.py 相同，20 手合法序列）
SGF_9 = (
    "(;GM[1]FF[4]CA[UTF-8]SZ[9]KM[7.5]PB[TestB]PW[TestW]"
    ";B[cc];W[gg];B[gc];W[cg];B[ee];W[ec];B[ed];W[dc];B[fd];W[fe]"
    ";B[gd];W[ge];B[hd];W[he];B[ef];W[df];B[de];W[dd];B[cf];W[ce])"
)

_FAKE_PATHS = ("fake-katago.exe", "fake-model.bin.gz", "fake-analysis.cfg")


class _FakeEngine:
    """替代 KataGoEngine：记录请求，按末手构造响应（不触碰真实引擎）。"""

    def __init__(self, responder):
        self.responder = responder
        self.requests: list[dict] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def query(self, req, timeout=None):
        self.requests.append(req)
        return self.responder(req)


def _make_responder(
    pv,
    base_len=2,
    best_wr=0.62,
    best_score=3.0,
    dev_wr=0.55,
    dev_score=-1.0,
    fail_on=None,
    best_last=False,
):
    """构造响应：最优手 = 当前 PV 步（由请求 moves 长度推算 k）；次优手 = C3。

    ``best_last=False``：order 0 排在 moveInfos 首位（常规）；
    ``best_last=True``：order 0 排在列表末尾（验证"以引擎返回为准"）。
    """

    def responder(req):
        # 查询局面 = 局面 + PV 前 k-1 步 → k = len(moves) - base_len + 1
        k = len(req["moves"]) - base_len + 1
        best_move = pv[k - 1] if 1 <= k <= len(pv) else "?"
        if fail_on is not None and best_move == fail_on:
            raise EngineError("模拟查询失败: engine down")
        best = {
            "move": best_move,
            "order": 0,
            "winrate": best_wr,
            "scoreLead": best_score,
            "visits": 150,
            "pv": [best_move, "D5"],
        }
        dev = {
            "move": "C3",
            "order": 1,
            "winrate": dev_wr,
            "scoreLead": dev_score,
            "visits": 80,
            "pv": ["C3"],
        }
        return {
            "id": "x",
            "turnNumber": len(req["moves"]) - 1,
            "moveInfos": [dev, best] if best_last else [best, dev],
            "rootInfo": {
                "winrate": best_wr,
                "scoreLead": best_score,
                "visits": 200,
                "currentPlayer": "B",
            },
        }

    return responder


class TestDeviationUnit(unittest.TestCase):
    """mock 引擎：差值方向/口径、pass 跳过、pv_step 映射、失败降级。"""

    def _run(self, responder, moves, pv, setup_sgf="(;GM[1]SZ[9]KM[7.5])",
             **kw):
        fake = _FakeEngine(responder)
        with mock.patch.object(
            deviate, "_resolve_paths", return_value=_FAKE_PATHS
        ), mock.patch.object(deviate, "KataGoEngine", return_value=fake):
            results = deviate.query_deviation(setup_sgf, moves, pv, **kw)
        return fake, results

    def test_winrate_score_diff_direction_and_pass_skip(self):
        moves = [["B", "D4"], ["W", "H6"]]   # 2 手后轮到黑
        pv = ["E5", "F5", "pass", "G5"]
        fake, results = self._run(_make_responder(pv, base_len=2), moves, pv)
        # pass 步（pv 第 3 步）跳过：3 条结果
        self.assertEqual(len(results), 3)
        r0, r1, r2 = results
        self.assertEqual(r0.pv_step, 1)
        self.assertEqual(r0.pv_move, "E5")
        self.assertEqual(r0.deviation_move, "C3")
        self.assertAlmostEqual(r0.winrate_loss, 0.07, places=9)   # 0.62 - 0.55
        self.assertAlmostEqual(r0.score_loss, 4.0, places=9)      # 3.0 - (-1.0)
        self.assertEqual(r0.visits, 200)
        self.assertIsNone(r0.error)
        self.assertEqual(r1.pv_step, 2)
        self.assertEqual(r1.pv_move, "F5")
        self.assertEqual(r2.pv_step, 4)   # pv_step 与原始 PV 序号映射正确
        self.assertEqual(r2.pv_move, "G5")
        # 查询局面 = 局面 + moves + PV 前 k-1 步（不含当前 PV 步）
        self.assertEqual(len(fake.requests), 3)
        req1, req2, req3 = fake.requests
        self.assertEqual(req1["moves"], [["B", "D4"], ["W", "H6"]])
        self.assertEqual(req1["analyzeTurns"], [2])    # k=1：PV 前 0 步
        self.assertEqual(
            req2["moves"],
            [["B", "D4"], ["W", "H6"], ["B", "E5"]],
        )
        self.assertEqual(req2["analyzeTurns"], [3])    # k=2：PV 前 1 步
        self.assertEqual(
            req3["moves"],
            [["B", "D4"], ["W", "H6"], ["B", "E5"], ["W", "F5"],
             ["B", "pass"]],
        )
        self.assertEqual(req3["analyzeTurns"], [5])    # k=4：PV 前 3 步（含 pass）

    def test_max_visits_override(self):
        moves = [["B", "D4"]]
        fake, results = self._run(_make_responder(["E5"], base_len=1),
                                  moves, ["E5"], max_visits=50)
        self.assertEqual(len(results), 1)
        self.assertEqual(fake.requests[0]["maxVisits"], 50)
        # 默认值 200 也写入查询
        fake2, _ = self._run(_make_responder(["E5"], base_len=1),
                             moves, ["E5"])
        self.assertEqual(fake2.requests[0]["maxVisits"], 200)

    def test_negative_loss_when_deviation_better(self):
        # 偏离手反而更好（低 visits 噪声场景）：差值方向为 最优 − 偏离，可为负
        moves = [["B", "D4"]]
        _, results = self._run(
            _make_responder(["E5"], base_len=1, best_wr=0.62,
                            best_score=1.0, dev_wr=0.70, dev_score=2.5),
            moves, ["E5"],
        )
        self.assertAlmostEqual(results[0].winrate_loss, -0.08, places=9)
        self.assertAlmostEqual(results[0].score_loss, -1.5, places=9)

    def test_best_taken_from_engine_when_order0_not_first(self):
        # moveInfos 乱序时仍以 order==0 为最优手（以引擎返回为准）
        moves = [["B", "D4"]]
        _, results = self._run(
            _make_responder(["E5"], base_len=1, best_last=True),
            moves, ["E5"],
        )
        self.assertEqual(results[0].deviation_move, "C3")
        self.assertAlmostEqual(results[0].winrate_loss, 0.07, places=9)

    def test_query_failure_recorded_per_step(self):
        moves = [["B", "D4"], ["W", "H6"]]   # 2 手后轮到黑，PV 从黑开始
        pv = ["E5", "F5", "G5"]
        fake, results = self._run(
            _make_responder(pv, base_len=2, fail_on="F5"), moves, pv
        )
        self.assertEqual(len(results), 3)
        self.assertIsNone(results[0].error)
        self.assertIsNotNone(results[1].error)
        self.assertIn("模拟查询失败", results[1].error)
        self.assertIsNone(results[1].winrate_loss)
        self.assertIsNone(results[2].error)
        # 失败步骤不阻断后续：第 3 步查询局面仍按 PV 演进（含 F5）
        self.assertEqual(fake.requests[2]["moves"][-1], ["W", "F5"])

    def test_empty_pv_and_all_pass(self):
        fake, results = self._run(
            _make_responder([], base_len=1), [["B", "D4"]], []
        )
        self.assertEqual(results, [])
        self.assertEqual(fake.requests, [])
        fake2, results2 = self._run(
            _make_responder([], base_len=1), [["B", "D4"]], ["pass", "pass"]
        )
        self.assertEqual(results2, [])
        self.assertEqual(fake2.requests, [])

    def test_no_alternative_move(self):
        def responder(req):
            return {
                "moveInfos": [{"move": "E5", "order": 0, "winrate": 0.6,
                               "scoreLead": 1.0, "visits": 100}],
                "rootInfo": {"visits": 100},
            }

        _, results = self._run(responder, [["B", "D4"]], ["E5"])
        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0].error)
        self.assertIsNone(results[0].winrate_loss)


@unittest.skipUnless(
    (PROJECT_ROOT / "engine" / "katago-opencl.exe").exists()
    or (PROJECT_ROOT / "engine" / "katago-eigenavx2.exe").exists(),
    "需要引擎可执行文件",
)
@unittest.skipUnless(
    (PROJECT_ROOT / "engine" / "b10c128.bin.gz").exists(), "需要模型文件"
)
class TestRealEngine(unittest.TestCase):
    """真实引擎轻量用例：9 路 SGF 某手 PV 2~3 步，端到端 <30s。"""

    def test_deviation_end_to_end(self):
        parsed = sgf_io.parse_sgf(SGF_9)
        moves = [[c, "pass" if not p else p] for c, p in parsed.moves[:10]]
        # 先低 visits 查一次，取行棋方 PV（供 query_deviation 输入）
        executable, model, cfg_path = _resolve_paths()
        engine = KataGoEngine(executable, model, cfg_path, analysis_threads=1)
        try:
            engine.start()
            req = _game_to_query(SGF_9, "standard", [len(moves)])
            req["moves"] = moves
            req["maxVisits"] = 200
            resp = engine.query(req, timeout=120)
        finally:
            engine.stop()
        move_infos = resp.get("moveInfos") or []
        self.assertTrue(move_infos)
        best = next(
            (m for m in move_infos if m.get("order") == 0), move_infos[0]
        )
        pv = [str(p) for p in (best.get("pv") or [])][:3]
        self.assertGreaterEqual(len(pv), 2, "PV 至少 2 步")

        t0 = time.time()
        results = deviate.query_deviation(
            SGF_9, moves, pv, profile="standard", max_visits=200, timeout=120
        )
        elapsed = time.time() - t0
        print(f"\n[bench] 偏离惩罚查询耗时 {elapsed:.1f}s")
        self.assertLess(elapsed, 30)

        # 返回条目数 = PV 中非 pass 步数（pass 跳过）
        expected = sum(1 for m in pv if m.strip().lower() not in ("", "pass"))
        self.assertEqual(len(results), expected)
        self.assertTrue(results, "返回非空")
        ok = [r for r in results if r.error is None]
        self.assertTrue(ok, "至少一条无错误的偏离惩罚")
        for r in ok:
            self.assertIsNotNone(r.winrate_loss)
            self.assertIsNotNone(r.score_loss)
            self.assertGreaterEqual(r.winrate_loss, -0.3)
            self.assertLessEqual(r.winrate_loss, 1.0)
            self.assertGreaterEqual(r.visits or 0, 1)
            self.assertIsNotNone(r.deviation_move)
            # pv_move 可能等于 deviation_move：引擎以满 visits 重搜该局面时
            # 最优手与原 PV 不一致，原 PV 步落到 order 1（偏离手）属正常。
            self.assertNotIn(r.deviation_move, ("", "pass"))
            self.assertGreaterEqual(r.pv_step, 1)
        print(
            "[sample] "
            + json.dumps(
                [dataclasses.asdict(r) for r in results], ensure_ascii=False
            )
        )


if __name__ == "__main__":
    unittest.main()
