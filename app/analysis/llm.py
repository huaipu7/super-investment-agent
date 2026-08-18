"""分析模块 LLM 适配层 —— 将 Research-Agent 的 DeepSeekClient.chat() 接口
桥接到统一 LLM 客户端(app/llm/client.py)。

所有分析器（SWOT/竞品/地缘/分部识别）只依赖本模块的 analysis_llm 单例。
"""
from __future__ import annotations

from app.llm.client import LLMClient, get_llm_client


class AnalysisLLM:
    """兼容 DeepSeekClient.chat() 接口的适配器（temperature/retry 由统一客户端接管）。"""

    def __init__(self, client: LLMClient | None = None):
        self._client = client or get_llm_client()

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        retry_count: int = 3,
        system_prompt: str | None = None,
    ) -> str:
        """发送聊天请求，返回文本。system 可从 system_prompt 或消息列表中的 system 角色提取。"""
        system = system_prompt
        msgs: list[dict] = []
        for m in messages:
            if m.get("role") == "system":
                if system is None:
                    system = m.get("content", "")
            else:
                msgs.append(m)
        return self._client.chat(msgs, system=system, max_tokens=max_tokens, task="report")

    def verify_connection(self) -> bool:
        try:
            text = self.chat(
                messages=[{"role": "user", "content": "请回复 'API连接成功' 这五个字。"}],
                temperature=0,
                max_tokens=50,
            )
            return "API连接成功" in text
        except Exception:
            return False


analysis_llm = AnalysisLLM()
