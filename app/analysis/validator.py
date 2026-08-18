"""事实核查与常识过滤器

在分析管线中插入此模块，对财务数据、市场数据和竞品名称进行
逻辑自洽性检查、常识校验和拼写修正。
"""

import difflib
from typing import Optional

from app.constants import FINANCIAL_RATIOS
from app.utils.logger import logger


# 已知公司名白名单（用于模糊匹配拼写修正）
# 来源于 competitive_analyzer.NAME_TO_TICKER 的 key
KNOWN_COMPANY_NAMES = [
    # ── 科技 mega-cap ──
    "Apple", "Apple Inc.", "Alphabet", "Alphabet Inc.", "Google",
    "Microsoft", "Microsoft Corporation", "Amazon", "Amazon.com",
    "Meta", "Meta Platforms", "Facebook", "Nvidia", "Tesla",
    # ── 半导体 ──
    "TSMC", "Samsung", "Samsung Electronics", "Intel", "AMD", "Qualcomm",
    "Broadcom", "Texas Instruments", "Micron Technology", "ASML",
    "Applied Materials",
    # ── 企业软件/IT 服务 ──
    "Adobe", "Salesforce", "IBM", "Oracle", "Cisco", "VMware", "SAP",
    # ── 中国科技 ──
    "Tencent", "Alibaba", "Baidu", "Pinduoduo", "JD.com", "Meituan",
    "Xiaomi", "NetEase", "ByteDance", "Huawei",
    # ── 消费者科技 ──
    "Netflix", "Zoom", "Spotify", "PayPal", "Square", "Block",
    "Shopify", "Snowflake", "Uber", "Sony",
    # ── 消费品牌 ──
    "McDonald's", "Starbucks", "Coca-Cola", "Pepsi", "PepsiCo",
    "Walmart", "Costco", "Home Depot", "Nike", "Disney",
    "Procter & Gamble", "Unilever", "Nestlé", "L'Oréal",
    "Philip Morris International", "Altria", "Mondelez",
    "General Mills", "Kraft Heinz", "Colgate-Palmolive",
    "Estée Lauder",
    # ── 金融服务 ──
    "JPMorgan Chase", "Goldman Sachs", "Bank of America",
    "Wells Fargo", "Citigroup", "Morgan Stanley",
    "Berkshire Hathaway", "Visa", "Mastercard", "American Express",
    "BlackRock", "U.S. Bancorp", "Charles Schwab",
    # ── 能源 ──
    "Exxon Mobil", "ExxonMobil", "Chevron", "Shell", "BP",
    "TotalEnergies", "ConocoPhillips", "Schlumberger", "Halliburton",
    # ── 医药健康 ──
    "Johnson & Johnson", "Pfizer", "Merck", "AbbVie", "Eli Lilly",
    "Bristol-Myers Squibb", "Amgen", "Gilead Sciences", "Moderna",
    "UnitedHealth Group", "CVS Health", "Thermo Fisher Scientific",
    "AstraZeneca", "Novartis", "Roche",
    # ── 工业 ──
    "Caterpillar", "Boeing", "Lockheed Martin", "3M",
    "General Electric", "Honeywell", "Union Pacific", "FedEx", "UPS",
    "Raytheon Technologies",
    # ── 电信/公用事业/地产 ──
    "AT&T", "Verizon", "T-Mobile", "Comcast", "NextEra Energy",
    "Duke Energy", "American Tower", "Prologis",
    # ── A 股 / 港股 ──
    "Kweichow Moutai", "贵州茅台", "Wuliangye", "五粮液",
    "Ping An Insurance", "中国平安", "China Merchants Bank", "招商银行",
    "CATL", "宁德时代", "BYD", "比亚迪",
    # ── 其他 ──
    "SpaceX", "OpenAI",
]

# 公司名常见后缀和格式修正模式
_NAME_SUFFIX_PATTERNS = {
    "Ine.": "Inc.",
    "Ince.": "Inc.",
    "Corperation": "Corporation",
    "Corp.": "Corporation",
    "Technologis": "Technologies",
    "Technolgies": "Technologies",
    "Tehnologies": "Technologies",
}


def _normalize_for_match(name: str) -> str:
    """标准化公司名用于模糊匹配"""
    name = name.lower().strip()
    name = name.replace(",", "").replace(".", "").replace("  ", " ")
    for suffix in [" inc", " ltd", " limited", " corp", " corporation",
                   " technologies", " technology", " group", " holdings",
                   " co", " company", " plc"]:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name.strip()


