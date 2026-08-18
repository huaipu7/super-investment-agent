"""Agent LLM 适配层 —— 桥接原 Violetz utils/deepseek_client.py 接口到统一 LLM 客户端。

提供与 Violetz 签名一致的 chat / chat_stream / chat_tool_calls，
内部统一走 app/llm/client.py（DeepSeek V4 Flash，Anthropic 兼容 Messages API）。
消息中的 role="system" 会被提取为顶层 system 参数（Anthropic API 要求）。
"""
from __future__ import annotations

from app.llm.client import get_llm_client, to_anthropic_tool


def _split_system(messages: list[dict]) -> tuple[list[dict], str | None]:
    """从消息列表提取 system 角色内容，其余保留。"""
    system: list[str] = []
    msgs: list[dict] = []
    for m in messages:
        if m.get("role") == "system":
            c = m.get("content", "")
            if c:
                system.append(c)
        else:
            msgs.append(m)
    return msgs, "\n".join(system) if system else None


def chat(messages: list[dict], **kwargs) -> str:
    """非流式对话，返回最终文本（兼容 Violetz chat()）。

    kwargs 支持 max_tokens / task（task 用于模型网关按任务路由模型）。
    """
    msgs, system = _split_system(messages)
    client = get_llm_client()
    return client.chat(msgs, system=system, max_tokens=kwargs.get("max_tokens"),
                       task=kwargs.get("task"))


def chat_stream(messages: list[dict], **kwargs):
    """流式对话，逐 token 产出文本片段（兼容 Violetz chat_stream()）。"""
    msgs, system = _split_system(messages)
    client = get_llm_client()
    yield from client.stream(msgs, system=system, max_tokens=kwargs.get("max_tokens"),
                             task=kwargs.get("task"))


def chat_tool_calls(messages: list[dict], tools: list[dict], **kwargs) -> list[dict]:
    """单轮 Function Calling：返回 tool_calls 列表 [{name, arguments}]。

    tools 以 OpenAI 格式传入（Violetz 原定义），内部转换为 Anthropic 格式。
    供意图识别"一次性结构化抽取"使用，不执行工具循环。
    kwargs 支持 max_tokens / task。
    """
    msgs, system = _split_system(messages)
    client = get_llm_client()
    anthro_tools = [to_anthropic_tool(t) for t in tools]
    return client.chat_tool_calls(
        msgs,
        system=system,
        tools=anthro_tools,
        max_tokens=kwargs.get("max_tokens"),
        task=kwargs.get("task"),
    )
