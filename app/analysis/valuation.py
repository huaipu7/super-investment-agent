"""DCF 估值模型 + 相对估值

提供确定性（无 LLM）的 DCF 估值和行业相对估值计算。
数据不足时优雅降级，返回 None。
"""

from typing import Optional

from app.utils.logger import logger
from app.analysis.industry import IndustryClass, get_avg_multiples
from app.analysis.estimates import FALLBACK_RF, make_estimate

# 各行业股权风险溢价（ERP）估计
INDUSTRY_ERP: dict[IndustryClass, float] = {
    IndustryClass.TECH_HARDWARE: 0.055,
    IndustryClass.TECH_SOFTWARE: 0.06,
    IndustryClass.FINANCIAL: 0.045,
    IndustryClass.ENERGY: 0.05,
    IndustryClass.HEALTHCARE: 0.05,
    IndustryClass.CONSUMER_STAPLES: 0.04,
    IndustryClass.INDUSTRIAL: 0.05,
    IndustryClass.UTILITIES: 0.035,
    IndustryClass.TELECOM: 0.04,
    IndustryClass.REAL_ESTATE: 0.05,
    IndustryClass.UNKNOWN: 0.05,
}

# 各行业债务成本估计
INDUSTRY_COST_OF_DEBT: dict[IndustryClass, float] = {
    IndustryClass.TECH_HARDWARE: 0.035,
    IndustryClass.TECH_SOFTWARE: 0.04,
    IndustryClass.FINANCIAL: 0.03,
    IndustryClass.ENERGY: 0.045,
    IndustryClass.HEALTHCARE: 0.035,
    IndustryClass.CONSUMER_STAPLES: 0.03,
    IndustryClass.INDUSTRIAL: 0.04,
    IndustryClass.UTILITIES: 0.035,
    IndustryClass.TELECOM: 0.035,
    IndustryClass.REAL_ESTATE: 0.045,
    IndustryClass.UNKNOWN: 0.04,
}

# 各行业目标债务占比（D/(D+E)），用于 WACC 计算
INDUSTRY_TARGET_DEBT_RATIO: dict[IndustryClass, float] = {
    IndustryClass.TECH_HARDWARE: 0.20,
    IndustryClass.TECH_SOFTWARE: 0.15,
    IndustryClass.FINANCIAL: 0.50,
    IndustryClass.ENERGY: 0.30,
    IndustryClass.HEALTHCARE: 0.25,
    IndustryClass.CONSUMER_STAPLES: 0.30,
    IndustryClass.INDUSTRIAL: 0.30,
    IndustryClass.UTILITIES: 0.40,
    IndustryClass.TELECOM: 0.35,
    IndustryClass.REAL_ESTATE: 0.40,
    IndustryClass.UNKNOWN: 0.25,
}

# 各行业默认 beta（相对市场）
INDUSTRY_DEFAULT_BETA: dict[IndustryClass, float] = {
    IndustryClass.TECH_HARDWARE: 1.30,
    IndustryClass.TECH_SOFTWARE: 1.20,
    IndustryClass.FINANCIAL: 1.10,
    IndustryClass.ENERGY: 1.15,
    IndustryClass.HEALTHCARE: 0.95,
    IndustryClass.CONSUMER_STAPLES: 0.65,
    IndustryClass.INDUSTRIAL: 1.05,
    IndustryClass.UTILITIES: 0.60,
    IndustryClass.TELECOM: 0.75,
    IndustryClass.REAL_ESTATE: 0.85,
    IndustryClass.UNKNOWN: 1.00,
}

# 默认企业税率
CORPORATE_TAX_RATE = 0.21

# 终端增长率默认为名义 GDP 长期增速
DEFAULT_TERMINAL_GROWTH = 0.03

# 显式预测期年数
DEFAULT_PROJECTION_YEARS = 5

