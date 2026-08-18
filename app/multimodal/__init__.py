"""多模态接入包：图 / 音 / 视频 → 文本预处理层。

DeepSeek 官方 API 不支持视觉输入，所有多模态内容在本地转成结构化文本，
再喂给现有 LLM 客户端（不改 app/llm/client.py）。
"""
from .ingest import ingest_bytes, kind_for, summarize

__all__ = ["ingest_bytes", "kind_for", "summarize"]
