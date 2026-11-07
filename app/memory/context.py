"""记忆上下文装配 —— 把长时记忆（用户画像 + 会话滚动摘要）渲染成可注入 prompt 的文本。

只读不写。无数据时返回空串：不编造「无偏好」之类的占位内容（约束#7 数据真实性）。
DB 读取失败不阻断对话主流程，但记 warning 日志，不静默吞掉。
"""
from __future__ import annotations

from app.memory import db as memory_db
from app.utils.logger import logger

_PROFILE_LABELS = {
    "risk_preference": "风险偏好",
    "investment_goal": "投资目标",
    "holdings": "持仓",
    "notes": "备注",
}


def _safe(fn, default=None):
    """记忆读取失败不阻断对话；记日志，不静默。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        logger.warning("记忆上下文读取失败(%s): %s", getattr(fn, "__name__", fn), e)
        return default


def build_memory_context() -> str:
    """装配「长期背景」文本；画像与摘要均缺失时返回 ""。"""
    blocks: list[str] = []

    summary = _safe(memory_db.latest_session_summary)
    if summary:
        blocks.append(f"【近期对话摘要】{summary}")

    profiles = _safe(memory_db.get_all_profiles) or {}
    lines = [f"- {_PROFILE_LABELS.get(k, k)}：{v}" for k, v in profiles.items() if v]
    if lines:
        blocks.append("【用户画像】\n" + "\n".join(lines))

    if not blocks:
        return ""
    return ("以下是该用户的长期背景，供参考（不要原样复述给用户）：\n"
            + "\n\n".join(blocks))
