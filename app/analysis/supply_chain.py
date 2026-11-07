"""供应链深度分析器

China+1 成本量化、供应链中断情景分析、华为竞争/供应链影响评估。
基于规则+行业配置，不使用 LLM（确保可重复性）。
"""

from typing import Optional

from app.utils.logger import logger
from app.analysis.industry import (
    IndustryClass, get_supply_chain_config,
    CHINA_PLUS_1_COST_BASES, SUPPLY_CHAIN_SCENARIOS,
)

# 制造业劳动力成本占总 COGS 的参考比例
_LABOR_SHARE_OF_COGS = 0.25

# 适用 Huawei 竞争分析的行业
_HUAWEI_RELEVANT_INDUSTRIES = {IndustryClass.TECH_HARDWARE, IndustryClass.TELECOM}


class SupplyChainAnalyzer:
    """供应链深度分析器"""

    def analyze(
        self,
        company_info: dict,
        financial_analysis: dict,
        geopolitical_analysis: dict,
        market_analysis: dict,
        industry_cls: IndustryClass = IndustryClass.UNKNOWN,
    ) -> dict:
        """执行完整的供应链深度分析

        Args:
            company_info: 公司基本信息
            financial_analysis: 财务分析结果
            geopolitical_analysis: 地缘政治分析结果（含关税敏感性）
            market_analysis: 市场分析结果（含地域分布）
            industry_cls: 行业分类

        Returns:
            {
                "china_plus_1": {...} | None,        # China+1 成本分析
                "disruption_scenarios": {...} | None, # 情景分析矩阵
                "huawei_impact": {...} | None,        # 华为影响评估
                "supplier_concentration": {...},      # 供应商集中度评估
                "summary": str,                       # 一句话结论
            }
        """
        sc_cfg = get_supply_chain_config(industry_cls)

        # 1. China+1 成本分析
        china_plus_1 = self.analyze_china_plus_1(
            company_info=company_info,
            financial_analysis=financial_analysis,
            geopolitical_analysis=geopolitical_analysis,
            industry_cls=industry_cls,
            sc_cfg=sc_cfg,
        )

        # 2. 供应链中断情景分析
        scenarios = self.analyze_disruption_scenarios(
            company_info=company_info,
            financial_analysis=financial_analysis,
            geopolitical_analysis=geopolitical_analysis,
            industry_cls=industry_cls,
            sc_cfg=sc_cfg,
        )

        # 3. 华为竞争/供应链影响评估
        huawei = self.analyze_huawei_impact(
            company_info=company_info,
            industry_cls=industry_cls,
        )

        # 4. 供应商集中度评估
        supplier_conc = self.assess_supplier_concentration(
            market_analysis=market_analysis,
            industry_cls=industry_cls,
            sc_cfg=sc_cfg,
        )

        # 综合摘要
        summary_parts = []
        if scenarios and scenarios.get("expected_gm_impact_bp"):
            summary_parts.append(
                f"供应链中断情景分析显示预期毛利率影响约"
                f"{scenarios['expected_gm_impact_bp']:.0f}bp"
            )
        if china_plus_1 and china_plus_1.get("feasible_bases"):
            count = len(china_plus_1["feasible_bases"])
            summary_parts.append(f"识别到 {count} 个可行的 China+1 替代基地")
        if huawei and huawei.get("is_relevant"):
            summary_parts.append("华为的技术自给战略对该公司的竞争格局有显著影响")

        # 估算/回退假设显式标注（Phase 1：不静默编造）
        estimates_used: list[dict] = [
            {"param": "关税税率", "value": 0.25,
             "source": "情景假设：当前对华关税税率", "confidence": "low"},
            {"param": "关税→毛利率传导", "value": 0.25,
             "source": "行业经验假设", "confidence": "low"},
            {"param": "经营杠杆系数(DOL)", "value": 1.5,
             "source": "行业经验假设", "confidence": "low"},
        ]
        if china_plus_1 and china_plus_1.get("applicable"):
            cp_exp = china_plus_1.get("china_exposure")
            estimates_used.append({
                "param": "China+1 中国敞口", "value": cp_exp,
                "source": ("业务描述关键词推断（无真实分部数据）" if cp_exp == 0.20
                           else "来自关税敏感性分析（估算链）"),
                "confidence": "low" if cp_exp == 0.20 else "medium"})
        if scenarios and scenarios.get("applicable"):
            estimates_used.append({
                "param": "中断情景中国敞口", "value": scenarios.get("china_exposure"),
                "source": "供应链情景假设（无真实分部数据）", "confidence": "low"})
            if scenarios.get("base_gross_margin") == 0.35:
                estimates_used.append({
                    "param": "基础毛利率", "value": 0.35,
                    "source": "行业参考默认值（无财务数据）", "confidence": "low"})
            if scenarios.get("base_net_margin") == 0.15:
                estimates_used.append({
                    "param": "基础净利率", "value": 0.15,
                    "source": "行业参考默认值（无财务数据）", "confidence": "low"})

        result = {
            "china_plus_1": china_plus_1,
            "disruption_scenarios": scenarios,
            "huawei_impact": huawei,
            "supplier_concentration": supplier_conc,
            "estimates_used": estimates_used,
            "summary": "；".join(summary_parts) if summary_parts else "供应链风险整体可控",
        }

        logger.info("供应链深度分析完成: %s", result["summary"])
        return result

    # ── China+1 成本分析 ──────────────────────────────────────

    def analyze_china_plus_1(
        self,
        company_info: dict,
        financial_analysis: dict,
        geopolitical_analysis: dict,
        industry_cls: IndustryClass,
        sc_cfg: dict,
    ) -> Optional[dict]:
        """China+1 替代基地成本量化分析

        对适用行业，估计将产能从中国转移至替代基地的净成本/收益。
        """
        if not sc_cfg.get("china_plus_1_relevance"):
            return None

        # 从关税敏感性结果获取中国敞口
        tariff = geopolitical_analysis.get("tariff_sensitivity", {})
        china_exposure = tariff.get("china_exposure")
        if china_exposure is None or china_exposure <= 0:
            # 尝试从 business desc 或 market 判断
            company_name = company_info.get("name", "")
            summary = (company_info.get("summary") or "").lower()
            if not any(kw in summary for kw in ["china", "asia", "supply chain"]):
                return None
            china_exposure = 0.20  # 有业务描述线索但无数据时的保守估计

        # 获取毛利率
        ratios = financial_analysis.get("ratios", {})
        gm_series = ratios.get("GROSS_MARGIN", [])
        gross_margin = gm_series[-1]["value"] if gm_series and gm_series[-1]["value"] is not None else None

        # 对各替代基地计算净成本影响
        bases = []
        for base_name, base_data in CHINA_PLUS_1_COST_BASES.items():
            # 关税节省：转移到该基地后节省的关税
            tariff_benefit = base_data["tariff_saving"] * china_exposure

            # 效率/良率损失占 COGS 的比例
            efficiency_cost = abs(base_data["efficiency_loss"]) * china_exposure

            # 人工成本节省（人工占 COGS 的 25%）
            labor_benefit = base_data["labor_cost_advantage"] * _LABOR_SHARE_OF_COGS * china_exposure

            # 物流成本差异
            logistics_cost = abs(base_data["logistics_cost_premium"]) * china_exposure \
                if base_data["logistics_cost_premium"] < 0 else 0
            logistics_benefit = base_data["logistics_cost_premium"] * china_exposure \
                if base_data["logistics_cost_premium"] > 0 else 0

            # 净毛利率影响（bp）：转为基点便于阅读
            net_gm_impact = (tariff_benefit + labor_benefit + logistics_benefit - efficiency_cost - logistics_cost)
            impact_bp = round(net_gm_impact * 10000, 0) if gross_margin else None

            bases.append({
                "name": base_name,
                "china_exposure_shiftable": round(china_exposure, 3),
                "tariff_benefit_bp": round(tariff_benefit * 10000, 0),
                "efficiency_cost_bp": round(-efficiency_cost * 10000, 0),
                "labor_benefit_bp": round(labor_benefit * 10000, 0),
                "logistics_impact_bp": round((logistics_benefit - logistics_cost) * 10000, 0),
                "net_gm_impact_bp": impact_bp,
                "transition_time_years": base_data["transition_time_years"],
                "description": base_data["description"],
            })

        # 按净影响排序
        bases.sort(key=lambda b: -(b["net_gm_impact_bp"] or 0))

        # 最佳基地
        best_base = max(bases, key=lambda b: b["net_gm_impact_bp"] or -9999) if bases else None

        return {
            "applicable": True,
            "china_exposure": round(china_exposure, 3),
            "gross_margin": round(gross_margin, 4) if gross_margin else None,
            "feasible_bases": bases,
            "best_base": best_base["name"] if best_base else None,
            "best_base_net_impact_bp": best_base["net_gm_impact_bp"] if best_base else None,
            "note": (
                "以上估算基于行业平均参考值，实际影响取决于具体产品类别、"
                "自动化程度和供应链复杂度。建议参考公司年报中的区域分部数据。"
            ),
        }

    # ── 供应链中断情景分析 ────────────────────────────────────

    def analyze_disruption_scenarios(
        self,
        company_info: dict,
        financial_analysis: dict,
        geopolitical_analysis: dict,
        industry_cls: IndustryClass,
        sc_cfg: dict,
    ) -> Optional[dict]:
        """供应链中断情景量化矩阵"""
        if not sc_cfg.get("has_global_supply_chain"):
            return None

        # 三个输入缺失时会退回硬编码常数。这些常数会一路算成「毛利率影响
        # -XXbp」，看上去与真实测算无异，所以必须逐项登记，让下游知道某个
        # 数字是建立在假设上的。
        assumptions: list[dict] = []

        def _assume(field: str, value: float, why: str) -> float:
            assumptions.append({"field": field, "value": value, "reason": why})
            return value

        # 获取中国敞口
        tariff = geopolitical_analysis.get("tariff_sensitivity", {})
        china_exposure = tariff.get("china_exposure", 0.0) or 0.0
        if china_exposure <= 0:
            china_exposure = _assume(
                "china_exposure", 0.15, "地缘政治分析未给出中国营收敞口，取保守假设")

        # 获取净利润相关数据
        raw_metrics = financial_analysis.get("raw_metrics", {})
        ni_series = raw_metrics.get("NET_INCOME", [])
        net_income = ni_series[-1]["value"] if ni_series and ni_series[-1]["value"] is not None else None

        rev_series = raw_metrics.get("REVENUE", [])
        revenue = rev_series[-1]["value"] if rev_series and rev_series[-1]["value"] is not None else None

        # 获取毛利率
        ratios = financial_analysis.get("ratios", {})
        gm_series = ratios.get("GROSS_MARGIN", [])
        gross_margin = gm_series[-1]["value"] if gm_series and gm_series[-1]["value"] is not None else None
        if gross_margin is None:
            gross_margin = _assume(
                "gross_margin", 0.35, "财报未给出毛利率，取制造业常见假设")

        net_margin = net_income / revenue if net_income and revenue else None
        if net_margin is None:
            net_margin = _assume(
                "net_margin", 0.15, "缺净利润或营收，取常见净利率假设")

        scenarios_out = []
        for sc in SUPPLY_CHAIN_SCENARIOS:
            # 毛利率影响：敞口 × 关税冲击 × 情景系数
            gm_impact = china_exposure * 0.25 * sc["gm_coefficient"]  # 0.25 = 当前关税税率

            # 营收影响：敞口 × 情景营收系数
            rev_impact = china_exposure * sc["revenue_impact_coefficient"]

            # EPS 影响近似：营收影响 × 净利率 + 直接成本影响
            # 简化：毛利率影响 × (1) + 营收下滑对固定成本的放大
            eps_impact = gm_impact + (rev_impact * net_margin * 1.5)  # 1.5 = DOL 估计

            scenarios_out.append({
                "name": sc["name"],
                "probability": sc["probability"],
                "description": sc["description"],
                "gm_impact_bp": round(gm_impact * 10000, 0),
                "revenue_impact_pct": round(-rev_impact * 100, 1),
                "eps_impact_pct": round(-eps_impact * 100, 1),
            })

        # 概率加权预期影响
        expected_gm = sum(s["gm_impact_bp"] * s["probability"] for s in scenarios_out)
        expected_eps = sum(s["eps_impact_pct"] * s["probability"] for s in scenarios_out)

        result = {
            "applicable": True,
            "china_exposure": round(china_exposure, 3),
            "scenarios": scenarios_out,
            "expected_gm_impact_bp": round(expected_gm, 0),
            "expected_eps_impact_pct": round(expected_eps, 1),
            "base_gross_margin": round(gross_margin, 4),
            "base_net_margin": round(net_margin, 4),
            "note": "概率加权预期损失反映供应链中断的统计学期望值，实际结果可能因具体事件发展而偏离。",
        }
        if assumptions:
            result["degraded"] = True
            result["assumptions"] = assumptions
            result["reason"] = "以下输入缺失，改用假设值：" + "；".join(
                f"{a['field']}={a['value']}（{a['reason']}）" for a in assumptions)
        return result

    # ── 华为竞争/供应链影响评估 ───────────────────────────────

    def analyze_huawei_impact(
        self,
        company_info: dict,
        industry_cls: IndustryClass,
    ) -> dict:
        """华为竞争及供应链影响评估

        仅对 TECH_HARDWARE 和 TELECOM 行业执行。
        评估华为的技术自给战略、出口管制应对及其对竞争格局的影响。
        """
        country = (company_info.get("country") or "").upper()
        company_name = company_info.get("name", "")

        # 仅对相关行业中的非中国公司执行
        if industry_cls not in _HUAWEI_RELEVANT_INDUSTRIES:
            return {"is_relevant": False, "note": "所处行业与华为业务重叠度低"}

        is_chinese_company = country in ("CN", "CHN")
        if is_chinese_company:
            return {"is_relevant": False, "note": "中国公司，华为不构成外部竞争分析的主要因素"}

        # 判断公司与华为的竞争重叠度
        industry_name = "半导体/芯片" if industry_cls == IndustryClass.TECH_HARDWARE else "电信设备"
        summary = (company_info.get("summary") or "").lower()
        overlap_keywords = {
            "semiconductor", "chip", "5g", "telecom", "network", "基站",
            "手机", "smartphone", "iot", "cloud", "radio", "base station",
        }
        overlap_score = sum(1 for kw in overlap_keywords if kw in summary)

        if overlap_score < 2:
            return {"is_relevant": False, "note": f"业务与华为在{industry_name}领域的重叠度较低"}

        # 结构化评估
        assessment = self._assess_huawei_impact_detail(industry_cls, overlap_score)

        return {
            "is_relevant": True,
            "company_name": company_name,
            "industry": industry_name,
            "overlap_score": overlap_score,
            "assessment": assessment,
        }

    def _assess_huawei_impact_detail(
        self, industry_cls: IndustryClass, overlap_score: int
    ) -> dict:
        """生成华为影响的结构化评估"""
        assessment = {
            "competitive_impact": "华为在被制裁后加大自主研发投入，在多个领域从客户转为竞争对手。"
        }

        if industry_cls == IndustryClass.TECH_HARDWARE:
            assessment.update({
                "semiconductor_impact": (
                    "华为通过海思（HiSilicon）持续投入芯片设计，虽受先进制程限制，"
                    "但在成熟制程（28nm+）领域实现自给，对美系芯片公司的替代效应逐步显现。"
                ),
                "supply_chain_implication": (
                    "华为去美化供应链的推进，加速了中国本土半导体产业链的国产替代进程，"
                    "中长期可能削弱美系半导体公司在全球市场的定价能力。"
                ),
                "revenue_risk": (
                    "华为在智能手机和基站领域的回归，可能在价格和市场份额层面对美系/台系公司形成压力。"
                ),
            })
        else:  # TELECOM
            assessment.update({
                "semiconductor_impact": (
                    "华为在 5G 基站设备领域仍保持全球竞争力，但受限于射频芯片和 FPGA 的供应。"
                ),
                "supply_chain_implication": (
                    "华为作为全球最大电信设备商，其供应链选择直接影响上游元器件厂商的订单分配。"
                ),
                "revenue_risk": (
                    "华为在欧洲、东南亚等市场的 5G 设备份额变化是该行业竞争格局的核心变量。"
                ),
            })

        return assessment

    # ── 供应商集中度评估 ──────────────────────────────────────

    def assess_supplier_concentration(
        self,
        market_analysis: dict,
        industry_cls: IndustryClass,
        sc_cfg: dict,
    ) -> dict:
        """供应商集中度评估

        通过营收地域分布推断供应商集中度。
        若营收高度集中于单一市场，暗示该市场也是主要供应链节点。
        """
        markets_data = market_analysis.get("markets", {})
        global_market = markets_data.get("global", {})
        company_data = global_market.get("company", {})

        # 从 company info 中尝试获取供应商信息（数据源可能不提供）
        concentration_rating = sc_cfg.get("supplier_concentration_risk", "中")

        result = {
            "rating": concentration_rating,
            "description": self._concentration_description(concentration_rating, industry_cls),
        }

        return result

    def _concentration_description(
        self, rating: str, industry_cls: IndustryClass
    ) -> str:
        """供应商集中度的描述文本"""
        descriptions = {
            "高": (
                f"该行业供应链集中度较高，关键零部件/原材料供应可能依赖少数供应商，"
                f"单一供应商中断将产生显著影响。建议持续跟踪供应商多元化进展。"
            ),
            "中": (
                f"该行业供应链集中度适中，有一定替代供应商选择，"
                f"但仍需关注主要供应商的经营和地缘风险。"
            ),
            "低": (
                f"该行业供应链集中度较低，供应商选择丰富，"
                f"单一供应商变更对整体运营的影响有限。"
            ),
        }
        return descriptions.get(rating, descriptions["中"])
