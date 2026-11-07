"""意图车间 - 自然语言理解与实体抽取（迁移自 workshops/intent.py）。

LLM 调用统一走 app.agent.llm（内部是统一 DeepSeek 客户端）。
意图解析三层降级：Function Calling → 传统 LLM JSON → 规则兜底。
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.parse

from app.agent.llm import chat, chat_tool_calls
from app.llm.schemas import IntentResult
from app.llm.structured import parse_llm_json
from app.memory.context import build_memory_context

# ===== 中英文公司名 → Ticker 映射表（兜底用） =====
TICKER_MAP = {
    # 美股
    "苹果": "AAPL", "apple": "AAPL",
    "微软": "MSFT", "microsoft": "MSFT",
    "谷歌": "GOOGL", "google": "GOOGL", "alphabet": "GOOGL",
    "亚马逊": "AMZN", "amazon": "AMZN",
    "英伟达": "NVDA", "nvidia": "NVDA",
    "特斯拉": "TSLA", "tesla": "TSLA",
    "meta": "META", "facebook": "META",
    "台积电": "TSM", "tsmc": "TSM",
    "阿里巴巴": "BABA", "alibaba": "BABA",
    "百度": "BIDU", "baidu": "BIDU",
    "拼多多": "PDD", "pdd": "PDD",
    "京东": "JD", "jd": "JD",
    "网易": "NTES", "ntes": "NTES",
    "蔚来": "NIO", "nio": "NIO",
    "理想汽车": "LI", "li": "LI",
    "小鹏汽车": "XPEV", "xpev": "XPEV",
    "伯克希尔": "BRK-B", "berkshire": "BRK-B", "brk": "BRK-B",
    "可口可乐": "KO", "coca-cola": "KO",
    "麦当劳": "MCD", "mcdonald": "MCD",
    "耐克": "NKE", "nike": "NKE",
    # 港股
    "腾讯": "0700.HK", "tencent": "0700.HK",
    "美团": "3690.HK", "meituan": "3690.HK",
    "小米": "1810.HK", "xiaomi": "1810.HK",
    "比亚迪": "1211.HK", "byd": "1211.HK",
    "港交所": "0388.HK", "香港交易所": "0388.HK",
    "中国平安(港股)": "2318.HK", "友邦保险": "1299.HK",
    "工商银行": "1398.HK",
    "汇丰": "0005.HK", "汇丰控股": "0005.HK",
    "京东健康": "6618.HK",
    # A股
    "贵州茅台": "600519.SS", "茅台": "600519.SS",
    "招商银行": "600036.SS",
    "中国平安": "601318.SS",
    "五粮液": "000858.SZ",
    "宁德时代": "300750.SZ",
    "中国银行": "601988.SS",
    "建设银行": "601939.SS",
    "农业银行": "601288.SS",
    "中国石油": "601857.SS",
    "中国石化": "600028.SS",
    "中国神华": "601088.SS",
    "中国中免": "601888.SS",
    "海康威视": "002415.SZ",
    "隆基绿能": "601012.SS",
    "药明康德": "603259.SS",
    "长城汽车": "601633.SS",
    "中信证券": "600030.SS",
    "万华化学": "600309.SS",
    "恒瑞医药": "600276.SS",
    "美的集团": "000333.SZ",
    "格力电器": "000651.SZ",
    "长江电力": "600900.SS",
    "中国建筑": "601668.SS",
    "伊利股份": "600887.SS",
    "紫金矿业": "601899.SS",
    "海螺水泥": "600585.SS",
    "科大讯飞": "002230.SZ",
    "东方财富": "300059.SZ",
    "阳光电源": "300274.SZ",
    "万科A": "000002.SZ", "万科": "000002.SZ",
    "中兴通讯": "000063.SZ",
    # ETF
    "标普500": "SPY", "spy": "SPY",
    "纳斯达克": "QQQ", "qqq": "QQQ", "纳指": "QQQ",
    "沪深300": "ASHR", "csi300": "ASHR",
    "恒生指数": "HSI", "恒指": "HSI",
}

# 特殊 ticker 映射（Function Calling 返回的指数代码 → 可交易标的）
SPECIAL_TICKER_MAP = {
    "^GSPC": "SPY",    # 标普500指数 → SPY ETF
    "^IXIC": "QQQ",    # 纳斯达克指数 → QQQ ETF
    "^DJI": "DIA",     # 道琼斯指数 → DIA ETF
    "^HSI": "HSI",     # 恒生指数
}

# 时间范围映射
PERIOD_MAP = {
    "1d": ("1d", "今日|今天|日内|一天"),
    "5d": ("5d", "本周|最近五天|近五天|近5天|这周"),
    "1mo": ("1mo", "本月|上个月|上月|最近一个月|近一个月|近1个月|过去一个月"),
    "3mo": ("3mo", "本季度|近三个月|近3个月|最近一季度|过去三个月"),
    "6mo": ("6mo", "近半年|近6个月|最近六个月|过去半年"),
    "ytd": ("ytd", "今年|今年以来|年初至今|今年至今"),
    "1y": ("1y", "近一年|最近一年|去年|过去一年|近1年"),
    "5y": ("5y", "近五年|近5年|过去五年"),
    "max": ("max", "全部|上市以来|所有|历史所有"),
}

INTENT_SYSTEM_PROMPT = """你是 Violetz 投资顾问的意图识别引擎。请分析用户输入，提取结构化信息。

