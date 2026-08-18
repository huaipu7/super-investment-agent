"""
统一 LLM 客户端 - DeepSeek V4 Flash（Anthropic 兼容 Messages API）

来源：参考 Risk-Chain Agent 中已验证的调用方式（src/ai_assistant.py），
改用 httpx 实现，支持：
  - chat()                  同步单轮/多轮对话
  - achat()                 异步对话
  - chat_with_tools()       同步工具调用循环（最多 max_turns 轮）
  - achat_with_tools()      异步工具调用循环
  - stream() / astream()    流式输出（SSE，逐 token）

所有 LLM 调用（研究报告、SWOT、意图识别、评分、对话、报告表达）统一走本客户端。
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Iterator

import httpx

from app.config import get_settings
from app.llm import router as model_router
from app.observability.metrics import metrics


@dataclass
class ChatResult:
    """一次对话（含工具循环）的完整结果。"""
    text: str                          # 最终文本回复
    messages: list[dict] = field(default_factory=list)   # 更新后的消息列表
    tool_calls: list[dict] = field(default_factory=list) # 工具调用日志
    raw: dict = field(default_factory=dict)              # 最后一次原始响应


class LLMError(RuntimeError):
    """LLM 调用失败（网络/鉴权/超时等）。"""


class LLMClient:
    """统一 DeepSeek V4 Flash 客户端。"""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        thinking_enabled: bool | None = None,
        retry: int | None = None,
        retry_backoff: float | None = None,
    ):
        s = get_settings()
        self.api_key = api_key or s.LLM_API_KEY
        self.api_base = (api_base or s.LLM_API_BASE).rstrip("/")
        self.model = model or s.LLM_MODEL
        self.max_tokens = max_tokens or s.LLM_MAX_TOKENS
        self.timeout = timeout or s.LLM_TIMEOUT
        # 思维链默认关闭：更稳更快，避免 thinking 占满 token 预算导致无文本输出
        self.thinking_enabled = (
            s.LLM_THINKING_ENABLED if thinking_enabled is None else thinking_enabled
        )
        # 重试：每模型尝试次数 + 指数退避基数（秒）
        self.retry = retry if retry is not None else s.LLM_RETRY
        self.retry_backoff = retry_backoff if retry_backoff is not None else s.LLM_RETRY_BACKOFF

    # ── 内部工具 ──────────────────────────────────────────────

    @property
    def _messages_url(self) -> str:
        return f"{self.api_base}/v1/messages"

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise LLMError("未配置 LLM_API_KEY，请检查 .env")
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _build_body(
        self,
        messages: list[dict],
        system: str | None,
        max_tokens: int | None,
        tools: list[dict] | None,
        stream: bool = False,
        model: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
        if not self.thinking_enabled:
            body["thinking"] = {"type": "disabled"}
        return body

    @staticmethod
    def _extract_text(response: dict) -> str:
        return "".join(
            block.get("text", "")
            for block in response.get("content", [])
            if block.get("type") == "text"
        )

    @staticmethod
    def _split_tool_blocks(response: dict) -> tuple[list[dict], list[dict]]:
        """将 assistant 响应拆分为 text 块与 tool_use 块。"""
        text_blocks = [b for b in response.get("content", []) if b.get("type") == "text"]
        tool_blocks = [b for b in response.get("content", []) if b.get("type") == "tool_use"]
        return text_blocks, tool_blocks

    def _raise_for_response(self, resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200:
            raise LLMError(
                f"LLM API 请求失败 (HTTP {resp.status_code}): "
                f"{json.dumps(data, ensure_ascii=False)[:500]}"
            )
        return data

    # ── 模型路由 / 重试 / token 计数 ─────────────────────────

    def _model_for(self, task: str | None = None) -> str:
        """任务 → 主模型（未给 task 用默认模型）。"""
        if not task:
            return self.model
        return model_router.resolve_model(task) or self.model

    def _models_for(self, task: str | None = None) -> list[str]:
        """任务 → 模型尝试链（主 + 降级），默认模型兜底。"""
        if not task:
            return [self.model]
        chain = model_router.get_model_chain(task)
        if self.model not in chain:
            chain.append(self.model)
        return chain

    def _retry_call(self, fn: Callable[[str], Any], task: str | None = None) -> Any:
        """按模型链逐模型重试（每模型 LLM_RETRY 次 + 指数退避）。

        fn(model) -> 结果；仅捕获可重试的 LLMError / httpx.HTTPError。
        全部失败抛最后一个异常。成功计 llm_calls，总失败计 errors/llm_errors。
        """
        models = self._models_for(task)
        last_err: Exception | None = None
        for model in models:
            for attempt in range(max(1, self.retry)):
                try:
                    out = fn(model)
                    metrics.inc("llm_calls")
                    return out
                except (LLMError, httpx.HTTPError) as e:
                    last_err = e
                    if attempt < max(1, self.retry) - 1:
                        time.sleep(self.retry_backoff * (2 ** attempt))
                    elif model != models[-1]:
                        break  # 换下一个降级模型，不立即抛
        if last_err is not None:
            metrics.inc("errors")
            metrics.inc("llm_errors")
        raise last_err if last_err is not None else LLMError("LLM 调用失败")

    async def _aretry_call(
        self, afn: Callable[[str], Any], task: str | None = None
    ) -> Any:
        """异步版 _retry_call（asyncio.sleep 退避）。"""
        models = self._models_for(task)
        last_err: Exception | None = None
        for model in models:
            for attempt in range(max(1, self.retry)):
                try:
                    out = await afn(model)
                    metrics.inc("llm_calls")
                    return out
                except (LLMError, httpx.HTTPError) as e:
                    last_err = e
                    if attempt < max(1, self.retry) - 1:
                        await asyncio.sleep(self.retry_backoff * (2 ** attempt))
                    elif model != models[-1]:
                        break
        if last_err is not None:
            metrics.inc("errors")
            metrics.inc("llm_errors")
        raise last_err if last_err is not None else LLMError("LLM 调用失败")

    @staticmethod
    def _est_tokens(chars: int) -> int:
        # 中文场景粗略估计：约 3 字符 ≈ 1 token（API usage 缺省时兜底口径）
        return max(1, chars // 3)

    def _record_usage(
        self, data: dict | None, messages: list[dict], output_chars: int = 0
    ) -> None:
        """计数 token：优先 API usage 字段，缺省按字符估计（诚实标注口径）。"""
        usage = (data or {}).get("usage") or {}
        tin = usage.get("input_tokens")
        tout = usage.get("output_tokens")
        if tin is None:
            chars = 0
            for m in messages:
                c = m.get("content")
                if isinstance(c, str):
                    chars += len(c)
                elif isinstance(c, list):
                    chars += len(json.dumps(c, ensure_ascii=False))
            tin = self._est_tokens(chars)
        if tout is None:
            tout = self._est_tokens(output_chars)
        metrics.inc("llm_tokens_in", int(tin or 0))
        metrics.inc("llm_tokens_out", int(tout or 0))

    # ── 同步接口 ──────────────────────────────────────────────

    def chat(self, messages: list[dict], system: str | None = None,
             max_tokens: int | None = None, task: str | None = None) -> str:
        """同步对话，返回最终文本。task 用于模型路由。"""

        def _do(model: str) -> str:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    self._messages_url,
                    headers=self._headers(),
                    json=self._build_body(messages, system, max_tokens, None, model=model),
                )
                data = self._raise_for_response(resp)
            text = self._extract_text(data)
            self._record_usage(data, messages, len(text))
            return text

        return self._retry_call(_do, task)

    def chat_tool_calls(
        self,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        max_tokens: int | None = None,
        task: str | None = None,
    ) -> list[dict]:
        """单轮工具调用：返回一次响应的 tool_use 列表（[{name, arguments}]），不执行工具循环。

        供意图识别等"只需一次结构化抽取"的场景使用；tools 以 Anthropic 格式传入。
        tool_choice 需传字典形式（如 {"type": "auto"}）；缺省时让模型自动决定（不传该字段，
        兼容 DeepSeek Anthropic 端点不接受字符串枚举的限制）。
        """
        def _do(model: str) -> list[dict]:
            body = self._build_body(messages, system, max_tokens, tools, model=model)
            if tool_choice:
                body["tool_choice"] = tool_choice
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    self._messages_url,
                    headers=self._headers(),
                    json=body,
                )
                data = self._raise_for_response(resp)
            self._record_usage(data, messages)
            calls: list[dict] = []
            for block in data.get("content", []):
                if block.get("type") == "tool_use":
                    calls.append({
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    })
            return calls

        return self._retry_call(_do, task)

    def chat_with_tools(
        self,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_executor: Callable[[str, dict], str] | None = None,
        max_turns: int = 10,
        max_tokens: int | None = None,
        task: str | None = None,
    ) -> ChatResult:
        """同步工具调用循环。tool_executor(name, args) -> str(工具返回)。"""
        msgs = list(messages)
        log: list[dict] = []

        def _post_once(model: str) -> dict:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    self._messages_url,
                    headers=self._headers(),
                    json=self._build_body(msgs, system, max_tokens, tools, model=model),
                )
                data = self._raise_for_response(resp)
            self._record_usage(data, msgs)
            return data

        for _ in range(max_turns):
            data = self._retry_call(_post_once, task)
            stop_reason = data.get("stop_reason", "end_turn")

            if stop_reason == "tool_use":
                msgs, turn_log = self._apply_tool_turn(msgs, data, tool_executor)
                log.extend(turn_log)
                continue

            return ChatResult(
                text=self._extract_text(data),
                messages=msgs,
                tool_calls=log,
                raw=data,
            )
        return ChatResult(text="抱歉，工具调用轮数超限，请简化问题重试。",
                          messages=msgs, tool_calls=log)

    def stream(self, messages: list[dict], system: str | None = None,
               max_tokens: int | None = None, task: str | None = None) -> Iterator[str]:
        """同步流式对话，逐 token 产出文本片段。task 用于模型路由。

        重试只覆盖"首次连上/状态检查"前（未产出任何 token）；流中段断流直接抛，
        避免重发导致消费者收到重复片段。
        """
        models = self._models_for(task)
        last_err: Exception | None = None
        for model in models:
            for attempt in range(max(1, self.retry)):
                started = False
                try:
                    body = self._build_body(messages, system, max_tokens, None,
                                            stream=True, model=model)
                    with httpx.Client(timeout=self.timeout) as client:
                        with client.stream(
                            "POST", self._messages_url,
                            headers=self._headers(), json=body,
                        ) as resp:
                            if resp.status_code != 200:
                                b = resp.read().decode("utf-8", errors="ignore")[:500]
                                raise LLMError(
                                    f"LLM 流式请求失败 (HTTP {resp.status_code}): {b}")
                            out_chars = 0
                            for line in resp.iter_lines():
                                delta = self._parse_sse_delta(line)
                                if delta:
                                    started = True
                                    out_chars += len(delta)
                                    yield delta
                    self._record_usage(None, messages, out_chars)
                    metrics.inc("llm_calls")
                    return
                except (LLMError, httpx.HTTPError) as e:
                    if started:
                        raise
                    last_err = e
                    if attempt < max(1, self.retry) - 1:
                        time.sleep(self.retry_backoff * (2 ** attempt))
                    elif model != models[-1]:
                        break
        if last_err is not None:
            metrics.inc("errors")
            metrics.inc("llm_errors")
        raise last_err if last_err is not None else LLMError("LLM 流式调用失败")

    # ── 异步接口 ──────────────────────────────────────────────

    async def achat(self, messages: list[dict], system: str | None = None,
                    max_tokens: int | None = None, task: str | None = None) -> str:
        """异步对话，返回最终文本。task 用于模型路由。"""

        async def _ado(model: str) -> str:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self._messages_url,
                    headers=self._headers(),
                    json=self._build_body(messages, system, max_tokens, None, model=model),
                )
                data = self._raise_for_response(resp)
            text = self._extract_text(data)
            self._record_usage(data, messages, len(text))
            return text

        return await self._aretry_call(_ado, task)

    async def achat_with_tools(
        self,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_executor: Callable[[str, dict], str] | None = None,
        max_turns: int = 10,
        max_tokens: int | None = None,
        task: str | None = None,
    ) -> ChatResult:
        """异步工具调用循环。"""
        msgs = list(messages)
        log: list[dict] = []

        async def _post_once(model: str) -> dict:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self._messages_url,
                    headers=self._headers(),
                    json=self._build_body(msgs, system, max_tokens, tools, model=model),
                )
                data = self._raise_for_response(resp)
            self._record_usage(data, msgs)
            return data

        for _ in range(max_turns):
            data = await self._aretry_call(_post_once, task)
            stop_reason = data.get("stop_reason", "end_turn")

            if stop_reason == "tool_use":
                msgs, turn_log = self._apply_tool_turn(msgs, data, tool_executor)
                log.extend(turn_log)
                continue

            return ChatResult(
                text=self._extract_text(data),
                messages=msgs,
                tool_calls=log,
                raw=data,
            )
        return ChatResult(text="抱歉，工具调用轮数超限，请简化问题重试。",
                          messages=msgs, tool_calls=log)

    async def astream(self, messages: list[dict], system: str | None = None,
                      max_tokens: int | None = None, task: str | None = None) -> AsyncIterator[str]:
        """异步流式对话，逐 token 产出文本片段。task 用于模型路由。"""
        models = self._models_for(task)
        last_err: Exception | None = None
        for model in models:
            for attempt in range(max(1, self.retry)):
                started = False
                try:
                    body = self._build_body(messages, system, max_tokens, None,
                                            stream=True, model=model)
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        async with client.stream(
                            "POST", self._messages_url,
                            headers=self._headers(), json=body,
                        ) as resp:
                            if resp.status_code != 200:
                                b = (await resp.aread()).decode("utf-8", errors="ignore")[:500]
                                raise LLMError(
                                    f"LLM 流式请求失败 (HTTP {resp.status_code}): {b}")
                            out_chars = 0
                            async for line in resp.aiter_lines():
                                delta = self._parse_sse_delta(line)
                                if delta:
                                    started = True
                                    out_chars += len(delta)
                                    yield delta
                    self._record_usage(None, messages, out_chars)
                    metrics.inc("llm_calls")
                    return
                except (LLMError, httpx.HTTPError) as e:
                    if started:
                        raise
                    last_err = e
                    if attempt < max(1, self.retry) - 1:
                        await asyncio.sleep(self.retry_backoff * (2 ** attempt))
                    elif model != models[-1]:
                        break
        if last_err is not None:
            metrics.inc("errors")
            metrics.inc("llm_errors")
        raise last_err if last_err is not None else LLMError("LLM 流式调用失败")

    # ── 共享逻辑 ──────────────────────────────────────────────

    def _apply_tool_turn(
        self,
        messages: list[dict],
        data: dict,
        tool_executor: Callable[[str, dict], str] | None,
    ) -> tuple[list[dict], list[dict]]:
        """执行一轮工具调用：追加 assistant 消息 + tool_result 用户消息。"""
        text_blocks, tool_blocks = self._split_tool_blocks(data)
        assistant_blocks: list[dict] = list(text_blocks)
        tool_result_blocks: list[dict] = []
        turn_log: list[dict] = []

        for block in tool_blocks:
            tool_id = block["id"]
            tool_name = block["name"]
            tool_input = block.get("input") or {}
            assistant_blocks.append({
                "type": "tool_use",
                "id": tool_id,
                "name": tool_name,
                "input": tool_input,
            })
            if tool_executor is None:
                result_str = json.dumps({"error": "未注册工具执行器"}, ensure_ascii=False)
            else:
                try:
                    result_str = tool_executor(tool_name, tool_input)
                except Exception as e:
                    result_str = json.dumps({"error": str(e)}, ensure_ascii=False)
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": result_str,
            })
            try:
                parsed = json.loads(result_str)
            except Exception:
                parsed = result_str
            turn_log.append({"name": tool_name, "input": tool_input, "result": parsed})

        messages.append({"role": "assistant", "content": assistant_blocks})
        messages.append({"role": "user", "content": tool_result_blocks})
        return messages, turn_log

    @staticmethod
    def _parse_sse_delta(line: str) -> str | None:
        """解析 Anthropic SSE 一行，返回 text_delta 片段；非文本事件返回 None。"""
        if not line.startswith("data:"):
            return None
        payload = line[len("data:"):].strip()
        if not payload:
            return None
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if event.get("type") != "content_block_delta":
            return None
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            return delta.get("text", "")
        return None


@dataclass
class LLMChat:
    """便捷单例：默认使用全局配置的客户端。"""
    client: LLMClient = field(default_factory=LLMClient)


def get_llm_client() -> LLMClient:
    return LLMClient()


def to_anthropic_tool(openai_tool: dict) -> dict:
    """将 OpenAI 风格工具定义转换为 Anthropic Messages API 格式。

    OpenAI:   {"type": "function", "function": {"name": ..., "parameters": {...}}}
    Anthropic: {"name": ..., "description": ..., "input_schema": {...}}
    """
    fn = openai_tool.get("function", openai_tool)
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }
