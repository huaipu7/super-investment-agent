"""地缘政治分析器

量化关税敏感性影响，生成地缘政治事件上下文。
用于分析管线中市场分析之后、竞品分析之前。
"""

from datetime import datetime, timedelta

from app.utils.logger import logger
from app.analysis.llm import analysis_llm
from app.analysis.master_prompt import build_system_prompt
from app.llm.schemas import GeopoliticalResult
from app.llm.structured import parse_llm_json

ROLE_GEOPOLITICAL_ANALYST = "你是一位地缘政治风险分析师，擅长识别和评估国际政治经济事件对企业经营的潜在影响。"


# 美国科技公司对中国敞口的行业默认估计值
# 来源：彭博/高盛行业研究报告中对标普500科技公司的中国敞口估算
_CHINA_EXPOSURE_BY_INDUSTRY: dict[str, float] = {
    # 科技
    "semiconductor": 0.25,
    "semiconductors": 0.25,
    "technology": 0.18,
    "electronics": 0.22,
    "computer": 0.15,
    "communication": 0.12,
    "software": 0.10,
    "internet": 0.15,
    "consumer electronics": 0.28,
    "hardware": 0.20,
    "internet content & information": 0.12,
    # 金融
    "banking": 0.08,
    "financial services": 0.08,
    "insurance": 0.05,
    # 医药
    "pharmaceutical": 0.15,
    "biotechnology": 0.10,
    "healthcare": 0.08,
    # 能源
    "energy": 0.10,
    "oil": 0.08,
    "gas": 0.08,
    # 工业
    "industrial": 0.12,
    "manufacturing": 0.15,
    "automotive": 0.20,
    "materials": 0.12,
    # 消费/公用/地产
    "consumer": 0.15,
    "utility": 0.05,
    "real estate": 0.05,
}

_DEFAULT_CHINA_EXPOSURE = 0.18

# 关税情景：从当前 10% 升至 25%
_TARIFF_INCREASE = 0.15

_US_COUNTRY_CODES = {"US", "USA", "UNITED STATES"}

_TECH_KEYWORDS = [
    "technology", "software", "semiconductor", "electronics",
    "computer", "communication", "internet", "hardware",
    "互联网", "科技", "电子", "半导体", "通信", "信息技术", "软件",
]

_GEO_EVENT_COT_PROMPT = """请按以下三步推理过程，分析未来6个月（{date} ~ {date_6m}）可能影响该公司业务的地缘政治事件。

公司名称: {company_name}
行业: {industry}
业务描述: {business_summary}

### 第一步：现象描述
扫描该公司的地缘暴露面：
- 市场分布：主要营收来源地区，是否集中于高风险区域？
- 供应链：关键原材料或零部件是否依赖特定地区？
- 监管环境：是否涉及跨境数据流动、出口管制等领域？
- 地缘敏感度：该公司所在行业是否受地缘冲突影响显著？

### 第二步：归因分析
分析地缘风险的传导机制：
- 关税/贸易壁垒：通过成本端还是收入端影响？
- 出口管制：对技术/产品供应的影响路径？
- 区域冲突：对供应链/市场准入的具体影响？

### 第三步：情景评估
综合以上分析，输出2-4个最可能发生的具体事件及其影响评估。

输出 JSON：
{{"events": [{{"event": str, "region": str, "impact": str, "probability": str, "transmission_path": str}}], "key_regions": [str], "risk_level": str}}

要求：
1. events 列出 2-4 个事件，每个含 transmission_path（传导机制说明）
2. key_regions 列出关键地缘政治区域（1-3个）
3. risk_level 为整体地缘风险等级（高/中/低）
4. 只返回 JSON，不要额外说明"""


class GeopoliticalAnalyzer:
    """地缘政治分析器"""

    def __init__(self):
        self._client = analysis_llm

    def analyze_tariff_sensitivity(
        self,
        financial_analysis: dict,
        company_info: dict,
        market_analysis: dict,
    ) -> dict:
        """关税敏感性量化测试

        对在中国有业务敞口的美国公司，模拟关税从10%升至25%的毛利率影响。
        公式：毛利率影响 = 关税增幅 × 中国敞口比例 × 成本率

        Returns:
            {"applicable": bool, "description": str, ...}
        """
        country = (company_info.get("country") or "").upper()
        industry = (company_info.get("industry") or "").lower()
        summary = company_info.get("summary", "") or ""
        company_name = company_info.get("name", "")

        # 判断是否适用：美国公司 + 有中国业务敞口
        is_us_company = any(c in country for c in _US_COUNTRY_CODES)
        if not is_us_company:
            return {
                "applicable": False,
                "message": f"{company_name}注册地不在美国，不适用美国对华关税敏感性分析",
                "china_exposure": None,
                "gross_margin_impact": None,
            }

        # 判断中国敞口：从市场数据或关键词推断
        markets_data = market_analysis.get("markets", {})
        china_market = markets_data.get("china", {})
        china_revenue = china_market.get("company", {}).get("revenue")
        global_market = markets_data.get("global", {})
        global_revenue = global_market.get("company", {}).get("revenue")

        ctx = (industry + " " + summary[:400]).lower()
        has_china_keywords = any(kw in ctx for kw in [
            "china", "chinese", "asia", "supply chain", "manufacturing",
            "中国", "亚洲", "供应链",
        ])
        has_tech_keywords = any(kw in ctx for kw in _TECH_KEYWORDS)

        if not has_china_keywords and china_revenue is None:
            return {
                "applicable": False,
                "message": f"{company_name}未识别到显著中国业务敞口，不适用关税敏感性分析",
                "china_exposure": None,
                "gross_margin_impact": None,
            }

        # 中国敞口估计（追踪来源，Phase 1：显式标注不静默）
        china_exposure = _DEFAULT_CHINA_EXPOSURE
        exposure_source = "科技行业默认值 0.18（无数据）"
        exposure_confidence = "low"
        if china_revenue and global_revenue and global_revenue > 0:
            raw_exposure = float(china_revenue) / float(global_revenue)
            if raw_exposure > 0.99:
                # 中国营收 ≈ 全球营收，说明数据源以总营收填充了分地域明细
                # 回退到行业默认敞口，避免错误显示100%中国敞口
                for ind_key, exposure in _CHINA_EXPOSURE_BY_INDUSTRY.items():
                    if ind_key in industry:
                        china_exposure = exposure
                        exposure_source = f"行业默认敞口（{ind_key}，彭博/高盛行业报告估算）"
                        exposure_confidence = "medium"
                        break
                # 未匹配到行业敞口时保持 _DEFAULT_CHINA_EXPOSURE
            else:
                china_exposure = min(raw_exposure, 1.0)
                exposure_source = "中国营收/全球营收真实占比"
                exposure_confidence = "high"
        else:
            # 按行业默认值
            for ind_key, exposure in _CHINA_EXPOSURE_BY_INDUSTRY.items():
                if ind_key in industry:
                    china_exposure = exposure
                    exposure_source = f"行业默认敞口（{ind_key}，彭博/高盛行业报告估算）"
                    exposure_confidence = "medium"
                    break
            # 如果是科技公司但行业未匹配到，用默认值
            if has_tech_keywords and not china_revenue:
                china_exposure = _DEFAULT_CHINA_EXPOSURE

        # 成本率 = 1 - 毛利率
        ratios = financial_analysis.get("ratios", {})
        gm_series = ratios.get("GROSS_MARGIN", [])
        cost_ratio = None
        if gm_series and gm_series[-1]["value"] is not None:
            cost_ratio = 1.0 - gm_series[-1]["value"]

        if cost_ratio is None or cost_ratio <= 0:
            return {
                "applicable": False,
                "message": f"{company_name}缺少成本数据，无法计算关税敏感性",
                "china_exposure": china_exposure,
                "gross_margin_impact": None,
            }

        # 关税敏感性计算
        gm_impact = _TARIFF_INCREASE * china_exposure * cost_ratio

        result = {
            "applicable": True,
            "scenario": "美国对华关税从10%提升至25%（+15个百分点）",
            "china_exposure": round(china_exposure, 4),
            "china_exposure_estimate": {
                "value": round(china_exposure, 4),
                "source": exposure_source,
                "confidence": exposure_confidence,
                "note": "该敞口为估算值，报告/看板不得呈现为真实披露数据",
            },
            "cost_ratio": round(cost_ratio, 4),
            "gross_margin_impact": round(gm_impact, 4),
            "description": (
                f"若美国对华关税从10%提升至25%（+15pp），"
                f"基于中国业务敞口约{china_exposure:.1%}和成本占营收比{cost_ratio:.1%}，"
                f"预计毛利率将下降约{gm_impact*100:.1f}个百分点。"
            ),
        }

        logger.info("关税敏感性测试: %s 毛利率影响=%.1fpp (敞口=%.1f%%, 成本率=%.1f%%)",
                     company_name, gm_impact * 100, china_exposure * 100, cost_ratio * 100)
        return result

    def analyze_geopolitical_events(
        self,
        company_name: str,
        industry: str = "",
        business_summary: str = "",
        macro_line: str = "",
    ) -> dict:
        """使用 LLM 生成地缘政治事件上下文

        Returns:
            {"events": [...], "key_regions": [...], "risk_level": str}
        """
        if not company_name:
            return {"events": [], "key_regions": [], "risk_level": "低"}

        # 动态生成分析日期
        today = datetime.now().strftime("%Y-%m-%d")
        six_months = (datetime.now() + timedelta(days=182)).strftime("%Y-%m-%d")
        prompt = _GEO_EVENT_COT_PROMPT.format(
            date=today,
            date_6m=six_months,
            company_name=company_name,
            industry=industry or "未知",
            business_summary=business_summary[:500] if business_summary else "暂无详细业务描述",
        )

        try:
            system_prompt = build_system_prompt(ROLE_GEOPOLITICAL_ANALYST, macro_line)
            response = self._client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
                system_prompt=system_prompt,
            )
            result = parse_llm_json(response, model=GeopoliticalResult, default={},
                                    context="地缘政治")
            if not isinstance(result, dict):
                result = {}
            logger.info("地缘政治事件分析完成: %d 个事件, 风险等级=%s",
                         len(result.get("events", [])), result.get("risk_level", "N/A"))
            return result
        except Exception as e:
            logger.error("地缘政治事件分析失败: %s", e)
            return {"events": [], "key_regions": [], "risk_level": "低"}

