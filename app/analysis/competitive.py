"""竞品分析器

使用 DeepSeek LLM 识别竞争对手、构建多维对比矩阵、生成对比分析描述。
"""

from typing import Optional

from app.utils.logger import logger
from app.analysis.llm import analysis_llm
from app.analysis.industry import IndustryClass, get_comparison_dimensions
from app.analysis.master_prompt import build_system_prompt
from app.llm.schemas import Competitor, ComparisonResult
from app.llm.structured import parse_llm_json

ROLE_INDUSTRY_RESEARCHER = "你是一位资深行业研究员，擅长从行业格局和竞争态势中识别企业核心竞争力和战略定位。"


# 默认对比维度（industry_cls 未传入时使用）
_DEFAULT_COMPARISON_DIMENSIONS = [
    "市场份额",
    "产品矩阵",
    "品牌定位",
    "研发投入",
    "国际化程度",
]

# 常见公司名 → yfinance ticker 映射（作为 LLM 建议的补充）
NAME_TO_TICKER = {
    # ── 科技 mega-cap ──
    "apple": "AAPL",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "alphabet (google)": "GOOGL",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "meta": "META",
    "meta platforms": "META",
    "facebook": "META",
    "nvidia": "NVDA",
    "tesla": "TSLA",
    # ── 半导体 ──
    "tsmc": "TSM",
    "samsung": "005930.KS",
    "samsung electronics": "005930.KS",
    "intel": "INTC",
    "amd": "AMD",
    "qualcomm": "QCOM",
    "broadcom": "AVGO",
    "texas instruments": "TXN",
    "micron": "MU",
    "micron technology": "MU",
    "asml": "ASML",
    "applied materials": "AMAT",
    # ── 企业软件/IT 服务 ──
    "adobe": "ADBE",
    "salesforce": "CRM",
    "ibm": "IBM",
    "oracle": "ORCL",
    "cisco": "CSCO",
    "vmware": "VMW",
    "sap": "SAP",
    # ── 中国科技 ──
    "tencent": "0700.HK",
    "alibaba": "BABA",
    "baidu": "BIDU",
    "pinduoduo": "PDD",
    "jd.com": "JD",
    "meituan": "3690.HK",
    "xiaomi": "1810.HK",
    "netease": "NTES",
    "bytedance": None,
    "huawei": None,
    # ── 消费者科技 ──
    "netflix": "NFLX",
    "zoom": "ZM",
    "spotify": "SPOT",
    "paypal": "PYPL",
    "square": "SQ",
    "block": "SQ",
    "shopify": "SHOP",
    "snowflake": "SNOW",
    "sony": "SONY",
    "uber": "UBER",
    # ── 消费品牌 ──
    "mcdonald's": "MCD",
    "starbucks": "SBUX",
    "coca-cola": "KO",
    "pepsi": "PEP",
    "pepsico": "PEP",
    "walmart": "WMT",
    "costco": "COST",
    "home depot": "HD",
    "nike": "NKE",
    "disney": "DIS",
    "procter & gamble": "PG",
    "unilever": "UL",
    "nestlé": "NSRGY",
    "nestle": "NSRGY",
    "l'oréal": "LRLCY",
    "loreal": "LRLCY",
    "philip morris": "PM",
    "philip morris international": "PM",
    "altria": "MO",
    "mondelez": "MDLZ",
    "general mills": "GIS",
    "kraft heinz": "KHC",
    "colgate-palmolive": "CL",
    "estée lauder": "EL",
    "estee lauder": "EL",
    # ── 金融服务 ──
    "jpmorgan chase": "JPM",
    "jpmorgan": "JPM",
    "goldman sachs": "GS",
    "bank of america": "BAC",
    "wells fargo": "WFC",
    "citigroup": "C",
    "morgan stanley": "MS",
    "berkshire hathaway": "BRK-B",
    "berkshire": "BRK-B",
    "visa": "V",
    "mastercard": "MA",
    "american express": "AXP",
    "blackrock": "BLK",
    "us bancorp": "USB",
    "charles schwab": "SCHW",
    # ── 能源 ──
    "exxon mobil": "XOM",
    "exxonmobil": "XOM",
    "chevron": "CVX",
    "shell": "SHEL",
    "bp": "BP",
    "totalenergies": "TTE",
    "conocophillips": "COP",
    "schlumberger": "SLB",
    "halliburton": "HAL",
    # ── 医药健康 ──
    "johnson & johnson": "JNJ",
    "pfizer": "PFE",
    "merck": "MRK",
    "abbvie": "ABBV",
    "eli lilly": "LLY",
    "bristol-myers squibb": "BMY",
    "amgen": "AMGN",
    "gilead sciences": "GILD",
    "moderna": "MRNA",
    "unitedhealth": "UNH",
    "unitedhealth group": "UNH",
    "cvs health": "CVS",
    "thermo fisher scientific": "TMO",
    "astrazeneca": "AZN",
    "novartis": "NVS",
    "roche": "RHHBY",
    # ── 工业 ──
    "caterpillar": "CAT",
    "boeing": "BA",
    "lockheed martin": "LMT",
    "3m": "MMM",
    "general electric": "GE",
    "honeywell": "HON",
    "union pacific": "UNP",
    "fedex": "FDX",
    "ups": "UPS",
    "raytheon": "RTX",
    "raytheon technologies": "RTX",
    # ── 电信/公用事业/地产 ──
    "at&t": "T",
    "verizon": "VZ",
    "t-mobile": "TMUS",
    "comcast": "CMCSA",
    "nextera energy": "NEE",
    "duke energy": "DUK",
    "american tower": "AMT",
    "prologis": "PLD",
    # ── 金融（补充）──
    "ubs": "UBS",
    "ubs group": "UBS",
    "deutsche bank": "DB",
    "barclays": "BCS",
    "mitsubishi ufj": "MUFG",
    "mitsubishi ufj financial": "MUFG",
    "sumitomo mitsui": "SMFG",
    "icbc": "IDCBY",
    "industrial and commercial bank of china": "IDCBY",
    "china construction bank": "CICHY",
    "cmb china": "CIHKY",
    "china merchants bank (hk)": "CIHKY",
    # ── 能源（补充）──
    "eog resources": "EOG",
    "eog": "EOG",
    "baker hughes": "BKR",
    "baker hughes company": "BKR",
    "enbridge": "ENB",
    "canadian natural": "CNQ",
    "occidental petroleum": "OXY",
    "occidental": "OXY",
    "devon energy": "DVN",
    "pioneer natural": "PXD",
    # ── 医药健康（补充）──
    "novo nordisk": "NVO",
    "sanofi": "SNY",
    "bristol-myers": "BMY",
    "bristol myers squibb": "BMY",
    "takada": "TAK",
    "takeda pharmaceutical": "TAK",
    "bayer": "BAYRY",
    "glaxosmithkline": "GSK",
    "csl": "CSLLY",
    # ── 消费品（补充）──
    "anheuser-busch": "BUD",
    "anheuser busch inbev": "BUD",
    "budweiser": "BUD",
    "lvmh": "LVMUY",
    "hermes": "HESAY",
    "hermès": "HESAY",
    "diageo": "DEO",
    "fomento economico": "FMX",
    "femsa": "FMX",
    # ── 工业（补充）──
    "simens": "SIEGY",
    "siemens": "SIEGY",
    "abb": "ABBNY",
    "northrop grumman": "NOC",
    "general dynamics": "GD",
    "illinois tool works": "ITW",
    "deere": "DE",
    "john deere": "DE",
    "parker hannifin": "PH",
    "eaton": "ETN",
    # ── 电信（补充）──
    "deutsche telekom": "DTEGY",
    "vodafone": "VOD",
    "orange": "ORAN",
    "telefonica": "TEF",
    "bharti airtel": "BHRY",
    # ── 公用事业（补充）──
    "southern company": "SO",
    "southern co": "SO",
    "national grid": "NGG",
    "american electric power": "AEP",
    "dominion energy": "D",
    "sempra": "SRE",
    # ── 地产/REITs（补充）──
    "simon property": "SPG",
    "simon property group": "SPG",
    "realty income": "O",
    "digital realty": "DLR",
    "equinix": "EQIX",
    "crown castle": "CCI",
    "welltower": "WELL",
    "public storage": "PSA",
    # ── A 股 / 港股 ──
    "kweichow moutai": "600519.SS",
    "贵州茅台": "600519.SS",
    "wuliangye": "000858.SZ",
    "五粮液": "000858.SZ",
    "ping an insurance": "601318.SS",
    "中国平安": "601318.SS",
    "china merchants bank": "600036.SS",
    "招商银行": "600036.SS",
    "catl": "300750.SZ",
    "宁德时代": "300750.SZ",
    "byd": "002594.SZ",
    "比亚迪": "002594.SZ",
    "spacex": None,
    "openai": None,
}

