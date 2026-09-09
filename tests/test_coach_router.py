"""窗口2 单测：路由层契约（§4.2 响应结构、402/404 语义、system/info 用量）。

使用 TestClient + 注入 FakeClient（不访问网络），数据库路径重定向到临时文件。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.common import cost, db
from backend.services.coach import service
from tests.test_coach_service import EXPLAIN_DATA, SUMMARY_DATA, FakeClient


class CoachRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "goapp.db"
        db.init_db(self.db_path)
        # 重定向数据库，避免污染 data/goapp.db
        patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._saved_client = service._client
        service._client = FakeClient(EXPLAIN_DATA)
        self.client = TestClient(__import__("backend.main", fromlist=["app"]).app)

    def tearDown(self) -> None:
        service._client = self._saved_client
        self.client.close()
        self.tmp.cleanup()

    def test_explain_contract(self):
        resp = self.client.post(
            "/api/v1/coach/explain",
            json={"review_id": "mock-review-001", "move_number": 37},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["move_number"], 37)
        self.assertEqual(body["kind"], "move")
        for key in ("problem", "reason", "recommendation", "variation", "takeaway"):
            self.assertIn(key, body["content"])
        # 质量红线：variation 只含传入 PV
        self.assertEqual(body["content"]["variation"], ["P10"])
        self.assertEqual(body["model"], "fake-model")
        self.assertIsInstance(body["cost"], float)

    def test_explain_404_when_review_missing(self):
        resp = self.client.post(
            "/api/v1/coach/explain",
            json={"review_id": "no-such-review", "move_number": 1},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertIn("detail", resp.json())

    def test_explain_402_when_budget_exceeded(self):
        cost.record_call("deepseek-chat", 0, 0, 30.0, kind="move", db_path=self.db_path)
        resp = self.client.post(
            "/api/v1/coach/explain",
            json={"review_id": "mock-review-001", "move_number": 37},
        )
        self.assertEqual(resp.status_code, 402)
        self.assertEqual(resp.json()["detail"], "本月预算已用尽")

    def test_summary_contract(self):
        service._client = FakeClient(SUMMARY_DATA)
        resp = self.client.post("/api/v1/coach/summary",
                                json={"review_id": "mock-review-001"})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["kind"], "summary")
        for key in ("opening", "middle", "endgame", "strengths", "weaknesses", "suggestions"):
            self.assertIn(key, body["content"])

    def test_ask_contract(self):
        service._client = FakeClient({
            "conclusion": "这里应该断。", "reasoning": "断后白棋两块不安。",
            "variation": ["E16"], "kata_winrate": 0.62,
        })
        resp = self.client.post(
            "/api/v1/coach/ask",
            json={"sgf_text": "(;SZ[19];B[pd])", "question": "这里该不该断？", "level": "-5"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        for key in ("conclusion", "reasoning", "variation", "kata_winrate"):
            self.assertIn(key, body["answer"])

    def test_system_info_reports_month_usage(self):
        cost.record_call("deepseek-chat", 1000, 500, 0.006,
                         kind="move", db_path=self.db_path)
        resp = self.client.get("/api/v1/system/info")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertAlmostEqual(body["token_usage_month"], 0.006)
        self.assertEqual(body["token_limit_month"], 30.0)


if __name__ == "__main__":
    unittest.main()
