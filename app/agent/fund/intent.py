"""基金意图车间 - 基金查询的自然语言理解与实体抽取（迁移自 workshops/fund_intent.py）。

LLM 调用统一走 app.agent.llm。
"""
from __future__ import annotations

import json
import re

from app.agent.llm import chat, chat_tool_calls

# ===== 基金意图类型 =====

FUND_INTENTS = ["fund_nav", "fund_history", "fund_analysis", "fund_compare", "fund_holdings"]

FUND_INTENT_LABELS = {
    "fund_nav": "查询净值/实时行情",
    "fund_history": "查询历史净值走势",
    "fund_analysis": "基金深度分析",
    "fund_compare": "多基金对比",
    "fund_holdings": "查询基金持仓",
}

# 基金代码正则（6位数字）
FUND_CODE_RE = re.compile(r"\b(\d{6})\b")

FUND_PARSE_INTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "parse_fund_intent",
        "description": "解析用户的基金查询意图，提取结构化参数。识别用户想查净值、历史走势、深度分析、对比分析还是持仓。",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": FUND_INTENTS,
                    "description": "基金查询意图类型: fund_nav=查询净值/实时行情, fund_history=历史净值走势, fund_analysis=深度分析, fund_compare=对比分析, fund_holdings=查询持仓"
                },
                "fund_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "6位基金代码列表，如 005827, 110011"
                },
                "fund_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "用户提到的原始基金名称列表，保留原话"
                }
            },
            "required": ["intent"]
        }
    }
}

FUND_INTENT_SYSTEM_PROMPT = """你是 Violetz 投资顾问的基金意图识别引擎。请分析用户输入，提取结构化信息。

返回严格的 JSON 格式（不要 markdown 包裹）：
{
    "intent": "fund_nav | fund_history | fund_analysis | fund_compare | fund_holdings",
    "fund_codes": ["6位基金代码列表"],
    "fund_names": ["用户提到的基金名列表"]
}

意图说明：
- fund_nav: 查净值/实时行情/价格
- fund_history: 查历史净值走势/表现/趋势
- fund_analysis: 综合分析/深度分析/评价基金
- fund_compare: 对比分析/比较多只基金
- fund_holdings: 查持仓/股票/投资组合

规则：
1. 基金代码为6位数字，不要添加任何后缀
2. 如果用户输入基金名称（如"易方达蓝筹"），保留在 fund_names 中
3. 不确定的字段设为 null"""


def _parse_with_function_calling(user_input: str) -> dict | None:
    """使用 DeepSeek Function Calling 解析基金意图"""
    try:
        tool_calls = chat_tool_calls(
            messages=[
                {"role": "system", "content": "你是 Violetz 投资顾问的基金意图识别引擎。请根据用户输入调用 parse_fund_intent 工具返回结构化结果。"},
                {"role": "user", "content": user_input},
            ],
            tools=[FUND_PARSE_INTENT_TOOL],
            task="intent",
        )
        if not tool_calls:
            return None

        for tc in tool_calls:
            if tc["name"] == "parse_fund_intent":
                args = json.loads(tc["arguments"])
                return {
                    "intent": args.get("intent"),
                    "fund_codes": args.get("fund_codes", []),
                    "fund_names": args.get("fund_names", []),
                }
        return None
    except Exception:
        return None


def _parse_with_llm(user_input: str) -> dict | None:
    """使用 DeepSeek LLM 解析基金意图（兜底）"""
    try:
        result = chat([
            {"role": "system", "content": FUND_INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ], task="intent")
        result = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(result)
    except Exception:
        return None


def _extract_fund_codes(text: str) -> list[str]:
    """从文本中提取6位数字基金代码"""
    return FUND_CODE_RE.findall(text)


def _detect_fund_intent_fallback(text: str) -> str:
    """规则兜底判断基金意图"""
    if any(k in text for k in ["对比", "比较", "vs", "和.*比", "哪个"]):
        return "fund_compare"
    if any(k in text for k in ["持仓", "重仓", "股票", "投资组合", "买了什么"]):
        return "fund_holdings"
    if any(k in text for k in ["分析", "怎么样", "评价", "表现如何", "值得", "好不好"]):
        return "fund_analysis"
    if any(k in text for k in ["走势", "历史", "趋势", "涨跌", "表现", "曲线"]):
        return "fund_history"
    return "fund_nav"


def _search_fund_name_to_code(text: str, fund_names: list[str]) -> list[str]:
    """通过基金名称模糊搜索转换为基金代码

    如果用户提供了基金名称但没有代码，尝试在本地搜索匹配。
    这里只做简单的占位标记，实际搜索由 planner 调用 fund_search 完成。

    Returns:
        已识别的基金代码列表（可能为空，由上层补充搜索）
    """
    # 已有代码直接返回
    codes = _extract_fund_codes(text)
    if codes:
        return codes
    return []


def parse(user_input: str) -> dict:
    """解析用户输入，返回结构化基金意图

    Args:
        user_input: 用户自然语言输入

    Returns:
        dict: {
            "intent": str,
            "fund_codes": list[str],
            "fund_names": list[str],
            "raw_input": str
        }
    """
    # 从输入中提取基金代码
    raw_codes = _extract_fund_codes(user_input)

    # 第1优先：Function Calling 解析
    parsed = _parse_with_function_calling(user_input)

    if parsed and isinstance(parsed, dict) and parsed.get("intent"):
        codes = parsed.get("fund_codes", []) or raw_codes
        names = parsed.get("fund_names", [])
        return {
            "intent": parsed["intent"],
            "fund_codes": codes if codes else None,
            "fund_names": names,
            "raw_input": user_input,
        }

    # 第2优先：传统 LLM 解析
    parsed = _parse_with_llm(user_input)

    if parsed and isinstance(parsed, dict) and parsed.get("intent"):
        codes = parsed.get("fund_codes", []) or raw_codes
        names = parsed.get("fund_names", [])
        return {
            "intent": parsed["intent"],
            "fund_codes": codes if codes else None,
            "fund_names": names,
            "raw_input": user_input,
        }

    # 第3优先：规则兜底
    intent = _detect_fund_intent_fallback(user_input)

    return {
        "intent": intent,
        "fund_codes": raw_codes if raw_codes else None,
        "fund_names": [],
        "raw_input": user_input,
    }