# 建议竞品识别 prompt 模板（思维链）
COMPETITOR_COT_PROMPT = """请按以下三步推理过程，识别目标公司在{market}中的3-5家最主要竞争对手。

公司名称: {company_name}
行业: {industry}
业务描述: {business_summary}

### 第一步：现象描述
扫描{market}的市场格局，从以下角度观察：
- 市场集中度：是高度集中（寡头）还是分散竞争？
- 主要参与者：有哪些主要公司参与竞争？
- 进入壁垒：技术/资本/品牌/政策壁垒如何？

### 第二步：归因分析
分析该行业的关键成功因素，并评估目标公司表现：
- 技术/产品差异化程度
- 品牌/渠道/客户粘性
- 规模效应/成本优势

### 第三步：对手识别
基于以上推理，筛选3-5家最主要竞争对手，并解释竞争基础。

要求：
1. 返回 JSON 数组，每个元素包含 "name"(公司名)、"ticker"(股票代码) 和 "reason"(竞争原因)
2. 对于未上市公司，ticker 设为 null
3. 竞争对手必须在 {market} 中有重要业务
4. reason 不得超过 80 字，且不得写具体价格数值或价格区间
   （「千元价位」「次高端」可以，「600-1000 元价格带」不行——上下文没有价格数据）
5. 只返回 JSON，不要额外说明
6. 如果信息不足，返回 []

示例：
[{{"name": "贵州茅台", "ticker": "600519.SS", "reason": "同为高端白酒龙头企业"}}]"""

