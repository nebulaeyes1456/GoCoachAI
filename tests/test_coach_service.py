"""窗口2 单测：教练服务（mock 复盘兜底、缓存、限额、variation 红线）。"""
import json
import tempfile
import unittest
from pathlib import Path

from backend.common import cost, db
from backend.services.coach import service
from backend.services.coach.llm import ChatResult


class FakeClient:
    """替代 LLMClient：不访问网络，返回预置 JSON。"""

    def __init__(self, data: dict, model: str = "fake-model", call_cost: float = 0.001):
        self.data = data
        self.model = model
        self.call_cost = call_cost
        self.calls: list[tuple] = []  # (kind, schema_hint)

    def chat_json(self, messages, schema_hint=None, kind="coach", db_path=None):
        self.calls.append((kind, schema_hint))
        # 与真实 LLMClient 一致：成功调用即记账（缓存命中不经过这里）
        cost.record_call(self.model, 100, 50, self.call_cost, kind=kind, db_path=db_path)
        return ChatResult(
            data=dict(self.data), content=json.dumps(self.data, ensure_ascii=False),
            model=self.model, prompt_tokens=100, completion_tokens=50,
            cost=self.call_cost,
        )


EXPLAIN_DATA = {
    "problem": "这手棋把外面的断点送给了白棋。",
    "reason": "因为 P11 与周围棋子没有连上，白棋 P10 一断即可分断。",
    "recommendation": "推荐下在 P10：先补住断点，再回头处理下边。",
    "variation": ["P10", "X1"],  # X1 不在 PV 中，应被清洗
    "takeaway": "落子前先看断点。",
    "level_note": "对 5K 来说，补断优先。",
}

SUMMARY_DATA = {
    "opening": "布局阶段双方互占大场，黑棋略缓。",
    "middle": "中盘第 37 手的失误让黑棋丢掉主动权。",
    "endgame": "官子阶段双方收官平稳。",
    "strengths": ["局部计算有耐心", "局面落后不慌乱"],
    "weaknesses": ["断点意识不足", "中盘作战方向不清"],
    "suggestions": ["本周练习接触战断点 10 题", "复盘每局自己找出 3 个断点"],
}


class CoachServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "goapp.db"
        db.init_db(self.db_path)
        self._saved_client = service._client
        service._client = FakeClient(EXPLAIN_DATA)

    def tearDown(self) -> None:
        service._client = self._saved_client
        self.tmp.cleanup()

    # ---------------------------------------------------------- explain
    def test_explain_uses_mock_review_fallback(self):
        """moves 表为空时回退到 tests/mock_review.json。"""
        result = service.explain("mock-review-001", 37, db_path=self.db_path)
        self.assertEqual(result["move_number"], 37)
        self.assertEqual(result["kind"], "move")
        content = result["content"]
        for key in ("problem", "reason", "recommendation", "variation", "takeaway"):
            self.assertIn(key, content)
        # 质量红线：variation 只含传入 PV 中的着法（X1 被清洗）
        self.assertEqual(content["variation"], ["P10"])
        self.assertEqual(result["model"], "fake-model")

    def test_explain_cache_second_hit_no_extra_call(self):
        first = service.explain("mock-review-001", 37, db_path=self.db_path)
        second = service.explain("mock-review-001", 37, db_path=self.db_path)
        self.assertEqual(first["content"], second["content"])
        client: FakeClient = service._client
        self.assertEqual(len([c for c in client.calls if c[0] == "move"]), 1)
        # 成本只记一次
        self.assertAlmostEqual(cost.month_cost(db_path=self.db_path), 0.001)

    def test_explain_move_not_found(self):
        with self.assertRaises(service.MoveNotFoundError):
            service.explain("mock-review-001", 999, db_path=self.db_path)

    def test_explain_review_not_found(self):
        with self.assertRaises(service.ReviewNotFoundError):
            service.explain("no-such-review", 1, db_path=self.db_path)

    # ---------------------------------------------------------- summary
    def test_summary_six_fields(self):
        service._client = FakeClient(SUMMARY_DATA)
        result = service.summary("mock-review-001", db_path=self.db_path)
        content = result["content"]
        for key in ("opening", "middle", "endgame", "strengths", "weaknesses", "suggestions"):
            self.assertIn(key, content)
        self.assertEqual(len(content["suggestions"]), 2)

    def test_summary_cache(self):
        service._client = FakeClient(SUMMARY_DATA)
        service.summary("mock-review-001", db_path=self.db_path)
        service.summary("mock-review-001", db_path=self.db_path)
        client: FakeClient = service._client
        self.assertEqual(len([c for c in client.calls if c[0] == "summary"]), 1)

    # ---------------------------------------------------------- ask
    def test_ask_records_to_coach_asks(self):
        service._client = FakeClient({
            "conclusion": "这里应该断。",
            "reasoning": "断后白棋两块不安。",
            "variation": ["E16"],
            "kata_winrate": 0.62,
        })
        result = service.ask("(;SZ[19];B[pd])", "这里该不该断？", "-5", db_path=self.db_path)
        self.assertEqual(result["answer"]["conclusion"], "这里应该断。")
        conn = db.connect(self.db_path)
        try:
            row = conn.execute("SELECT COUNT(*) FROM coach_asks").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 1)

    # ---------------------------------------------------------- 限额
    def test_budget_exceeded_raises(self):
        # 预置一条 30 元调用，达到默认限额 → 402 语义异常
        cost.record_call("deepseek-chat", 0, 0, 30.0, kind="move", db_path=self.db_path)
        with self.assertRaises(service.BudgetExceededError):
            service.explain("mock-review-001", 37, db_path=self.db_path)

    # ---------------------------------------------------------- variation 红线
    def test_sanitize_variation_keeps_only_pv(self):
        pv = ["P10", "Q10", "P9"]
        self.assertEqual(service.sanitize_variation(["P10", "X1", "Q10"], pv), ["P10", "Q10"])
        self.assertEqual(service.sanitize_variation(["X1"], pv), pv)   # 全部非法 → 直接用 PV
        self.assertEqual(service.sanitize_variation([], pv), pv)
        self.assertEqual(service.sanitize_variation("坏输入", pv), pv)
        self.assertEqual(service.sanitize_variation(["P10"], []), [])


    # ---------------------------------------------------------- penalty
    def test_penalty_messages_multistep_lecture(self):
        """惩罚变化 prompt：多手连讲（先手/好手/俗手/应对）+ 总结要求。"""
        from backend.services.coach import prompts

        msgs = prompts.build_penalty_messages(
            {"winrate_curve": [
                {"move": 36, "color": "W", "coord": "P11", "winrate": 0.4, "delta": 0.01},
                {"move": 37, "color": "B", "coord": "P10", "winrate": 0.3,
                 "delta": -0.2, "category": "blunder", "best_coord": "Q10"},
            ]}, 37, [["W", "Q10"], ["B", "Q11"]], 0.62, "-5")
        self.assertEqual(len(msgs), 2)
        user = msgs[1]["content"]
        self.assertIn("第1步：白 Q10", user)
        self.assertIn("第2步：黑 Q11", user)
        for keyword in ("先手", "好手", "俗手", "应对", "summary", "局部厮杀"):
            self.assertIn(keyword, user)

    def test_penalty_cached_with_steps_returns_without_engine(self):
        """(review_id, move, penalty) 缓存含分步解说 → 直接返回，不再算 KataGo/LLM。"""
        content = {
            "penalty_pv": ["Q10", "Q11"],
            "punisher": "W",
            "punisher_winrate": 0.62,
            "steps": [{"step": 1, "role": "好手", "text": "严厉的惩罚点。"}],
            "summary": "黑被惩罚，亏了。",
            "focused": True,
        }
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO explanations"
                " (review_id, move_number, kind, content, model, cost, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                ("mock-review-001", 37, "penalty",
                 json.dumps(content, ensure_ascii=False), "kata", 0.0, "2026-09-02T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()
        result = service.penalty("mock-review-001", 37, db_path=self.db_path)
        self.assertEqual(result["kind"], "penalty")
        self.assertEqual(result["content"]["steps"][0]["role"], "好手")

    def test_penalty_move_not_found(self):
        with self.assertRaises(service.MoveNotFoundError):
            service.penalty("mock-review-001", 999, db_path=self.db_path)


if __name__ == "__main__":
    unittest.main()