# WACC 与永续增长率的最小差距。低于 2pp 时分母 (WACC − g) 趋近于零，终端价值被
# 放大成数学产物而非经济判断——曾出现 WACC 3.17% / g 3% → 内在价值为现价 51 倍。
MIN_WACC_GROWTH_SPREAD = 0.02

# 每股内在价值 / 现价 的可接受倍数区间。越界说明参数或输入失真（ERP 触底、
# WACC 偏离、FCF 口径错误），结果不予采用，显式降级并说明原因。
FAIR_VALUE_PRICE_BAND = (0.3, 3.0)


def band_degrade_reason(fair_value: float, price: float, ratio: float) -> str:
    """每股价值与现价背离过远时的降级说明（分析与增量刷新共用同一措辞）。"""
    lo, hi = FAIR_VALUE_PRICE_BAND
    return (f"DCF 每股价值 {fair_value:,.2f} 为现价 {price:,.2f} 的 {ratio:.2f} 倍，"
            f"超出合理区间 [{lo}, {hi}]，参数或输入失真，结果不予采用")


def build_valuation_summary(
    dcf: Optional[dict],
    relative_valuation: Optional[dict],
    current_price: Optional[float],
    supply_chain_premium: float = 0.0,
) -> str:
    """估值摘要文本。

    全量分析与增量刷新共用同一口径：刷新时只换了价格和空间，摘要必须跟着重算，
    否则报告正文写着旧空间、字段里是新数字。
    """
    summary_parts = []
    dcf = dcf if isinstance(dcf, dict) else {}
    if dcf.get("fair_value") is not None:
        fv = dcf["fair_value"]
        upside = dcf.get("upside")
        if upside is not None:
            label = "高估" if upside < -0.1 else "低估" if upside > 0.15 else "合理"
            summary_parts.append(
                f"DCF 内在价值 ${fv:.2f}，"
                f"当前价格 {'$' + str(current_price) if current_price else 'N/A'}，"
                f"{label}（空间 {upside:+.1%}）")
        else:
            summary_parts.append(f"DCF 内在价值 ${fv:.2f}")
    elif dcf.get("reason"):
        summary_parts.append(f"DCF 不可用：{dcf['reason']}")
    else:
        summary_parts.append("DCF 不适用（FCF 不足或为负）")

    rel = relative_valuation if isinstance(relative_valuation, dict) else {}
    premiums = [
        f"{mk.upper()} {entry['premium']:+.1%}"
        for mk, entry in rel.items()
        if isinstance(entry, dict) and entry.get("premium") is not None
    ]
    if premiums:
        summary_parts.append("相对估值: " + "，".join(premiums))

    summary = "；".join(summary_parts)
    if supply_chain_premium > 0:
        summary += f"；供应链风险溢价 {supply_chain_premium * 100:.2f}%"
    return summary