# 对比分析 prompt（思维链）
COMPARISON_COT_PROMPT = """请按以下三步推理过程，对目标公司和其竞争对手进行多维对比分析。

目标公司: {company_name}
行业: {industry}

竞品列表:
{competitors}

目标公司财务摘要:
{financial_summary}

### 第一步：现象描述
观察目标公司在竞争格局中的位置：
- 营收规模：与竞品相比处于什么梯队（领先/持平/落后）？
- 盈利能力：毛利率、净利率与竞品相比如何？
- 成长性：增速与竞品相比如何？

### 第二步：归因分析
逐维度对比评估，分析差距背后的驱动因素：
{comparison_dimensions}

对每个维度，评估目标公司和竞品的相对表现，并分析差距原因。

### 第三步：横向总结
- 总结竞争格局（目标公司处于什么位置）
- 竞争优势：哪些是可持续的？
- 竞争劣势：哪些是结构性而非暂时的？

输出 JSON 对象：
{{
    "matrix": [{{"dimension": str, "items": [{{"company": str, "assessment": str}}]}}],
    "summary": str,
    "competitive_advantage": str,
    "competitive_disadvantage": str,
    "attribution_notes": [str]
}}

要求：
1. "matrix" 是各维度下的公司对比评估
2. "summary" 是竞争格局概述（100 字以内）
3. "competitive_advantage" 是目标公司的竞争优势（50 字以内）
4. "competitive_disadvantage" 是目标公司的竞争劣势（50 字以内）
5. "attribution_notes" 是差距驱动因素列表（2-4条）
6. 只返回 JSON，不要额外说明"""


