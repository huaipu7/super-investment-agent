"""合规检测规则 —— 关键词匹配投资建议倾向（轻量，允许误报只加免责声明）。

`check_advice(text)` 纯函数，供生成器与报告路径复用；
`apply_gate(text, source)` 一站式：检测 → 命中则追加免责声明 + 落复核队列。
"""
from __future__ import annotations

import hashlib

_ADVICE_PATTERNS = (
    "建议买入", "建议卖出", "推荐买入", "强烈推荐", "逢低吸纳", "获利了结",
    "买入", "卖出", "加仓", "减仓", "建仓", "清仓", "满仓", "重仓",
    "目标价", "第一目标", "止损位", "止盈位", "抄底", "追高",
)

COMPLIANCE_DISCLAIMER = (
    "\n\n---\n"
    "> ⚠️ **合规提示**：以上内容可能包含投资倾向性表述，仅供参考，不构成任何投资建议。"
    "投资有风险，入市需谨慎。"
)


def check_advice(text: str) -> tuple[bool, list[str]]:
    """返回 (是否含投资建议模式, 命中词列表)。"""
    hits = [p for p in _ADVICE_PATTERNS if p in text]
    return (len(hits) > 0, hits)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def apply_gate(text: str, source: str = "") -> tuple[str, str, bool]:
    """检测 → 命中则追加免责声明 + 落复核队列。

    Returns:
        (处理后的文本, content_hash, 是否命中)
    """
    flagged, hits = check_advice(text)
    h = content_hash(text)
    if flagged:
        from app.compliance.queue import flag as queue_flag
        queue_flag(source=source, content_hash=h, snippet=text[:200],
                   hit_words=hits, advice_flag=1)
        return text + COMPLIANCE_DISCLAIMER, h, True
    return text, h, False
