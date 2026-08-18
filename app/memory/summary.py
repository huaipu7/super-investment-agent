"""会话滚动摘要 —— 每 SUMMARY_EVERY 条消息触发一次。

LLM 可用时用 LLM 滚动摘要；LLM 不可用时用确定性截断兜底（显式标注
「[LLM 未配置]」，不伪造）。摘要写入 memory.sessions，供上下文注入。
"""
from __future__ import annotations

from app.config import get_settings
from app.memory import db
from app.utils.logger import logger

SUMMARY_EVERY = 10

_SUMMARY_PROMPT = (
    "请用不超过 3 句话概括以下投资对话的要点"
    "（用户关注的标的、查询意图、结论或待办），用中文：\n"
)


def maybe_roll_summary() -> str | None:
    """消息数每到 SUMMARY_EVERY 整数倍时生成/更新滚动摘要，返回本次摘要或 None。"""
    n = db.count_messages()
    if n < SUMMARY_EVERY or n % SUMMARY_EVERY != 0:
        return None
    recent = db.recent_messages(SUMMARY_EVERY)
    text = "\n".join(f"{m['role']}: {m['text']}" for m in recent)
    summary = _summarize(text)
    db.update_session(summary)
    logger.info("会话滚动摘要已更新（累计 %d 条消息）", n)
    return summary


def _summarize(text: str) -> str:
    """LLM 摘要；不可用/失败时确定性截断兜底（诚实标注，不伪造）。"""
    if not get_settings().check_llm_ready():
        return f"[LLM 未配置] 最近 {SUMMARY_EVERY} 条对话要点: {text[:200]}"
    try:
        from app.agent.llm import chat

        out = chat([
            {"role": "system", "content": _SUMMARY_PROMPT},
            {"role": "user", "content": text},
        ], task="summary")
        out = (out or "").strip()
        return out[:500] if out else "[摘要为空]"
    except Exception as e:  # noqa: BLE001 摘要失败不阻断对话
        logger.warning("会话摘要 LLM 失败，确定性截断兜底: %s", e)
        return f"[摘要降级] 最近 {SUMMARY_EVERY} 条对话要点: {text[:200]}"
