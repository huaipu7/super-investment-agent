"""
Phase 0 冒烟测试：配置加载 + LLM 连通性（同步/流式/工具调用）。

运行: D:\\python\\python.exe scripts\\smoke_phase0.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.llm.client import LLMClient


def main():
    print("=" * 60)
    print("Phase 0 冒烟测试")
    print("=" * 60)

    # 1. 配置加载
    s = get_settings()
    assert s.LLM_API_KEY, "LLM_API_KEY 为空"
    assert s.LLM_MODEL == "deepseek-v4-flash"
    assert not s.LLM_API_KEY.startswith("your_")
    print(f"[OK] 配置加载: model={s.LLM_MODEL}, base={s.LLM_API_BASE}")
    print(f"[OK] 数据源开关: yfinance={s.SOURCE_YFINANCE}, akshare={s.SOURCE_AKSHARE}, "
          f"sina={s.SOURCE_SINA}, fund={s.SOURCE_FUND}")
    print(f"[OK] RAG_ENABLED={s.RAG_ENABLED}, 缓存={s.cache_db}")

    client = LLMClient()

    # 2. 基础对话
    print("\n--- 2. 基础对话 (chat) ---")
    text = client.chat([{"role": "user", "content": "用一句话介绍贵州茅台这家公司。"}],
                       system="你是专业的投资研究助手。请简洁回答。")
    print(f"[OK] 模型返回: {text[:120]}...")

    # 3. 流式输出
    print("\n--- 3. 流式输出 (stream) ---")
    chunks = []
    for chunk in client.stream([{"role": "user", "content": "请写一句 '你好'。"}],
                               system="只输出一句话。", max_tokens=50):
        chunks.append(chunk)
    streamed = "".join(chunks)
    assert streamed.strip(), "流式输出为空"
    print(f"[OK] 流式 token 数: {len(chunks)}, 结果: {streamed[:80]}...")

    # 4. 工具调用
    print("\n--- 4. 工具调用 (chat_with_tools) ---")

    def calc_tool(name, args):
        if name == "multiply":
            return json.dumps({"result": args["a"] * args["b"]}, ensure_ascii=False)
        return json.dumps({"error": f"未知工具 {name}"})

    tools = [{
        "name": "multiply",
        "description": "计算两个整数的乘积",
        "input_schema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "第一个数"},
                "b": {"type": "integer", "description": "第二个数"},
            },
            "required": ["a", "b"],
        },
    }]
    result = client.chat_with_tools(
        [{"role": "user", "content": "请用工具计算 6 乘以 7 的结果。"}],
        system="需要数据时使用工具。",
        tools=tools,
        tool_executor=calc_tool,
    )
    print(f"[OK] 工具调用次数: {len(result.tool_calls)}")
    for tc in result.tool_calls:
        print(f"     tool={tc['name']} input={tc['input']} result={tc['result']}")
    print(f"[OK] 最终回复: {result.text[:120]}...")
    assert "42" in result.text, f"工具结果未正确注入回复: {result.text}"

    # 5. 异步接口
    print("\n--- 5. 异步接口 (achat / astream) ---")
    import asyncio

    async def _async_test():
        text = await client.achat(
            [{"role": "user", "content": "用一句话介绍腾讯。"}],
            system="简洁回答。",
        )
        assert text.strip(), "achat 返回为空"
        print(f"[OK] achat: {text[:80]}...")
        parts = []
        async for chunk in client.astream(
            [{"role": "user", "content": "请输出两个字：在。"}],
            system="只输出两个字。", max_tokens=50,
        ):
            parts.append(chunk)
        joined = "".join(parts).strip()
        assert joined, "astream 返回为空"
        print(f"[OK] astream: {joined[:80]} (chunks={len(parts)})")

    asyncio.run(_async_test())

    print("\n" + "=" * 60)
    print("Phase 0 冒烟测试全部通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