class CompetitiveAnalyzer:
    """竞品分析器"""

    def __init__(self):
        self._client = analysis_llm

    def identify_competitors(
        self,
        company_name: str,
        industry: str = "",
        business_summary: str = "",
        market: str = "全球市场",
        macro_line: str = "",
    ) -> list[dict]:
        """使用 DeepSeek 识别竞品

        Args:
            company_name: 公司名称
            industry: 行业
            business_summary: 业务描述
            market: 市场范围（"全球市场"/"美国市场"/"中国市场"）

        Returns:
            [{"name": str, "ticker": str|None, "reason": str}, ...]
        """
        if not company_name and not industry:
            logger.warning("缺少公司名和行业信息，无法识别竞品")
            return []

        prompt = COMPETITOR_COT_PROMPT.format(
            company_name=company_name or "未知",
            industry=industry or "未知",
            business_summary=business_summary[:500] if business_summary else "暂无详细业务描述",
            market=market,
        )

        try:
            system_prompt = build_system_prompt(ROLE_INDUSTRY_RESEARCHER, macro_line)
            response = self._client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
                system_prompt=system_prompt,
            )
            competitors = parse_llm_json(response, model=list[Competitor], default=[],
                                         context="竞品识别")
            if not isinstance(competitors, list):
                competitors = []
            logger.info("识别到 %d 个竞品: %s", len(competitors),
                        [c.get("name", "?") for c in competitors])
            return competitors
        except Exception as e:
            logger.error("竞品识别失败: %s", e)
            return []

    def generate_comparison(
        self,
        company_name: str,
        industry: str,
        financial_summary: str,
        competitors: list[dict],
        industry_cls: IndustryClass = IndustryClass.UNKNOWN,
        macro_line: str = "",
    ) -> dict:
        """生成多维竞争对比分析

        Args:
            company_name: 目标公司名
            industry: 行业
            financial_summary: 目标公司财务摘要文本
            competitors: [{"name": str, ...}, ...]
            industry_cls: 行业分类（影响对比维度选择）

        Returns:
            {
                "matrix": [{"dimension": str, "items": [{"company": str, "assessment": str}]}],
                "summary": str,
                "competitive_advantage": str,
                "competitive_disadvantage": str,
            }
        """
        if not competitors:
            return {
                "matrix": [],
                "summary": "无竞品数据，无法进行对比分析",
                "competitive_advantage": "",
                "competitive_disadvantage": "",
            }

        competitor_names = "\n".join(
            f"- {c.get('name', '?')}: {c.get('reason', '')}"
            for c in competitors
        )

        dimensions = get_comparison_dimensions(industry_cls)
        dims_text = "\n".join(f"- {d}" for d in dimensions)

        prompt = COMPARISON_COT_PROMPT.format(
            company_name=company_name,
            industry=industry,
            competitors=competitor_names,
            financial_summary=financial_summary[:1000] if financial_summary else "暂无财务数据",
            comparison_dimensions=dims_text,
        )

        try:
            system_prompt = build_system_prompt(ROLE_INDUSTRY_RESEARCHER, macro_line)
            response = self._client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=2048,
                system_prompt=system_prompt,
            )
            result = parse_llm_json(response, model=ComparisonResult, default={},
                                    context="竞品对比")
            if not isinstance(result, dict):
                result = {}
            return result
        except Exception as e:
            logger.error("竞品对比分析失败: %s", e)
            return {
                "matrix": [],
                "summary": "对比分析生成失败",
                "competitive_advantage": "",
                "competitive_disadvantage": "",
            }

    def build_financial_summary(
        self,
        financials: dict,
        company_info: Optional[dict] = None,
    ) -> str:
        """从财务分析结果构建可读的财务摘要"""
        parts = []

        ratios = financials.get("ratios", {})
        if ratios:
            latest = {}
            for code, series in ratios.items():
                if series:
                    last = series[-1]
                    latest[code] = last["value"]

            from app.constants import FINANCIAL_RATIOS
            for code in ["ROE", "GROSS_MARGIN", "NET_MARGIN", "CURRENT_RATIO", "DEBT_TO_EQUITY"]:
                if code in latest and latest[code] is not None:
                    name = FINANCIAL_RATIOS.get(code, code)
                    parts.append(f"{name}: {latest[code]:.2%}" if abs(latest[code]) < 10 else f"{name}: {latest[code]:.2f}")

        growth = financials.get("growth_rates", {})
        if growth:
            for code, series in growth.items():
                if series and series[-1]["value"] is not None:
                    name = FINANCIAL_RATIOS.get(code, code)
                    val = series[-1]["value"]
                    parts.append(f"{name}: {val:+.2%}")

        periods = financials.get("periods", [])
        if periods:
            parts.append(f"分析期间: {periods[0]}~{periods[-1]}")

        company_info = company_info or {}
        if company_info.get("market_cap"):
            cap = company_info["market_cap"]
            parts.append(f"市值: {cap:,.0f}")

        return "；".join(parts) if parts else "暂无财务数据"

    # ── 内部方法 ──────────────────────────────────────────────

    @staticmethod
    def resolve_ticker(company_name: str) -> str | None:
        """通过已知映射查找公司对应的 yfinance ticker"""
        key = company_name.lower().strip()
        # 直接匹配
        if key in NAME_TO_TICKER:
            return NAME_TO_TICKER[key]
        # 尝试部分匹配
        for name, ticker in NAME_TO_TICKER.items():
            if name in key or key in name:
                return ticker
        return None

