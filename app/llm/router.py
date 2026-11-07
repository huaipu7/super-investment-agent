"""模型网关 —— 任务→模型路由与降级链。

按任务类型选择主模型（MODEL_TIERS），未配置的任务回退 LLM_MODEL；
失败时可沿降级链切换模型（MODEL_FALLBACKS）。本模块只做路由决策，
真正的重试/退避在 app/llm/client.py 的 _retry_call / _aretry_call。
"""
from __future__ import annotations

from app.config import get_settings

# 常见任务名（供配置与文档引用）
KNOWN_TASKS = ("intent", "plan", "report", "summary", "sentiment", "tool",
               "investigation")


def resolve_model(task: str | None = None) -> str | None:
    """任务 → 主模型名。task 为空 → None（调用方用默认模型）。"""
    if not task:
        return None
    s = get_settings()
    tiers = s.MODEL_TIERS or {}
    return tiers.get(task) or s.LLM_MODEL


def fallback_models(task: str | None = None) -> list[str]:
    """任务 → 降级模型链（不含主模型）。"""
    if not task:
        return []
    s = get_settings()
    fb = s.MODEL_FALLBACKS or {}
    return [m for m in (fb.get(task, []) or []) if m]


def get_model_chain(task: str | None = None) -> list[str]:
    """任务 → 完整模型尝试链（主模型 + 降级，去重）。task 为空返回 []。"""
    if not task:
        return []
    chain = [resolve_model(task)]
    chain.extend(m for m in fallback_models(task) if m not in chain)
    return [m for m in chain if m]