class FactChecker:
    """事实核查器

    对分析管线中的中间结果进行三类检查：
    1. 逻辑自洽性（财务数据矛盾）
    2. 市场常识（地域营收合理性）
    3. 竞品名称拼写
    """

    def run(
        self,
        financial_analysis: dict,
        market_analysis: dict,
        company_info: dict,
        competitors: Optional[list[dict]] = None,
    ) -> dict:
        """执行全部检查

        Returns:
            {
                "flags": [{"type": str, "severity": str, "message": str}, ...],
                "corrected_competitors": list[dict] | None,
                "warnings": [str, ...],
            }
        """
        flags = []
        warnings = []

        # 1. 逻辑自洽性
        flags.extend(self.check_logical_consistency(financial_analysis))

        # 2. 市场常识
        extra = self.check_market_commonsense(market_analysis, company_info)
        flags.extend(extra["flags"])
        warnings.extend(extra["warnings"])

        # 3. 竞品名称拼写
        corrected = None
        if competitors:
            corrected = self.check_competitor_names(competitors)

        return {
            "flags": flags,
            "corrected_competitors": corrected,
            "warnings": warnings,
        }

    # ── 1. 逻辑自洽性检查 ────────────────────────────────────

    def check_logical_consistency(self, financial_analysis: dict) -> list[dict]:
        """检查财务指标之间的逻辑矛盾"""
        flags = []
        ratios = financial_analysis.get("ratios", {})

        # 检查1: FCF 为正但标记了"偿债风险"
        fcf = ratios.get("FREE_CASH_FLOW", [])
        nde = ratios.get("NET_DEBT_TO_EQUITY", [])

        if fcf and fcf[-1]["value"] is not None and nde and nde[-1]["value"] is not None:
            if fcf[-1]["value"] > 0 and nde[-1]["value"] < 0:
                flags.append({
                    "type": "consistency",
                    "severity": "info",
                    "message": "公司处于净现金状态且自由现金流为正，不应标记为偿债风险",
                })

        # 检查2: 营收负增长但 CAPEX 大幅增加（扩张期特征，非风险）
        growth = financial_analysis.get("growth_rates", {})
        rg = growth.get("REVENUE_GROWTH", [])
        capex = ratios.get("CAPEX_TO_REVENUE", [])

        if rg and rg[-1]["value"] is not None and capex and capex[-1]["value"] is not None:
            if rg[-1]["value"] < 0 and capex[-1]["value"] > 0.1:
                flags.append({
                    "type": "consistency",
                    "severity": "info",
                    "message": "营收下滑但资本支出占比仍高（{:.1%}），表明处于产能扩张期，需结合行业周期判断".format(capex[-1]["value"]),
                })

        # 检查3: 毛利率极低但 R&D 极高（不合理的商业模式）
        gm = ratios.get("GROSS_MARGIN", [])
        rd = ratios.get("RD_TO_REVENUE", [])

        if gm and gm[-1]["value"] is not None and rd and rd[-1]["value"] is not None:
            if gm[-1]["value"] < 0.1 and rd[-1]["value"] > 0.3:
                flags.append({
                    "type": "consistency",
                    "severity": "warning",
                    "message": f"毛利率 {gm[-1]['value']:.1%} 极低但研发费用率 {rd[-1]['value']:.1%} 极高，数据可能异常",
                })

        return flags

    # ── 2. 市场常识校验 ──────────────────────────────────────

    def check_market_commonsense(self, market_analysis: dict, company_info: dict) -> dict:
        """检查地域营收数据的常识合理性"""
        flags = []
        warnings = []
        markets_data = market_analysis.get("markets", {})

        country = (company_info.get("country") or "").upper()
        company_name = company_info.get("name", "")

        # 收集各市场营收
        market_revenues = {}
        for mk_key, mk_label in [("global", "全球"), ("us", "美国"), ("china", "中国")]:
            mk = markets_data.get(mk_key, {})
            mk_company = mk.get("company", {})
            rev = mk_company.get("revenue")
            if rev:
                market_revenues[mk_label] = float(rev)

        if len(market_revenues) < 2:
            return {"flags": flags, "warnings": warnings}

        rev_values = list(market_revenues.values())
        max_rev = max(rev_values)
        min_rev = min(rev_values)

        # 检查1: 三个市场营收完全相同（数据质量问题）
        if max_rev > 0 and (max_rev - min_rev) / max_rev < 0.01:
            warnings.append("全球/美国/中国三个市场的营收数据完全相同，数据源未提供分地域明细")
            flags.append({
                "type": "market_data",
                "severity": "warning",
                "message": "全球/美国/中国三个市场的营收数据完全相同，数据源未提供分地域明细",
            })

        # 检查2: 美国公司但中国营收明显高于美国
        if country in ("US", "USA", "UNITED STATES"):
            us_rev = market_revenues.get("美国")
            cn_rev = market_revenues.get("中国")
            if us_rev and cn_rev and cn_rev > us_rev * 1.2:
                warnings.append(f"美国公司 {company_name} 的中国营收 ({cn_rev:,.0f}) 超过美国营收 ({us_rev:,.0f})，数据可能存在异常")

        # 检查3: 中国公司但美国营收高于中国
        if country in ("CN", "CHN", "CHINA"):
            cn_rev = market_revenues.get("中国")
            us_rev = market_revenues.get("美国")
            if cn_rev and us_rev and us_rev > cn_rev * 1.2:
                warnings.append(f"中国公司 {company_name} 的美国营收 ({us_rev:,.0f}) 超过中国营收 ({cn_rev:,.0f})，数据可能存在异常")

        return {"flags": flags, "warnings": warnings}

    # ── 3. 竞品名称拼写校验 ──────────────────────────────────

    def check_competitor_names(self, competitors: list[dict]) -> list[dict]:
        """对竞品列表中的公司名做模糊匹配修正"""
        corrected = []
        for comp in competitors:
            original_name = comp.get("name", "")
            if not original_name:
                corrected.append(comp)
                continue

            # Step 1: 应用硬编码模式修正
            name = original_name
            for wrong, correct in _NAME_SUFFIX_PATTERNS.items():
                if wrong in name:
                    name = name.replace(wrong, correct)

            # Step 2: 对不在白名单中的名称做模糊匹配
            if name not in KNOWN_COMPANY_NAMES:
                normalized = _normalize_for_match(name)
                matches = difflib.get_close_matches(
                    normalized,
                    [_normalize_for_match(kn) for kn in KNOWN_COMPANY_NAMES],
                    n=1, cutoff=0.75
                )
                if matches:
                    # 找到白名单中的原始名称
                    for kn in KNOWN_COMPANY_NAMES:
                        if _normalize_for_match(kn) == matches[0]:
                            name = kn
                            break

            if name != original_name:
                logger.debug("竞品名称修正: '%s' -> '%s'", original_name, name)

            comp = dict(comp)
            comp["name"] = name
            comp["name_original"] = original_name
            corrected.append(comp)

        return corrected
