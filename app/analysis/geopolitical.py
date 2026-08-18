"""地缘政治分析器

量化关税敏感性影响，生成地缘政治事件上下文。
用于分析管线中市场分析之后、竞品分析之前。
"""

import json
from datetime import datetime, timedelta

from app.utils.logger import logger
from app.analysis.llm import analysis_llm

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
            system_prompt = f"{ROLE_GEOPOLITICAL_ANALYST}\n\n{MASTER_SYSTEM_PROMPT}"
            response = self._client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
                system_prompt=system_prompt,
            )
            result = self._parse_json_response(response, {})
            if not isinstance(result, dict):
                result = {}
            logger.info("地缘政治事件分析完成: %d 个事件, 风险等级=%s",
                         len(result.get("events", [])), result.get("risk_level", "N/A"))
            return result
        except Exception as e:
            logger.error("地缘政治事件分析失败: %s", e)
            return {"events": [], "key_regions": [], "risk_level": "低"}

    def _parse_json_response(self, response: str, default):
        """解析 LLM 返回的 JSON"""
        if not response:
            return default
        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("LLM 返回非 JSON 格式: %s", text[:100])
            return default
