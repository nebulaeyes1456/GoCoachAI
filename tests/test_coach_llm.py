"""窗口2 单测：LLM 客户端（JSON 解析容错、重试、降级、成本记账）。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend.common import cost, db
from backend.services.coach.llm import (ChatResult, LLMClient, apply_schema,
                                        parse_json_response)

TEST_CONFIG = {
    "coach": {
        "provider": "deepseek",
        "api_key": "test-key",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "ollama": {"base_url": "http://127.0.0.1:11434", "model": "qwen2.5:14b"},
        "pricing": {"deepseek-chat": {"input": 2.0, "output": 8.0}},
    },
    "budget": {"token_limit_month": 30},
}


def _resp(url: str, status: int = 200, payload: dict | None = None,
          text: str | None = None) -> httpx.Response:
    req = httpx.Request("POST", url)
    if payload is not None:
        return httpx.Response(status, json=payload, request=req)
    return httpx.Response(status, text=text or "", request=req)


# ---------------------------------------------------------------------------
# JSON 解析容错（任务书 §3.5：模拟 markdown 包裹、少字段）
# ---------------------------------------------------------------------------

class ParseJsonTest(unittest.TestCase):
    def test_fenced_json_block(self):
        text = '好的，讲解如下：\n```json\n{"problem": "断点", "variation": ["P10"]}\n```\n希望对你有帮助'
        self.assertEqual(parse_json_response(text), {"problem": "断点", "variation": ["P10"]})

    def test_fence_without_lang(self):
        text = '```\n{"a": 1}\n```'
        self.assertEqual(parse_json_response(text), {"a": 1})

    def test_plain_json(self):
        self.assertEqual(parse_json_response('{"a": 1}'), {"a": 1})

    def test_extra_text_around_json(self):
        text = '先说两句…… {"a": 1, "b": [1, 2]} 再说两句……'
        self.assertEqual(parse_json_response(text), {"a": 1, "b": [1, 2]})

    def test_bad_json_raises(self):
        with self.assertRaises(ValueError):
            parse_json_response("这不是 JSON，完全没有任何对象")

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse_json_response("")

    def test_missing_fields_filled_by_schema(self):
        schema = {"problem": str, "reason": str, "variation": list, "kata_winrate": float}
        data = apply_schema({"problem": "有值"}, schema)
        self.assertEqual(data["problem"], "有值")
        self.assertEqual(data["reason"], "")
        self.assertEqual(data["variation"], [])
        self.assertIsNone(data["kata_winrate"])  # 缺失数值 → null（未知）

    def test_wrong_type_filled_by_schema(self):
        data = apply_schema({"variation": "不是数组"}, {"variation": list})
        self.assertEqual(data["variation"], [])


# ---------------------------------------------------------------------------
# chat_json：重试 / 降级 / 成本记账（mock httpx.post，不访问网络）
# ---------------------------------------------------------------------------

class ChatJsonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "goapp.db"
        db.init_db(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_retry_once_then_success(self):
        calls: list[str] = []

        def side_effect(url, **kwargs):
            calls.append(str(url))
            if len(calls) == 1:
                return _resp(url, payload={
                    "choices": [{"message": {"content": "这不是 JSON"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                })
            return _resp(url, payload={
                "choices": [{"message": {"content": '{"problem": "ok"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })

        client = LLMClient(TEST_CONFIG)
        with mock.patch("backend.services.coach.llm.httpx.post", side_effect=side_effect):
            result = client.chat_json([{"role": "user", "content": "hi"}],
                                      {"problem": str}, kind="move", db_path=self.db_path)
        self.assertEqual(result.data, {"problem": "ok"})
        self.assertEqual(len(calls), 2)  # 解析失败后自动重试 1 次成功

    def test_fallback_to_ollama_on_deepseek_failure(self):
        """任务书 §3.4：DeepSeek 失败自动降级 Ollama，model 标注 ollama: 前缀。"""
        def side_effect(url, **kwargs):
            if "chat/completions" in url:
                return _resp(url, status=500, text="server error")
            return _resp(url, payload={
                "message": {"content": '{"problem": "本地讲解"}'},
                "model": "qwen2.5:14b",
                "prompt_eval_count": 20,
                "eval_count": 10,
            })

        client = LLMClient(TEST_CONFIG)
        with mock.patch("backend.services.coach.llm.httpx.post", side_effect=side_effect):
            result = client.chat_json([{"role": "user", "content": "hi"}],
                                      {"problem": str}, kind="move", db_path=self.db_path)
        self.assertEqual(result.data, {"problem": "本地讲解"})
        self.assertTrue(result.model.startswith("ollama:"))
        self.assertEqual(result.cost, 0.0)  # 本地推理不计费

    def test_all_providers_fail_raises(self):
        def side_effect(url, **kwargs):
            return _resp(url, status=500, text="server error")

        client = LLMClient(TEST_CONFIG)
        with mock.patch("backend.services.coach.llm.httpx.post", side_effect=side_effect):
            with self.assertRaises(Exception) as ctx:
                client.chat_json([{"role": "user", "content": "hi"}],
                                 {"problem": str}, kind="move", db_path=self.db_path)
        self.assertIn("失败", str(ctx.exception))

    def test_usage_recorded_and_cost_calculated(self):
        def side_effect(url, **kwargs):
            return _resp(url, payload={
                "choices": [{"message": {"content": '{"a": "b"}'}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            })

        client = LLMClient(TEST_CONFIG)
        with mock.patch("backend.services.coach.llm.httpx.post", side_effect=side_effect):
            result = client.chat_json([{"role": "user", "content": "hi"}],
                                      kind="move", db_path=self.db_path)
        # (1000 * 2 + 500 * 8) / 1e6 = 0.006 元
        self.assertAlmostEqual(result.cost, 0.006)
        self.assertAlmostEqual(cost.month_cost(db_path=self.db_path), 0.006)
        self.assertEqual(cost.month_tokens(db_path=self.db_path), (1000, 500))

    def test_ollama_provider_direct(self):
        """provider=ollama 时直接走本地，不经过 DeepSeek。"""
        config = dict(TEST_CONFIG)
        config["coach"] = dict(TEST_CONFIG["coach"], provider="ollama")
        seen: list[str] = []

        def side_effect(url, **kwargs):
            seen.append(str(url))
            return _resp(url, payload={
                "message": {"content": '{"x": 1}'},
                "model": "qwen2.5:14b",
            })

        client = LLMClient(config)
        with mock.patch("backend.services.coach.llm.httpx.post", side_effect=side_effect):
            result = client.chat_json([{"role": "user", "content": "hi"}],
                                      kind="move", db_path=self.db_path)
        self.assertEqual(result.data, {"x": 1})
        self.assertTrue(all("api/chat" in u for u in seen))


if __name__ == "__main__":
    unittest.main()