class DCFModel:
    """DCF 估值模型"""

    def compute_wacc(
        self,
        risk_free_rate: float,
        industry_cls: IndustryClass,
        beta: Optional[float] = None,
        debt_ratio: Optional[float] = None,
        erp: Optional[float] = None,
    ) -> float:
        """计算加权平均资本成本

        WACC = E/(D+E) × Ke + D/(D+E) × Kd × (1-T)

        Ke = Rf + β × ERP
        Kd = 行业债务成本
        """
        erp_v = erp if erp is not None else INDUSTRY_ERP.get(industry_cls, 0.05)
        b = beta if beta is not None else INDUSTRY_DEFAULT_BETA.get(industry_cls, 1.0)
        ke = risk_free_rate + b * erp_v

        kd = INDUSTRY_COST_OF_DEBT.get(industry_cls, 0.04)
        wd = debt_ratio if debt_ratio is not None else INDUSTRY_TARGET_DEBT_RATIO.get(industry_cls, 0.25)
        we = 1.0 - wd

        wacc = we * ke + wd * kd * (1 - CORPORATE_TAX_RATE)
        return wacc

    def compute_dcf(
        self,
        fcf_history: list[float],
        wacc: float,
        projected_growth: Optional[float] = None,
        terminal_growth: float = DEFAULT_TERMINAL_GROWTH,
        years: int = DEFAULT_PROJECTION_YEARS,
    ) -> dict:
        """执行 DCF 估值

        Args:
            fcf_history: 历史自由现金流序列（最近一期为最后）
            wacc: 加权平均资本成本
            projected_growth: 预测期增长率（None 时取历史均值）
            terminal_growth: 终端增长率
            years: 显式预测期年数

        Returns:
            {
                "fair_value": float | None,       # 企业内在价值（股权价值）
                "upside": float | None,            # 上涨/下跌空间
                "projected_fcf": list[float],      # 预测期 FCF
                "terminal_value": float | None,    # 终端价值
                "pv_terminal": float | None,       # 终端价值现值
                "pv_projections": float | None,    # 预测期现值之和
                "wacc_used": float,
                "growth_assumption": float,
                "terminal_growth": float,
            }
        """
        if not fcf_history or len(fcf_history) < 2:
            return {"fair_value": None, "upside": None, "reason": "历史 FCF 数据不足（需要至少 2 期）"}

        if wacc <= 0 or wacc >= 1:
            return {"fair_value": None, "upside": None, "reason": f"WACC ({wacc:.2%}) 超出合理范围"}

        # 分母保护：WACC 逼近永续增长率时终端价值趋于无穷，结果不是估值而是噪声
        spread = wacc - terminal_growth
        if spread < MIN_WACC_GROWTH_SPREAD:
            return {
                "fair_value": None,
                "upside": None,
                "degraded": True,
                "wacc_used": wacc,
                "terminal_growth": terminal_growth,
                "reason": (f"WACC {wacc:.2%} 与永续增长率 {terminal_growth:.2%} "
                           f"仅相差 {spread * 100:.2f}pp，低于 "
                           f"{MIN_WACC_GROWTH_SPREAD * 100:.0f}pp 下限，"
                           f"终端价值分母趋零、结果无经济意义"),
            }

        latest_fcf = fcf_history[-1]
        if latest_fcf <= 0:
            return {"fair_value": None, "upside": None, "reason": "最新 FCF 为负值，DCF 不适用"}

        # 估算增长率：取历史均值，但限制在合理范围
        if projected_growth is not None:
            growth = projected_growth
        else:
            if len(fcf_history) >= 2:
                growth_rates_v = []
                for i in range(1, len(fcf_history)):
                    prev_v = fcf_history[i - 1]
                    if prev_v > 0:
                        growth_rates_v.append((fcf_history[i] - prev_v) / abs(prev_v))
                growth = sum(growth_rates_v) / len(growth_rates_v) if growth_rates_v else 0.05
            else:
                growth = 0.05

        # 增长率上下限
        growth = max(-0.10, min(growth, 0.25))

        # 计算预测期 FCF
        projected_fcf = []
        fcf_t = latest_fcf
        for y in range(1, years + 1):
            fcf_t = fcf_t * (1 + growth)
            projected_fcf.append(fcf_t)

        # 计算终端价值（永续增长法）
        terminal_fcf = projected_fcf[-1] * (1 + terminal_growth)
        terminal_value = terminal_fcf / (wacc - terminal_growth)
        pv_terminal = terminal_value / ((1 + wacc) ** years)

        # 计算预测期现值
        pv_projections = 0.0
        for y, fcf in enumerate(projected_fcf, 1):
            pv_projections += fcf / ((1 + wacc) ** y)

        enterprise_value = pv_projections + pv_terminal

        return {
            "fair_value_ev": enterprise_value,
            "projected_fcf": projected_fcf,
            "terminal_value": terminal_value,
            "pv_terminal": pv_terminal,
            "pv_projections": pv_projections,
            "wacc_used": wacc,
            "growth_assumption": growth,
            "terminal_growth": terminal_growth,
            "projection_years": years,
        }

    def compute_sensitivity(
        self,
        fcf_history: list[float],
        base_wacc: float,
        base_terminal_growth: float = DEFAULT_TERMINAL_GROWTH,
        shares: Optional[float] = None,
        net_debt: float = 0.0,
        wacc_spread: float = 0.02,
        growth_spread: float = 0.015,
        steps: int = 5,
        years: int = DEFAULT_PROJECTION_YEARS,
    ) -> dict:
        """WACC × 永续增长率 → 每股内在价值矩阵（DCF 敏感性热力图数据）。

        Args:
            fcf_history: 历史自由现金流序列（最近一期为最后）
            base_wacc: 基准 WACC（矩阵中心）
            base_terminal_growth: 基准永续增长率（矩阵中心）
            shares: 总股本（换算每股）
            net_debt: 净负债
            wacc_spread: WACC 扫描半幅（±2pp）
            growth_spread: 永续增长率扫描半幅（±1.5pp）
            steps: 每轴扫描格点数（5 → 5×5）

        Returns:
            {
                "wacc_values": [...], "growth_values": [...],
                "matrix": [[每股价值,...], ...],   # 行=WACC，列=永续g
                "base_fair_value": float|None,
                "base_wacc": float, "base_terminal_growth": float,
                "note": str,
            }
        """
        lo = (steps - 1) / 2
        wacc_vals = [base_wacc + wacc_spread * (i - lo) for i in range(steps)]
        g_vals = [base_terminal_growth + growth_spread * (i - lo) for i in range(steps)]
        # WACC 需在 (0,1) 且与永续增长率拉开下限差距，否则 compute_dcf 拒绝
        wacc_vals = [max(0.01, min(0.40, w)) for w in wacc_vals]
        g_vals = [max(-0.01, min(w - MIN_WACC_GROWTH_SPREAD, g))
                  for g, w in zip(g_vals, wacc_vals)]

        matrix: list[list[float | None]] = []
        base_fv: float | None = None
        for w in wacc_vals:
            row: list[float | None] = []
            for g in g_vals:
                dcf = self.compute_dcf(fcf_history, w, terminal_growth=g, years=years)
                ev = dcf.get("fair_value_ev")
                if not ev:
                    row.append(None)
                    continue
                eq = ev - net_debt
                if shares and shares > 0 and eq > 0:
                    fv = eq / shares
                    if abs(w - base_wacc) < 1e-9 and abs(g - base_terminal_growth) < 1e-9:
                        base_fv = fv
                    row.append(round(fv, 2))
                else:
                    row.append(None)
            matrix.append(row)

        return {
            "wacc_values": wacc_vals,
            "growth_values": g_vals,
            "matrix": matrix,
            "base_fair_value": base_fv,
            "base_wacc": base_wacc,
            "base_terminal_growth": base_terminal_growth,
            "note": "热力图：行=WACC，列=永续增长率 g；中心点=当前参数，"
                    "四周=±2pp / ±1.5pp 敏感性。缺失/负值单元格为 None（DCF 不适用）。",
        }

    def compute_relative_valuation(
        self,
        ratios: dict,
        industry_cls: IndustryClass,
    ) -> dict:
        """计算相对估值（公司估值指标 vs 行业均值）

        Args:
            ratios: financial_analyzer 输出的 ratios dict
            industry_cls: 行业分类

        Returns:
            {
                "pe": {"company": float, "industry_avg": float, "premium": float | None},
                "pb": ...,
                "ps": ...,
                "ev_ebitda": ...,
            }
        """
        avg_multiples = get_avg_multiples(industry_cls)
        result = {}

        for metric_key in ["pe", "pb", "ps", "ev_ebitda"]:
            code = metric_key.upper()
            series = ratios.get(code, [])
            company_val = series[-1]["value"] if series and series[-1]["value"] is not None else None
            industry_val = avg_multiples.get(metric_key)

            entry = {
                "company": company_val,
                "industry_avg": industry_val,
            }

            if company_val is not None and company_val > 0 and industry_val is not None and industry_val > 0:
                premium = (company_val - industry_val) / industry_val
                entry["premium"] = premium
            else:
                entry["premium"] = None

            result[metric_key] = entry

        return result

    def compute_peg(
        self,
        pe: Optional[float],
        net_income_growth: Optional[float],
    ) -> dict:
        """计算 PEG 比率

        PEG = PE / (净利润增长率 × 100)

        Args:
            pe: 市盈率（正值）
            net_income_growth: 净利润增长率（小数，如 0.15 = 15%）

        Returns:
            {"peg": float | None, "judgment": str, "note": str}
        """
        if pe is None or pe <= 0 or net_income_growth is None or net_income_growth <= 0:
            return {
                "peg": None,
                "judgment": "无法判断",
                "note": "需要正值 PE 和净利润增长率数据",
            }

        peg = pe / (net_income_growth * 100)

        if peg < 1.0:
            judgment = "低估"
            note = f"PEG {peg:.2f} < 1，市场对增长的定价偏低"
        elif peg < 2.0:
            judgment = "合理"
            note = f"PEG {peg:.2f}，处于合理估值区间（1 ~ 2）"
        else:
            judgment = "高估"
            note = f"PEG {peg:.2f} > 2，增长预期已被充分定价"

        return {"peg": round(peg, 2), "judgment": judgment, "note": note}

    def analyze(
        self,
        ratios: dict,
        growth_rates: dict,
        raw_metrics: dict,
        company_info: dict,
        industry_cls: IndustryClass,
        current_price: Optional[float] = None,
        supply_chain_result: Optional[dict] = None,
        estimates: Optional[dict] = None,
        peer_pe: Optional[dict] = None,
    ) -> dict:
        """DCF + 相对估值全分析

        Args:
            ratios: financial_analyzer 输出的 ratios dict
            growth_rates: financial_analyzer 输出的 growth_rates dict
            raw_metrics: financial_analyzer 输出的 raw_metrics dict
            company_info: 公司信息（含 shares_outstanding 等）
            industry_cls: 行业分类
            current_price: 当前股价
            supply_chain_result: 可选供应链分析结果（用于风险调整）
            estimates: 可选估算参数字典，形如 {"risk_free_rate": {...},
                       "beta": {...}, "erp": {...}}（estimates.py 输出，
                       缺字段时回退行业静态常量并标注）
            peer_pe: 可选竞品集合真实 PE 估算（estimates.estimate_peer_pe 输出）

        Returns:
            {
                "dcf": {...} | None,
                "relative_valuation": {...} | None,
                "supply_chain_risk_premium": float,
                "estimates": {...},      # 估算参数来源与置信度（不静默编造）
                "summary": str,
            }
        """
        result = {
            "dcf": None,
            "relative_valuation": None,
            "supply_chain_risk_premium": 0.0,
            "estimates": {},
            "summary": "",
        }

        # ── 0. 供应链风险调整系数 ──
        sc_premium = self._compute_supply_chain_risk_premium(supply_chain_result)

        # ── 0.5 估算参数（Rf / Beta / ERP）——缺省显式标注，不静默 ──
        est = estimates or {}
        rf_est = est.get("risk_free_rate")
        beta_est = est.get("beta")
        erp_est = est.get("erp")
        rfr = rf_est.get("value") if isinstance(rf_est, dict) else None
        if rfr is None:
            from app.analysis.estimates import get_risk_free_rate
            rf_est = get_risk_free_rate(market="us")
            rfr = rf_est.get("value")
            if rfr is None:
                rfr = FALLBACK_RF
        beta = beta_est.get("value") if isinstance(beta_est, dict) else None
        erp_v = erp_est.get("value") if isinstance(erp_est, dict) else None
        result["estimates"] = {
            "risk_free_rate": rf_est,
            "beta": beta_est if beta_est else make_estimate(
                INDUSTRY_DEFAULT_BETA.get(industry_cls, 1.0),
                "行业静态默认 beta", "medium"),
            "erp": erp_est if erp_est else make_estimate(
                INDUSTRY_ERP.get(industry_cls, 0.05), "行业静态 ERP 常量", "medium"),
            "cost_of_debt": make_estimate(
                INDUSTRY_COST_OF_DEBT.get(industry_cls, 0.04),
                "行业静态债务成本", "medium"),
            "debt_ratio": make_estimate(
                INDUSTRY_TARGET_DEBT_RATIO.get(industry_cls, 0.25),
                "行业静态目标负债率", "medium"),
        }

        # ── 1. DCF 估值 ──
        try:
            # 提取历史 FCF 序列
            fcf_series = ratios.get("FREE_CASH_FLOW", [])
            fcf_history = [s["value"] for s in fcf_series if s["value"] is not None]

            if len(fcf_history) >= 2 and fcf_history[-1] > 0:
                wacc = self.compute_wacc(rfr, industry_cls, beta=beta, erp=erp_v)

                # 供应链风险溢价叠加
                if sc_premium > 0:
                    adjusted_rfr = rfr + sc_premium
                    wacc = self.compute_wacc(adjusted_rfr, industry_cls,
                                             beta=beta, erp=erp_v)
                    logger.info("供应链风险调整: +%.0fbp ERP, 调整后 WACC = %.2f%%",
                                sc_premium * 10000, wacc * 100)

                dcf_result = self.compute_dcf(fcf_history, wacc)

                if dcf_result.get("fair_value_ev"):
                    # 计算每股内在价值
                    shares = company_info.get("shares_outstanding")
                    net_debt = raw_metrics.get("NET_DEBT", [])
                    nd_val = net_debt[-1]["value"] if net_debt and net_debt[-1]["value"] is not None else 0

                    ev = dcf_result["fair_value_ev"]
                    equity_value = ev - nd_val

                    if shares and shares > 0 and equity_value > 0:
                        fair_value_per_share = equity_value / shares
                        lo, hi = FAIR_VALUE_PRICE_BAND
                        ratio = (fair_value_per_share / current_price
                                 if current_price and current_price > 0 else None)
                        if ratio is not None and not (lo <= ratio <= hi):
                            # 与现价偏离超过一个量级：模型参数或输入已失真，结果不予采用
                            dcf_result["fair_value"] = None
                            dcf_result["upside"] = None
                            dcf_result["degraded"] = True
                            dcf_result["reason"] = band_degrade_reason(
                                fair_value_per_share, current_price, ratio)
                            logger.warning("%s", dcf_result["reason"])
                        else:
                            dcf_result["fair_value"] = fair_value_per_share
                            if current_price and current_price > 0:
                                dcf_result["upside"] = (fair_value_per_share - current_price) / current_price
                            else:
                                dcf_result["upside"] = None
                            # DCF 敏感性矩阵（WACC × 永续g → 每股价值，热力图数据）
                            try:
                                dcf_result["sensitivity"] = self.compute_sensitivity(
                                    fcf_history, wacc,
                                    base_terminal_growth=dcf_result.get(
                                        "terminal_growth", DEFAULT_TERMINAL_GROWTH),
                                    shares=shares, net_debt=nd_val)
                            except Exception:
                                pass
                    else:
                        dcf_result["fair_value"] = None
                        dcf_result["upside"] = None
                        dcf_result["reason_extra"] = "无法计算每股价值（缺少股数或 EV 为负）"

                result["dcf"] = dcf_result
            else:
                reason = []
                if len(fcf_history) < 2:
                    reason.append("FCF 历史数据不足 2 期")
                if fcf_history and fcf_history[-1] <= 0:
                    reason.append(f"最新 FCF ({fcf_history[-1]:.0f}) 为负值")
                result["dcf"] = {"fair_value": None, "upside": None,
                                 "reason": "；".join(reason) if reason else "FCF 数据不足"}
        except Exception as e:
            logger.warning("DCF 估值计算失败: %s", e)
            result["dcf"] = {"fair_value": None, "upside": None, "reason": f"计算异常: {e}"}

        # ── 2. 相对估值 ──
        try:
            result["relative_valuation"] = self.compute_relative_valuation(ratios, industry_cls)
        except Exception as e:
            logger.warning("相对估值计算失败: %s", e)
            result["relative_valuation"] = {}

        # 竞品集合真实 PE 覆盖行业均值（免费源真实数据，n≥3 才 high 置信）
        if peer_pe and peer_pe.get("value"):
            rel = result.get("relative_valuation") or {}
            pe = rel.get("pe") if isinstance(rel.get("pe"), dict) else None
            if pe is not None:
                pe["industry_avg"] = peer_pe["value"]
                pe["industry_avg_source"] = peer_pe.get("source")
                c = pe.get("company")
                if c and c > 0:
                    pe["premium"] = (c - peer_pe["value"]) / peer_pe["value"]
            result["estimates"]["industry_pe"] = peer_pe

        # ── 3. 文字摘要 ──
        result["summary"] = build_valuation_summary(
            result.get("dcf", {}), result.get("relative_valuation", {}),
            current_price, sc_premium)
        result["supply_chain_risk_premium"] = round(sc_premium, 6)
        logger.info("估值分析完成: %s", result["summary"])
        return result

    @staticmethod
    def _compute_supply_chain_risk_premium(
        supply_chain_result: Optional[dict],
    ) -> float:
        """根据供应链分析结果计算 ERP 溢价"""
        if not supply_chain_result:
            return 0.0

        premium = 0.0

        scenarios = supply_chain_result.get("disruption_scenarios")
        if scenarios and isinstance(scenarios, dict):
            expected_eps = scenarios.get("expected_eps_impact_pct", 0) or 0
            if expected_eps > 10:
                premium = max(premium, 0.01)
            elif expected_eps > 5:
                premium = max(premium, 0.005)

        china_plus_1 = supply_chain_result.get("china_plus_1")
        if china_plus_1 and isinstance(china_plus_1, dict):
            best_net = china_plus_1.get("best_base_net_impact_bp")
            if best_net is not None and best_net < -50:
                premium = max(premium, 0.005)

        return premium


