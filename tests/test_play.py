"""新手对弈板块单测：analyze_move 判定逻辑 + explain_tip 缓存。"""
import json
import tempfile
import unittest
from pathlib import Path

from backend.common import db
from backend.services.play import service
from backend.services.coach.llm import ChatResult


class FakeEngine:
    """mock KataGoEngine.query：按 moves 长度返回预置响应。"""

    def __init__(self, user_coord="E5", user_wr=0.39):
        self.user_coord = user_coord
        self.user_wr = user_wr
        self.queries = []

    def query(self, req):
        self.queries.append(req)
        last_color = req["moves"][-1][0]
        if last_color == "B":
            # 用户刚下完（轮到 AI）：一选 = AI 应手
            return {
                "rootInfo": {"winrate": 0.66, "scoreLead": 4.5},
                "moveInfos": [
                    {"order": 0, "move": "C3", "winrate": 0.66,
                     "pv": ["C3", "E7"]},
                ],
            }
        # 用户落子前（轮到用户）：一选 vs 用户实际下的点
        return {
            "rootInfo": {"winrate": 0.45, "scoreLead": -3.0},
            "moveInfos": [
                {"order": 0, "move": "D4", "winrate": 0.55,
                 "pv": ["D4", "D6"]},
                {"order": 1, "move": self.user_coord, "winrate": self.user_wr,
                 "pv": [self.user_coord]},
            ],
        }


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat_json(self, messages, schema_hint=None, kind="coach", db_path=None):
        self.calls.append(kind)
        return ChatResult(
            data={"problem": "断点问题", "reason": "没连上", "recommendation": "补断",
                  "proverb": "棋从断处生"},
            content="{}", model="fake", prompt_tokens=10, completion_tokens=10,
            cost=0.001,
        )


class PlayServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "goapp.db"
        db.init_db(self.db_path)
        service.set_engine(FakeEngine())
        self._saved_client = getattr(service, "_client", None)

    def tearDown(self):
        service.set_engine(None)
        service._client = self._saved_client
        self.tmp.cleanup()

    def test_bad_move_hint(self):
        moves = [["B", "E5"], ["W", "C3"], ["B", "G4"], ["W", "D6"],
                 ["B", "E5"]]
        r = service.analyze_move(9, moves)
        self.assertEqual(r["ai_move"], "C3")
        self.assertEqual(r["level"], "bad")
        self.assertAlmostEqual(r["delta"], 0.39 - 0.55, places=4)
        self.assertIsNotNone(r["hint"])
        # 棋语化提示：不再依赖坐标字母，改为方位+绿圈指引
        self.assertIn("绿圈", r["hint"]["text"])
        self.assertNotIn("D4", r["hint"]["text"])

    def test_ai_rank_injects_human_sl(self):
        """棋力档：Query A（AI 应手）注入 humanSLProfile+降 visits，Query B 判定不注入。"""
        engine = FakeEngine(user_wr=0.55)
        service.set_engine(engine)
        moves = [["B", "E5"], ["W", "C3"], ["B", "G4"]]
        r = service.analyze_move(9, moves, 6.5, ai_rank="15k")
        self.assertEqual(r["ai_rank"], "15k")
        # 第一次查询 = 完整局面（AI 应手）→ 有 humanSLProfile；第二次 = 判定 → 无
        self.assertEqual(len(engine.queries), 2)
        self.assertEqual(engine.queries[0].get("humanSLProfile"), {"rank": "15k"})
        self.assertEqual(engine.queries[0].get("maxVisits"), 90)
        self.assertNotIn("humanSLProfile", engine.queries[1])

    def test_ai_rank_none_full_strength(self):
        engine = FakeEngine(user_wr=0.55)
        service.set_engine(engine)
        moves = [["B", "E5"], ["W", "C3"], ["B", "G4"]]
        service.analyze_move(9, moves, 6.5, ai_rank=None)
        self.assertNotIn("humanSLProfile", engine.queries[0])
        # 不注入棋力档时保持 fast 档默认算力，不改动 maxVisits
        self.assertEqual(engine.queries[0].get("maxVisits"), 400)

    def test_ok_move_silent(self):
        engine = FakeEngine(user_coord="D4", user_wr=0.55)
        service.set_engine(engine)
        moves = [["B", "D4"], ["W", "C3"], ["B", "E5"]]
        r = service.analyze_move(9, moves)
        self.assertEqual(r["level"], "ok")
        self.assertIsNone(r["hint"])
        self.assertAlmostEqual(r["delta"], 0.0, places=4)

    def test_question_move(self):
        engine = FakeEngine(user_wr=0.49)
        service.set_engine(engine)
        moves = [["B", "E5"], ["W", "C3"], ["B", "E5"]]
        r = service.analyze_move(9, moves)
        self.assertEqual(r["level"], "question")
        self.assertEqual(r["hint"]["title"], "疑问手")

    def test_empty_moves_raises(self):
        with self.assertRaises(ValueError):
            service.analyze_move(9, [])

    def test_tip_cached_no_extra_llm(self):
        service._client = FakeClient()
        moves = [["B", "E5"], ["W", "C3"], ["B", "G4"]]
        first = service.explain_tip(9, moves, "G4", "D4", -0.16, "bad",
                                    db_path=self.db_path)
        second = service.explain_tip(9, moves, "G4", "D4", -0.16, "bad",
                                     db_path=self.db_path)
        self.assertEqual(first["content"]["proverb"], "棋从断处生")
        self.assertEqual(second["content"], first["content"])
        self.assertEqual(len(service._client.calls), 1)


if __name__ == "__main__":
    unittest.main()
