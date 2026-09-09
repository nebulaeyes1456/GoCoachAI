"""LLM 客户端（窗口2）：DeepSeek（OpenAI 兼容）与 Ollama 本地兜底。

- ``LLMClient.chat_json(messages, schema_hint)``：消息列表进、JSON 出；
  解析时清洗 markdown 代码块包裹，失败自动重试 1 次；
  温度 0.3，单次请求超时 60 秒（任务书 §3.5）。
- provider 切换：``config.yaml`` 的 ``coach.provider``（deepseek | ollama）。
- 降级：DeepSeek 调用失败（网络/4xx/5xx/解析失败）自动降级到 Ollama，
  响应 model 标注为 ``ollama:<模型名>``。
- 每次成功调用通过 ``common.cost.record_call`` 记账（模型、token、成本；
  单价取 ``coach.pricing``，单位元/百万 token，可配置）。
- 接口保持通用（消息列表进、JSON 出），供窗口 3 出题讲解复用。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ...common import settings as settings_mod
from ...common.cost import record_call

# 任务书 §3.5：单次讲解超时 60 秒；温度 0.3
REQUEST_TIMEOUT = 60.0
TEMPERATURE = 0.3
RETRY_COUNT = 1  # 失败自动重试 1 次

# 未在 config 配置单价时的内置默认（deepseek-chat，元/百万 token）
DEFAULT_PRICING = {"input": 2.0, "output": 8.0}

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMError(RuntimeError):
    """LLM 调用失败（重试与降级后仍失败）。"""


@dataclass
class ChatResult:
    """一次成功调用的结果与元信息。"""

    data: dict = field(default_factory=dict)      # 解析后的 JSON
    content: str = ""                              # 模型原始输出
    model: str = ""                                # 标注后的模型名（含 ollama: 前缀）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0


def parse_json_response(text: str) -> dict:
    """清洗 markdown 代码块包裹并解析 JSON 对象。

    - 支持 ```json ... ``` / ``` ... ``` 包裹；
    - 支持前后夹杂说明文字（截取第一个 '{' 到最后一个 '}'）；
    - 解析失败抛 ValueError。
    """
    t = (text or "").strip()
    if not t:
        raise ValueError("空响应")
    m = _FENCE_RE.search(t)
    if m:
        t = m.group(1).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("响应中未找到 JSON 对象")
    try:
        obj = json.loads(t[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("JSON 根节点不是对象")
    return obj


def apply_schema(data: dict, schema_hint: dict | None) -> dict:
    """按 schema_hint 补齐缺失字段。

    schema_hint 形如 {"problem": str, "reason": str, "variation": list, ...}：
    - 字段缺失或类型不符时填入类型默认值（str→""、list→[]、数值→None）；
    - 不删除多余字段。
    """
    if not schema_hint:
        return data
    for key, typ in schema_hint.items():
        if key not in data or not _type_ok(data[key], typ):
            data[key] = _default_for(typ)
    return data


def _type_ok(value: Any, typ: Any) -> bool:
    if typ is str:
        return isinstance(value, str)
    if typ is list:
        return isinstance(value, list)
    if typ in (int, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True  # 未知类型不做校验


def _default_for(typ: Any) -> Any:
    if typ is list:
        return []
    if typ is int:
        return 0
    if typ is float:
        return None  # 缺失的数值表示"未知"（如 kata_winrate 无数据时为 null）
    return ""


class LLMClient:
    """DeepSeek / Ollama 统一客户端（线程安全：每次请求新建连接）。"""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or settings_mod.get_settings()
        self.coach_cfg = dict(self.config.get("coach") or {})

    # ------------------------------------------------------------------ URL
    def _deepseek_url(self) -> str:
        base = str(self.coach_cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
        return f"{base}/chat/completions"

    def _ollama_url(self) -> str:
        ollama = dict(self.coach_cfg.get("ollama") or {})
        base = str(ollama.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        return f"{base}/api/chat"

    # ------------------------------------------------------------ providers
    def _call_deepseek(self, messages: list[dict], json_mode: bool,
                        max_tokens: int | None = None) -> ChatResult:
        model = str(self.coach_cfg.get("model") or "deepseek-chat")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": TEMPERATURE,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.coach_cfg.get('api_key', '')}",
            "Content-Type": "application/json",
        }
        try:
            resp = httpx.post(self._deepseek_url(), json=payload,
                              headers=headers, timeout=REQUEST_TIMEOUT)
        except httpx.HTTPError as exc:
            raise LLMError(f"DeepSeek 网络错误: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"DeepSeek 返回 {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("DeepSeek 响应缺少 choices")
        content = str(choices[0].get("message", {}).get("content") or "")
        usage = data.get("usage") or {}
        result = ChatResult(
            content=content,
            model=str(data.get("model") or model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
        result.cost = self._calc_cost(result.model,
                                      result.prompt_tokens, result.completion_tokens)
        return result

    def _call_ollama(self, messages: list[dict], json_mode: bool) -> ChatResult:
        ollama = dict(self.coach_cfg.get("ollama") or {})
        model = str(ollama.get("model") or "qwen2.5:14b")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": TEMPERATURE},
        }
        if json_mode:
            payload["format"] = "json"
        try:
            resp = httpx.post(self._ollama_url(), json=payload, timeout=REQUEST_TIMEOUT)
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama 网络错误: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"Ollama 返回 {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        content = str(data.get("message", {}).get("content") or "")
        return ChatResult(
            content=content,
            model=f"ollama:{data.get('model') or model}",
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
            cost=0.0,  # 本地推理不计费
        )

    def _calc_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """按 config 中可配置单价换算（元/百万 token）。"""
        pricing = dict(self.coach_cfg.get("pricing") or {})
        p = pricing.get(model) or pricing.get("default") or DEFAULT_PRICING
        input_price = float(p.get("input", 0.0) or 0.0)
        output_price = float(p.get("output", 0.0) or 0.0)
        return (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000

    # ------------------------------------------------------------- 核心 API
    def _attempt(self, provider: str, messages: list[dict], json_mode: bool,
                 schema_hint: dict | None,
                 max_tokens: int | None = None) -> ChatResult:
        """单 provider 调用：失败或 JSON 解析失败自动重试 1 次。"""
        last_error: Exception | None = None
        for _ in range(RETRY_COUNT + 1):
            try:
                if provider == "deepseek":
                    result = self._call_deepseek(messages, json_mode, max_tokens=max_tokens)
                else:
                    result = self._call_ollama(messages, json_mode)
                result.data = apply_schema(parse_json_response(result.content),
                                           schema_hint)
                return result
            except (LLMError, ValueError) as exc:
                last_error = exc
        raise LLMError(f"{provider} 调用失败: {last_error}")

    def chat_json(
        self,
        messages: list[dict],
        schema_hint: dict | None = None,
        kind: str = "coach",
        db_path: str | Path | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """统一入口：消息列表进、JSON 出。

        - 按 coach.provider 调用；DeepSeek 失败自动降级 Ollama；
        - 成功后记账（record_call）；全部失败抛 LLMError。
        """
        provider = str(self.coach_cfg.get("provider") or "deepseek")
        result: ChatResult | None = None
        last_error: Exception | None = None
        try:
            result = self._attempt(provider, messages, json_mode=True, max_tokens=max_tokens,
                                   schema_hint=schema_hint)
        except LLMError as exc:
            last_error = exc
        if result is None and provider != "ollama":
            # 降级到 Ollama（任务书 §3.4）
            try:
                result = self._attempt("ollama", messages, json_mode=True,
                                       schema_hint=schema_hint)
            except LLMError as exc:
                last_error = exc
        if result is None:
            raise LLMError(f"所有 provider 均失败: {last_error}")
        record_call(
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost=result.cost,
            kind=kind,
            db_path=db_path,
        )
        # T0：LLM 调用摘要写服务日志（不落 key、不落正文）
        try:
            from ...common import serverlog

            serverlog.info(
                f"[llm] {kind} model={result.model} "
                f"prompt={result.prompt_tokens} completion="
                f"{result.completion_tokens} cost={result.cost:.6f}元"
            )
        except Exception:
            pass
        return result