def recompute_dcf_from_result(
    result: dict,
    *,
    wacc: Optional[float] = None,
    terminal_growth: float = DEFAULT_TERMINAL_GROWTH,
    projection_years: int = DEFAULT_PROJECTION_YEARS,
    beta: Optional[float] = None,
    risk_free_rate: Optional[float] = None,
    erp: Optional[float] = None,
    current_price: Optional[float] = None,
) -> dict:
    """基于已缓存分析结果重算 DCF 每股内在价值（确定性，无 LLM，免费源内）。

    从 result 提取 fcf_history / 股本 / 净负债 / 当前价，按给定参数重算；
    参数缺失时回退分析时使用的估算值/基准值，不静默编造。供
    `POST /api/v1/analysis/recompute-dcf` 复用（看板参数滑块即时重算）。

    Args:
        result: AnalysisOrchestrator.run() 输出。
        wacc: 显式 WACC；None 时用 beta/rf/erp 覆写重算，再无则用 dcf.wacc_used。
        terminal_growth: 永续增长率。
        projection_years: 显式预测期年数。
        beta / risk_free_rate / erp: 显式重算 WACC 的组件；None 回退估算值。

    Returns:
        {"available": True, "fair_value", "upside", "wacc_used", "terminal_growth",
         "projection_years", "net_debt", "current_price", "sensitivity", "base_wacc"}
        或 {"available": False, "reason": "..."}
    """
    if not isinstance(result, dict):
        return {"available": False, "reason": "缺少分析结果"}
    fin = result.get("financial_analysis") or {}
    va = result.get("valuation_analysis") or {}
    ci = result.get("company_info") or {}

    fcf_history = [v for v in (fin.get("fcf_history") or [])
                   if isinstance(v, (int, float)) and v is not None]
    if len(fcf_history) < 2:
        return {"available": False, "reason": "FCF 历史数据不足（需要至少 2 期）"}
    if fcf_history[-1] <= 0:
        return {"available": False, "reason": "最新 FCF 为负值，DCF 不适用"}

    shares = ci.get("shares_outstanding")
    if not shares or float(shares) <= 0:
        return {"available": False, "reason": "缺少股本数据"}
    shares = float(shares)

    net_debt = 0.0
    nd_series = (fin.get("raw_metrics") or {}).get("NET_DEBT") or []
    for s in reversed(nd_series):
        if isinstance(s, dict) and s.get("value") is not None:
            net_debt = float(s["value"])
            break

    price = current_price
    if price is None:
        price = fin.get("current_price")

    # ── WACC 确定：显式 > beta/rf/erp 覆写 > 基准值 ──
    base_dcf = va.get("dcf") if isinstance(va.get("dcf"), dict) else {}
    if wacc is not None:
        wacc_v = float(wacc)
    else:
        est = va.get("estimates") or {}

        def _ev(key):
            e = est.get(key)
            return e.get("value") if isinstance(e, dict) else None

        rf = risk_free_rate if risk_free_rate is not None else _ev("risk_free_rate")
        b = beta if beta is not None else _ev("beta")
        erpv = erp if erp is not None else _ev("erp")
        if rf is None or b is None or erpv is None:
            base_wacc = base_dcf.get("wacc_used")
            if base_wacc is None:
                return {"available": False, "reason": "缺少 WACC 参数"}
            wacc_v = float(base_wacc)
        else:
            wd = _ev("debt_ratio")
            kd = _ev("cost_of_debt")
            wd = wd if wd is not None else 0.25
            kd = kd if kd is not None else 0.04
            ke = float(rf) + float(b) * float(erpv)
            wacc_v = (1.0 - wd) * ke + wd * kd * (1 - CORPORATE_TAX_RATE)

    dm = DCFModel()
    dcf = dm.compute_dcf(fcf_history, wacc_v, terminal_growth=terminal_growth,
                         years=projection_years)
    ev = dcf.get("fair_value_ev")
    if not ev:
        return {"available": False, "reason": dcf.get("reason", "DCF 计算失败")}
    equity = ev - net_debt
    if equity <= 0:
        return {"available": False, "reason": "企业价值小于净负债，每股价值不适用"}
    fair_value = equity / shares
    if price and price > 0:
        lo, hi = FAIR_VALUE_PRICE_BAND
        ratio = fair_value / price
        if not (lo <= ratio <= hi):
            return {"available": False,
                    "reason": (f"重算每股价值 {fair_value:,.2f} 为现价 {price:,.2f} 的 "
                               f"{ratio:.2f} 倍，超出合理区间 [{lo}, {hi}]，"
                               f"请调整 WACC / 永续增长率参数")}
    upside = (fair_value - price) / price if price and price > 0 else None
    sens = dm.compute_sensitivity(fcf_history, wacc_v,
                                  base_terminal_growth=terminal_growth,
                                  shares=shares, net_debt=net_debt,
                                  years=projection_years)
    return {
        "available": True,
        "fair_value": round(fair_value, 4),
        "upside": round(upside, 6) if upside is not None else None,
        "wacc_used": round(wacc_v, 6),
        "terminal_growth": round(terminal_growth, 6),
        "projection_years": projection_years,
        "net_debt": round(net_debt, 2),
        "current_price": price,
        "base_wacc": base_dcf.get("wacc_used"),
        "base_fair_value": base_dcf.get("fair_value"),
        "sensitivity": sens,
    }