返回严格的 JSON 格式（不要 markdown 包裹）：
{
    "intent": "query_price | query_history | analysis | compare | financials | deep_compare",
    "tickers": ["股票代码列表"],
    "timeframe": "1d | 5d | 1mo | 3mo | 6mo | ytd | 1y | 5y | max | null",
    "analysis_type": "valuation | trend | financial_health | general | null",
    "company_names": ["用户提到的公司名列表"]
}

意图说明：
- query_price: 查价格/实时行情
- query_history: 查历史走势/表现
- analysis: 综合分析/深度分析/估值分析
- compare: 对比分析
- financials: 查财报/财务数据
- deep_compare: 跨标的/时序多跳（如「对比 A 和 B 近三年盈利能力并给出估值」）

规则：
1. 将公司名转换为正确的股票代码（美股直接代码如 AAPL，港股加 .HK 如 0700.HK）
2. 如果用户没指定时间范围，query_price 用 null，其他用 "1mo"
3. analysis_type 仅在 intent=analysis 时有意义
4. 不确定的字段设为 null
5. company_names 保存原始公司名"""

# Function Calling 工具定义（OpenAI 格式，由 app.agent.llm 转换为 Anthropic 格式）
PARSE_INTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "parse_investment_intent",
        "description": "解析用户的投资查询意图，提取结构化参数。识别用户想查股价、历史走势、深度分析、对比分析还是财务数据。",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["query_price", "query_history", "analysis", "compare", "financials", "deep_compare"],
                    "description": "用户的查询意图类型: query_price=查实时行情, query_history=查历史走势, analysis=深度综合分析, compare=对比分析, financials=查财务数据, deep_compare=跨标的/时序多跳对比（如对比两者近三年盈利能力并估值）"
                },
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "股票代码列表。美股用直接代码如 AAPL/MSFT/GOOGL, 港股加 .HK 后缀如 0700.HK/3690.HK, A股加 .SS 或 .SZ 后缀如 600519.SS"
                },
                "timeframe": {
                    "type": ["string", "null"],
                    "enum": ["1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "5y", "max", None],
                    "description": "查询的时间范围。1d=日内, 5d=五天, 1mo=一个月, 3mo=三个月, 6mo=六个月, ytd=年初至今, 1y=一年, 5y=五年, max=全部。用户未指定且非 query_price 时可省略或传 null"
                },
                "analysis_type": {
                    "type": ["string", "null"],
                    "enum": ["valuation", "trend", "financial_health", "general", None],
                    "description": "分析类型。仅当 intent=analysis 时有效: valuation=估值分析, trend=趋势分析, financial_health=财务健康, general=综合分析。其他意图传 null"
                },
                "company_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "用户提到的原始公司名称或股票名称列表，保留用户的原话"
                }
            },
            "required": ["intent"]
        }
    }
}


def _parse_with_llm(user_input: str) -> dict | None:
    """使用 DeepSeek API 解析用户输入"""
    try:
        result = chat([
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ], task="intent")
        return parse_llm_json(result, model=IntentResult, default=None,
                              context="意图识别(JSON)")
    except Exception:
        return None


def _extract_ticker_fallback(text: str) -> list[str]:
    """兜底：从文本中提取已知公司名并映射为 ticker，未匹配时尝试 A 股搜索"""
    text_lower = text.lower()
    found = []
    for name, ticker in TICKER_MAP.items():
        if name.lower() in text_lower:
            if ticker not in found:
                found.append(ticker)

    # TICKER_MAP 未匹配时，尝试 yfinance Search 搜索 A 股
    if not found:
        chinese_names = _extract_chinese_company_names(text)
        for name in chinese_names:
            ticker = _resolve_a_share(name)
            if ticker:
                found.append(ticker)
                break

    return found


def _extract_timeframe_fallback(text: str) -> str | None:
    """兜底：从文本中提取时间范围"""
    text_lower = text.lower()
    for period_key, (_, pattern) in PERIOD_MAP.items():
        if re.search(pattern, text_lower):
            return period_key
    return None


def _extract_chinese_company_names(text: str) -> list[str]:
    """从文本中提取潜在的中文公司名（2-6个汉字），过滤常见非公司词"""
    # 匹配2-4个汉字的连续序列（公司名通常2-4字）
    sequences = re.findall(r'[一-鿿]{2,4}', text)
    skip_words = {
        "什么", "怎么", "如何", "多少", "今天", "明天", "最近", "现在",
        "这个", "那个", "哪个", "为什么", "然后", "可以", "应该", "已经",
        "趋势", "分析", "走势", "行情", "股价", "价格", "对比", "比较",
        "财报", "财务", "营收", "利润", "收入", "增长", "下降", "上涨",
        "下跌", "表现", "估值", "最近", "历史", "过去", "今年", "去年",
        "一月", "二月", "三月", "四月", "五月", "六月", "七月", "八月",
        "九月", "十月", "十一月", "十二月", "关于", "投资", "风险",
    }
    return [s for s in sequences if s not in skip_words]


# A 股搜索本地缓存（避免重复请求东方财富 API）
_A_SHARE_CACHE: dict[str, str] = {}


def _resolve_a_share(company_name: str) -> str | None:
    """使用东方财富搜索 API 查找 A 股公司 ticker（含 .SS/.SZ 后缀）"""
    if not company_name:
        return None
    cached = _A_SHARE_CACHE.get(company_name)
    if cached:
        return cached

    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://quote.eastmoney.com/',
    }
    url = ('https://searchadapter.eastmoney.com/api/suggest/get?input='
           + urllib.parse.quote(company_name) + '&count=5&type=14')

    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=10)
            raw = resp.read()
            data = json.loads(raw.decode('utf-8'))
            table = data.get('QuotationCodeTable', {})
            items = table.get('Data', [])

            for item in items:
                if item.get('Classify') != 'AStock':
                    continue
                code = item.get('Code', '')
                jys = item.get('JYS', '')
                if jys == '2':
                    ticker = code + '.SS'
                elif jys in ('6', '80'):
                    ticker = code + '.SZ'
                else:
                    continue
                _A_SHARE_CACHE[company_name] = ticker
                return ticker
            return None
        except Exception:
            continue
    return None


def _detect_intent_fallback(text: str) -> str:
    """兜底：简单规则判断意图"""
    text_lower = text.lower()
    # 多跳优先：对比 + 估值/近N年/盈利能力/财务状况 等复合信号 → deep_compare
    if "对比" in text and any(
        k in text for k in ("估值", "近一年", "近三年", "近五年", "三年", "五年",
                            "盈利能力", "财务状况", "并给出")
    ):
        return "deep_compare"
    if any(k in text_lower for k in ["对比", "比较", "vs", "和.*比", "哪个"]):
        return "compare"
    if any(k in text_lower for k in ["财报", "财务", "营收", "利润", "报表", "financial"]):
        return "financials"
    if any(k in text_lower for k in ["分析", "估值", "怎么样", "表现", "如何", "评价", "前景", "值得"]):
        return "analysis"
    if any(k in text_lower for k in ["走势", "历史", "行情", "涨", "跌", "表现", "趋势", "k线", "k线"]):
        return "query_history"
    return "query_price"


def _parse_with_function_calling(user_input: str) -> dict | None:
    """使用 DeepSeek Function Calling 解析用户输入（主路径）"""
    try:
        tool_calls = chat_tool_calls(
            messages=[
                {"role": "system", "content": "你是 Violetz 投资顾问的意图识别引擎。请根据用户输入调用 parse_investment_intent 工具返回结构化结果。"},
                {"role": "user", "content": user_input},
            ],
            tools=[PARSE_INTENT_TOOL],
            task="intent",
        )
        if not tool_calls:
            return None

        for tc in tool_calls:
            if tc["name"] == "parse_investment_intent":
                return parse_llm_json(tc["arguments"], model=IntentResult, default=None,
                                      context="意图识别(FC)")
        return None
    except Exception:
        return None


def _map_special_tickers(tickers: list[str]) -> list[str]:
    """将特殊指数代码映射为可交易标的代码"""
    return [SPECIAL_TICKER_MAP.get(t, t) for t in tickers]


def _build_context_prefix(history: list[dict]) -> str:
    """从对话历史构建上下文前缀，帮助 LLM 理解追问"""
    if not history:
        return ""

    # 提取最近 3 轮用户消息（排除当前轮）
    user_rounds = [h["text"] for h in history if h.get("role") == "user"]
    if not user_rounds:
        return ""

    recent = user_rounds[-3:]
    lines = ["[对话历史 - 以下是你与用户的之前对话，供参考理解当前提问]"]
    for i, text in enumerate(recent, 1):
        lines.append(f"第{i}轮: {text}")
    lines.append("---")
    lines.append("结合以上对话背景，理解用户当前提问：")
    return "\n".join(lines) + "\n"


def parse(user_input: str, history: list[dict] = None) -> dict:
    """解析用户输入，返回结构化意图

    Args:
        user_input: 用户自然语言输入
        history: 可选，最近对话历史列表 [{role, text}, ...]，用于追问上下文

    Returns:
        dict: {
            "intent": str,
            "tickers": list[str],
            "timeframe": str | None,
            "analysis_type": str | None,
            "raw_input": str
        }
    """
    # 构建上下文前缀：长期背景（画像/摘要）+ 追问语境，帮助 LLM 理解当前提问
    memory_prefix = build_memory_context()
    context_prefix = _build_context_prefix(history) if history else ""
    parts = [p for p in (memory_prefix, context_prefix) if p]
    llm_input = "\n".join(parts) + "\n" + user_input if parts else user_input

    # 第1优先：Function Calling 解析（使用增强后的输入）
    parsed = _parse_with_function_calling(llm_input)

    if parsed and isinstance(parsed, dict) and parsed.get("intent"):
        tickers = _map_special_tickers(parsed.get("tickers", []))
        timeframe = parsed.get("timeframe") or None
        return {
            "intent": parsed["intent"],
            "tickers": tickers if tickers else _extract_ticker_fallback(user_input),
            "timeframe": timeframe if timeframe else _extract_timeframe_fallback(user_input),
            "analysis_type": parsed.get("analysis_type"),
            "raw_input": user_input,
            "confidence": "high",
        }

    # 第2优先：传统 LLM prompt 解析（使用增强后的输入）
    parsed = _parse_with_llm(llm_input)

    if parsed and isinstance(parsed, dict) and parsed.get("intent"):
        tickers = _map_special_tickers(parsed.get("tickers", []))
        timeframe = parsed.get("timeframe") or None
        return {
            "intent": parsed["intent"],
            "tickers": tickers if tickers else _extract_ticker_fallback(user_input),
            "timeframe": timeframe if timeframe else _extract_timeframe_fallback(user_input),
            "analysis_type": parsed.get("analysis_type"),
            "raw_input": user_input,
            "confidence": "high",
        }

    # 第3优先：规则兜底（使用原始输入，不添加上下文前缀）
    # confidence=low 是真的低：这条路径靠关键词猜意图，模板外需求要另想办法。
    tickers = _extract_ticker_fallback(user_input)
    timeframe = _extract_timeframe_fallback(user_input)
    intent = _detect_intent_fallback(user_input)

    return {
        "intent": intent,
        "tickers": tickers,
        "timeframe": timeframe,
        "analysis_type": "general" if intent == "analysis" else None,
        "raw_input": user_input,
        "confidence": "low",
    }
