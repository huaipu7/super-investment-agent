"""安全围栏 - 合规检查与敏感词过滤（迁移自 Agent-for-Investment/utils/safety.py）"""

import re

from app.constants import RISK_DISCLAIMER, DATA_SOURCE_DISCLAIMER

# 违禁词列表（承诺性、诱导性词汇）
FORBIDDEN_WORDS = [
    "保本", "必涨", " guaranteed", "guaranteed",
    "强烈推荐", "推荐买入", "推荐买",
    "稳赚", "稳赢", "包赚", "包赢",
    "翻倍", "翻番", "暴涨",
    "抄底", "赶紧买", "别错过",
    "100%", "百分之百",
    "无风险", "零风险", "no risk", "risk-free",
]

# 合规声明后缀
COMPLIANCE_SUFFIX = f"""

---
{'-' * 50}

📋 **免责声明**

{RISK_DISCLAIMER}

{DATA_SOURCE_DISCLAIMER}

*本内容由AI生成，仅供参考学习，不构成任何投资建议。投资者应独立判断并承担投资风险。*
"""


def contains_forbidden(text: str) -> list[str]:
    """检查文本是否包含违禁词，返回命中列表"""
    text_lower = text.lower()
    hits = []
    for word in FORBIDDEN_WORDS:
        if word.lower() in text_lower:
            hits.append(word)
    return hits


def sanitize(text: str) -> str:
    """过滤文本中的违禁词（替换为 ***）"""
    result = text
    for word in FORBIDDEN_WORDS:
        result = re.sub(re.escape(word), "***", result, flags=re.IGNORECASE)
    return result


def attach_disclaimer(text: str) -> str:
    """附加合规声明"""
    # 避免重复添加
    if "投资有风险" in text:
        return text
    return text + COMPLIANCE_SUFFIX


def check_output(text: str) -> dict:
    """对输出进行完整安全检查

    Returns:
        dict: {
            "safe": bool,
            "sanitized_text": str,
            "forbidden_hits": list[str],
            "warnings": list[str]
        }
    """
    warnings = []

    # 1. 检查违禁词
    hits = contains_forbidden(text)
    if hits:
        warnings.append(f"输出包含违禁词: {', '.join(hits)}，已过滤")
        text = sanitize(text)

    # 2. 确保有合规声明
    text = attach_disclaimer(text)

    return {
        "safe": len(hits) == 0,
        "sanitized_text": text,
        "forbidden_hits": hits,
        "warnings": warnings,
    }
