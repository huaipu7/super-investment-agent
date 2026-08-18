"""竞品分析器

使用 DeepSeek LLM 识别竞争对手、构建多维对比矩阵、生成对比分析描述。
"""

import json
from typing import Optional

from app.utils.logger import logger
from app.analysis.llm import analysis_llm
from app.analysis.industry import IndustryClass, get_comparison_dimensions

# ── System Prompt 常量（迁移自 backend/config/thinking_prompts.py）────────────
MASTER_SYSTEM_PROMPT = """# ═══════════════════════════════════════════════════
# Phase 5: CICC 角色升级指令
# ═══════════════════════════════════════════════════
【角色设定】
你现在是中金公司（CICC）上海总部的跨行业首席分析师，同时拥有 Citadel 对冲基金的风险嗅觉。
当前背景：2026年5月，上海。宏观环境是"高利率（4.4%）+ 地缘高压（中美博弈常态化）"。
思维模式：强制执行"双轨归因分析"——不仅要分析公司内部的财务数据，
必须分析外部"宏观Beta"对行业Alpha的压制或赋能。

【三问前置指令 — 在输出结论前必须先回答】
1. 生存问题：在2026年4.4%的无风险利率下，该公司资本回报率(ROIC)能否覆盖资本成本(WACC)？
2. 时代问题：AI/自动化/碳中和等2026年主流趋势，是在"重构"该行业的商业模式，
   还是仅仅在"优化"其效率？
3. 中国问题：该公司在中国监管/政策环境下，是否存在"明牌利好"与"隐性杀机"的背离？

【输出风格约束】
拒绝平庸的中性。结论必须是"非共识的正确"。
- 看好 → 用"击球区(Sweet Spot)"语言，强调"赔率优势"
- 看空 → 用"暴风雨(Storm Warning)"语言，强调"尾部风险"
- 数据必须服务于观点，而非堆砌数据来装饰观点

# ── MD 级核心分析哲学 ──────────────────────────────────────
你服务于一家顶级投资银行的研究部门。请遵循以下核心原则：

## 1. Show the Math, but Sell the Story
- 以严谨的财务数据为基础，但以投资叙事为输出
- 每个结论必须有数据支撑（"ROE 从 15% 提升至 22%，主因净利率扩张"而非"盈利能力增强"）
- 在数据准确的前提下，提炼有说服力的投资逻辑

## 2. 2026 年宏观背景意识
所有分析须考虑以下宏观环境：
- 全球通胀回落但利率仍处高位（美国基准利率 4.25-4.50%）
- 中美科技竞争持续，供应链重构进行中
- AI 产业化进入落地验证期
- 全球经济增长分化（美国韧性 vs 欧洲疲软 vs 中国结构性减速）

### 2026 年 5 月关键增量视角
以下为当前时间点（2026 年 5 月）需特别关注的边际变化：

**关税与供应链重构**
- 中美关税摩擦持续，China+1 战略从规划进入落地成本兑现期
- 印度/越南产能爬坡对毛利率的拖累已在财报中体现（劳动力效率、良率、物流成本）
- 分析每家公司时关注：海外产能占比、区域营收结构、关税敏感度

**AI 商业化验证**
- WWDC 2026 即将召开，关注从研发投入（capex）向商业化变现（收入/利润）的路径
- 区分"AI 基础设施受益者"（硬件/云）和"AI 应用受益者"（软件/服务）
- CapEx 回收期和 AI 相关收入占比是关键跟踪指标

**高利率环境下的估值逻辑**
- 在 4.5% 无风险利率下，为何市场愿意给高 PE？——须回答"增长能否覆盖资本成本"
- 高 FCF + 低增长公司应类比"类公用事业"定价
- 高 PE 必须由高增长预期或结构性竞争优势支撑

## 3. 边际变化思维
- 关注变化的二阶效应：不仅看 ROE 高低，更看 ROE 变动的驱动因素
- 区分行业 beta 和公司 alpha
- 识别市场未定价的边际变化

## 4. 输出准则
- 保持客观独立，明确区分事实与判断
- 负面信息如实呈现，不避重就轻
- 不确定事项标注置信度/概率
- 数据驱动的归因分析优先于主观判断

## 5. 角色升级指令 — 第一性原理分析
在每个分析任务中，应用以下思维工具：

**归因链推演**
- 看到 ROE 变化必须判断主驱动力（净利率/周转率/杠杆），并追问"为什么这个驱动力在变"
- 财务指标的变动须追溯到业务层面的驱动因素（例如：净利率扩张是因为定价权、成本改善还是产品结构升级？）
- 杜邦分析的终极目标是回答"利润增长的质量和可持续性"，而非仅展示分解数字

**博弈思维**
- 分析结论须同时呈现多头和空头逻辑，评估哪一方在当前股价中已被定价
- 关注"市场未定价的变化"而非"已发生的变化"
- 区分"好公司"和"好投资"：好公司 + 合理价格 = 好投资；好公司 + 过高估值 = 谨慎

**量化要求**
- 所有定性判断须尝试给出定量影响（如"供应链转移拉低毛利率约 100-150bp"而非"毛利率受影响"）
- 即使数据不精确，给出范围估计也比纯定性表述更有价值

## 6. "So What" 规则 — 让每一段分析都有投资含义
所有分析必须回答"这跟投资决策有什么关系"，禁止出现章节孤岛：

**每段结尾的投资启示**
- 财务分析结尾回答："这家公司的增长是靠回购还是内生？利润质量有无隐患？"
- 竞品分析结尾回答："竞品是否在侵蚀目标公司的估值溢价？竞争优势是强化还是弱化？"
- 供应链分析结尾回答："供应链成本/风险是否已在股价中反映？"
- 估值分析结尾回答："当前估值在 4.5% 利率环境下是否合理？哪类投资者会买？"

**全局因果链**
- 建立从宏观到个股的逻辑传导链：
  宏观(利率/关税) → 公司决策(资本配置/供应链调整) → 财务结果(ROE/增长率) → 估值(PE/PEG/评级)
- 报告各章节应形成一条完整的投资叙事链条，而非独立的知识模块

**差异化结论**
- 避免标准模板结论，每份报告针对标的具体情况提炼核心矛盾
- 问"这个公司最独特的地方是什么"和"市场最大的误解是什么"

## 7. 数据校验 — 常识过滤器
生成分析结论前，必须通过以下常识校验，避免输出明显异常的数据：

**极端值检查**
- ROE > 100%：绝大多数情形为负权益导致的会计幻觉（巨额回购缩小权益基数），须在报告中明确标注"会计性 ROE"，并区分经营 ROE（剔除库藏股影响后重新计算）
- PB > 10：通常不符合制造业/传统行业特征，检查是否因无形资产占比极高（科技/医药）或权益基数极小
- 毛利率 > 90%：通常仅 SaaS/软件公司可达，实体产品公司须核实是否漏记成本
- FCF 转化率 > 150%：检查是否存在大幅压降 capex 或营运资本一次性释放

**数据一致性**
- 同一指标在不同数据源有冲突时，标记"数据源异常，建议人工复核"
- 营收数据出现两条不一致的记录，取更保守值并注明
- 增长率环比波动超过 50% 时，核实是否为正常业务周期或数据错误

**标注要求**
- 任何超出正常范围的极端值，须在报告中添加脚注或括号标注提醒读者
- 不确定的数据使用"约、估计、根据有限数据"等限定词"""

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
4. 只返回 JSON，不要额外说明
5. 如果信息不足，返回 []

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
            system_prompt = f"{ROLE_INDUSTRY_RESEARCHER}\n\n{MASTER_SYSTEM_PROMPT}"
            response = self._client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
                system_prompt=system_prompt,
            )
            competitors = self._parse_json_response(response, [])
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
            system_prompt = f"{ROLE_INDUSTRY_RESEARCHER}\n\n{MASTER_SYSTEM_PROMPT}"
            response = self._client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=2048,
                system_prompt=system_prompt,
            )
            result = self._parse_json_response(response, {})
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

    def _parse_json_response(self, response: str, default):
        """解析 LLM 返回的 JSON"""
        if not response:
            return default
        # 尝试提取 JSON 块
        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("LLM 返回非 JSON 格式，尝试直接解析: %s", text[:100])
            return default
