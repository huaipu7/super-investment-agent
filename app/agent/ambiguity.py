"""歧义检测车间 — 多市场挂牌股票识别与用户澄清（逐字迁移自 workshops/ambiguity.py）"""

from __future__ import annotations

from dataclasses import dataclass, field

# ===== 跨市场挂牌公司定义 =====
# 当用户输入这些公司名时，系统需询问具体市场
DUAL_LISTED = {
    "比亚迪": [
        {"market": "A股", "ticker": "002594.SZ", "desc": "比亚迪(深交所)"},
        {"market": "港股", "ticker": "1211.HK", "desc": "BYD(港交所)"},
    ],
    "中国平安": [
        {"market": "A股", "ticker": "601318.SS", "desc": "中国平安(上交所)"},
        {"market": "港股", "ticker": "2318.HK", "desc": "中国平安(港交所)"},
    ],
    "工商银行": [
        {"market": "A股", "ticker": "601398.SS", "desc": "工商银行(上交所)"},
        {"market": "港股", "ticker": "1398.HK", "desc": "工商银行(港交所)"},
    ],
    "招商银行": [
        {"market": "A股", "ticker": "600036.SS", "desc": "招商银行(上交所)"},
        {"market": "港股", "ticker": "3968.HK", "desc": "招商银行(港交所)"},
    ],
    "建设银行": [
        {"market": "A股", "ticker": "601939.SS", "desc": "建设银行(上交所)"},
        {"market": "港股", "ticker": "0939.HK", "desc": "建设银行(港交所)"},
    ],
    "中国银行": [
        {"market": "A股", "ticker": "601988.SS", "desc": "中国银行(上交所)"},
        {"market": "港股", "ticker": "3988.HK", "desc": "中国银行(港交所)"},
    ],
    "中国石油": [
        {"market": "A股", "ticker": "601857.SS", "desc": "中国石油(上交所)"},
        {"market": "港股", "ticker": "0857.HK", "desc": "中国石油(港交所)"},
    ],
    "万科": [
        {"market": "A股", "ticker": "000002.SZ", "desc": "万科A(深交所)"},
        {"market": "港股", "ticker": "2202.HK", "desc": "万科企业(港交所)"},
    ],
    "长城汽车": [
        {"market": "A股", "ticker": "601633.SS", "desc": "长城汽车(上交所)"},
        {"market": "港股", "ticker": "2333.HK", "desc": "长城汽车(港交所)"},
    ],
    "中信证券": [
        {"market": "A股", "ticker": "600030.SS", "desc": "中信证券(上交所)"},
        {"market": "港股", "ticker": "6030.HK", "desc": "中信证券(港交所)"},
    ],
    "阿里巴巴": [
        {"market": "美股", "ticker": "BABA", "desc": "Alibaba(NYSE)"},
        {"market": "港股", "ticker": "9988.HK", "desc": "阿里巴巴(港交所)"},
    ],
    "百度": [
        {"market": "美股", "ticker": "BIDU", "desc": "Baidu(NASDAQ)"},
        {"market": "港股", "ticker": "9888.HK", "desc": "百度(港交所)"},
    ],
    "京东": [
        {"market": "美股", "ticker": "JD", "desc": "JD.com(NASDAQ)"},
        {"market": "港股", "ticker": "9618.HK", "desc": "京东(港交所)"},
    ],
    "网易": [
        {"market": "美股", "ticker": "NTES", "desc": "NetEase(NASDAQ)"},
        {"market": "港股", "ticker": "9999.HK", "desc": "网易(港交所)"},
    ],
}

# 各市场对应的交易所标签
MARKET_LABELS = {
    "A股": "🇨🇳 A股",
    "港股": "🇭🇰 港股",
    "美股": "🇺🇸 美股",
}


@dataclass
class PendingClarification:
    """挂起的歧义澄清请求"""
    company_name: str
    options: list[dict]  # [{"market": ..., "ticker": ..., "desc": ...}, ...]
    original_intent: dict
    user_input: str


# 全局 pending 状态（单用户应用）
_pending_clarification: PendingClarification | None = None


def check_ambiguity_from_input(raw_input: str) -> list[dict]:
    """直接从用户原始输入检测是否有跨市场歧义

    Args:
        raw_input: 用户原始输入文本

    Returns:
        list[dict]: 有歧义的公司列表
    """
    results = []
    text_lower = raw_input.lower()
    for name, options in DUAL_LISTED.items():
        if name.lower() in text_lower:
            results.append({"name": name, "options": options})
    return results


def check_ambiguity(company_names: list[str]) -> list[dict]:
    """检查公司名列表是否有跨市场歧义

    Args:
        company_names: intent.parse() 返回的 company_names 列表

    Returns:
        list[dict]: 有歧义的公司列表，每项包含 name 和 options
    """
    result = []
    for name in company_names:
        if name in DUAL_LISTED:
            result.append({"name": name, "options": DUAL_LISTED[name]})
    return result


def build_question(ambiguous: list[dict]) -> str:
    """构建询问用户选择市场的回复文本"""
    lines = []
    for item in ambiguous:
        name = item["name"]
        opts = item["options"]
        lines.append(f"📌 **{name}** 在多个市场上市，请您选择要查询的市场：\n")
        for opt in opts:
            market_icon = MARKET_LABELS.get(opt["market"], opt["market"])
            lines.append(f"  • `{opt['market']}` — {opt.get('desc', opt['ticker'])}")
        lines.append("")
        lines.append('请回复市场名称（如"港股"）或对应的股票代码。')
    return "\n".join(lines)


def set_pending(pc: PendingClarification):
    """设置挂起的澄清请求"""
    global _pending_clarification
    _pending_clarification = pc


def get_pending() -> PendingClarification | None:
    """获取挂起的澄清请求"""
    return _pending_clarification


def clear_pending():
    """清除挂起的澄清请求"""
    global _pending_clarification
    _pending_clarification = None


def resolve_ambiguity(user_input: str, pending: PendingClarification) -> dict | None:
    """解析用户对市场选择问题的回复

    Args:
        user_input: 用户回复文本
        pending: 挂起的澄清请求

    Returns:
        dict | None: 匹配的选项 {market, ticker, desc}，未匹配返回 None
    """
    text = user_input.lower().strip()
    for opt in pending.options:
        # 匹配市场名（如 "港股"、"A股"）
        if opt["market"].lower() in text:
            return opt
        # 匹配股票代码（如 "1211.HK"）
        if opt["ticker"].lower() in text:
            return opt
        # 匹配描述关键词
        desc = opt.get("desc", "").lower()
        if desc and desc in text:
            return opt
    return None


def get_market_label(ticker: str) -> str:
    """根据 ticker 后缀返回市场标签

    Args:
        ticker: 股票代码

    Returns:
        str: 市场标签（如 "🇭🇰 港股"）
    """
    t = ticker.upper()
    if t.endswith(".HK"):
        return MARKET_LABELS["港股"]
    elif t.endswith(".SS"):
        return MARKET_LABELS["A股"]
    elif t.endswith(".SZ"):
        return MARKET_LABELS["A股"]
    elif t in ("SPY", "QQQ", "DIA", "HSI"):
        return "ETF"
    else:
        return MARKET_LABELS["美股"]
