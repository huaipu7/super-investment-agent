"""财务比率分析和趋势分析

输入：清洗后的三张报表 DataFrames（index=日期, columns=标准化指标代码）
输出：结构化比率序列和趋势数据
"""

from typing import Optional

import numpy as np
import pandas as pd

from app.constants import FINANCIAL_RATIOS
from app.analysis.industry import IndustryClass
from app.utils.logger import logger


# ── 比率定义：代码 → (中文名, 分类, 计算方式) ─────────────────────
# 计算方式为 lambda(income, balance, cashflow, year) → float | None
# 三个 DataFrame 的 index 为年份，columns 为指标代码

RATIO_CATEGORIES = {
    "盈利能力": ["ROE", "ROA", "GROSS_MARGIN", "NET_MARGIN", "OPERATING_MARGIN", "RD_TO_REVENUE"],
    "偿债能力": ["CURRENT_RATIO", "QUICK_RATIO", "DEBT_TO_EQUITY"],
    "营运能力": ["ASSET_TURNOVER", "EQUITY_MULTIPLIER"],
    "市场估值": ["PE", "PB", "PS", "EV_EBITDA"],
    "成长能力": ["REVENUE_GROWTH", "NET_INCOME_GROWTH"],
    "现金流分析": ["OPERATING_CF_MARGIN", "FREE_CASH_FLOW", "FCF_PER_SHARE", "CAPEX_TO_REVENUE", "CASH_CONVERSION_RATIO"],
}


_Q_LABEL = {3: "Q1", 6: "Q2", 9: "Q3", 12: "Q4"}


def _f(v):
    """安全转 float；None/非数字 → None。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _yoy(cur, prev) -> Optional[float]:
    """同比：(cur-prev)/|prev|；prev≈0 或缺失 → None。"""
    if cur is None or prev is None or abs(prev) < 1e-12:
        return None
    return (cur - prev) / abs(prev)


def compute_quarterly_trend(income_q: pd.DataFrame) -> dict:
    """近 4 个报告期营收/净利趋势 + 同比（同报告期 vs 上年同期）。

    income_q: 季度利润表，index=报告期(DatetimeIndex)，columns 含 REVENUE/NET_INCOME。
    免费源口径说明：A 股东财季度利润表为「报告期累计值」（Q2=半年累计、Q4=全年），
    因此同比按同一报告日期对比上年同期，而非单季度差分。数值全部来自真实数据，
    缺失显式标注 None，不编造。

    Returns: {"available", "source", "reason", "periods": [{"period","report_date",
              "revenue","net_income","revenue_yoy","net_income_yoy"}, ...]}（升序）
    """
    if income_q is None or income_q.empty:
        return {"available": False, "reason": "缺季度利润表数据", "periods": []}
    cols = [c for c in ("REVENUE", "NET_INCOME") if c in income_q.columns]
    if not cols:
        return {"available": False, "reason": "季度利润表缺营收/净利字段", "periods": []}
    df = income_q[cols].apply(pd.to_numeric, errors="coerce")
    df = df.dropna(how="all").sort_index()
    if df.empty:
        return {"available": False, "reason": "季度数据为空", "periods": []}

    idx = pd.Index([pd.Timestamp(ts) if not isinstance(ts, pd.Timestamp) else ts
                    for ts in df.index])
    df = df.set_axis(idx)
    last4 = df.iloc[-4:]
    periods = []
    for ts, row in last4.iterrows():
        rev, ni = _f(row.get("REVENUE")), _f(row.get("NET_INCOME"))
        prev_ts = pd.Timestamp(year=ts.year - 1, month=ts.month, day=ts.day)
        m = df.index == prev_ts
        rev_yoy = ni_yoy = None
        if m.any():
            prev_row = df.loc[m].iloc[0]
            rev_yoy = _yoy(rev, _f(prev_row.get("REVENUE")))
            ni_yoy = _yoy(ni, _f(prev_row.get("NET_INCOME")))
        periods.append({
            "period": f"{ts.year}{_Q_LABEL.get(ts.month, 'Q?')}",
            "report_date": ts.strftime("%Y-%m-%d"),
            "revenue": rev,
            "net_income": ni,
            "revenue_yoy": round(rev_yoy, 4) if rev_yoy is not None else None,
            "net_income_yoy": round(ni_yoy, 4) if ni_yoy is not None else None,
        })
    periods.sort(key=lambda p: p["report_date"])  # 升序（旧→新），对倒序输入也安全
    return {
        "available": True,
        "source": "季度财报（报告期累计口径，同比=同报告期 vs 上年同期）",
        "periods": periods,
    }


class FinancialAnalyzer:
    """财务比率分析器"""

    def analyze(
        self,
        financials: dict[str, pd.DataFrame],
        company_info: Optional[dict] = None,
        price_data: Optional[pd.DataFrame] = None,
        industry_cls: Optional[IndustryClass] = None,
    ) -> dict:
        """执行完整财务分析

        Args:
            financials: {"income": df, "balance_sheet": df, "cash_flow": df}
            company_info: 公司信息（含 market_cap 等）
            price_data: 股价历史（index=日期, columns=[Close]）

        Returns:
            {
                "ratios": {ratio_code: [{"period": str, "value": float|None}, ...]},
                "growth_rates": {code: [...]},
                "periods": [str, ...],
                "summary": str,
                "metadata": {"company_name": str, "ticker": str, "period_count": int}
            }
        """
        income = financials.get("income", pd.DataFrame())
        balance = financials.get("balance_sheet", pd.DataFrame())
        cashflow = financials.get("cash_flow", pd.DataFrame())

        years = self._get_aligned_years(income, balance, cashflow)
        if not years:
            logger.warning("财务数据为空，无法分析")
            return self._empty_result()

        logger.info("财务分析: %d 个期间", len(years))

        # 按年对齐数据
        aligned = self._align_by_year(income, balance, cashflow, years)

        # 计算所有比率
        ratios = {}
        for code in FINANCIAL_RATIOS:
            series = self._compute_ratio(code, aligned, years, price_data, company_info)
            if series is not None:
                ratios[code] = series

        # 计算同比增长率
        growth_rates = self._compute_growth_rates(aligned, income, years)

        # 收集原始财务指标（供 DuPont 回购分析等使用）
        raw_metrics = {}
        for metric in ["NET_INCOME", "TOTAL_EQUITY", "TOTAL_ASSETS", "REVENUE",
                       "TAX_PROVISION", "INTEREST_EXPENSE", "STOCK_BUYBACK",
                       "OPERATING_INCOME", "EBITDA"]:
            series = []
            for year in years:
                val = self._get_val(aligned, year, metric)
                if val is not None:
                    series.append({"period": str(year), "value": val})
            if series:
                raw_metrics[metric] = series

        # ── DuPont 分解（ROE = 净利率 × 资产周转 × 权益乘数）──
        dupont_breakdown = self._compute_dupont(aligned, years)

        # ── ROIC（NOPAT / 经营性投入资本，近似，显式标注）──
        roic_series = self._compute_roic(aligned, years)

        # ── 估值历史分位（当前 PE 相对历史区间，近似，显式标注）──
        pe_percentile = self._compute_pe_percentile(aligned, years, price_data, company_info)

        # 提取历史自由现金流序列（供 DCF 模型使用）
        fcf_history = []
        fcf_series = ratios.get("FREE_CASH_FLOW", [])
        for s in fcf_series:
            if s["value"] is not None:
                fcf_history.append(s["value"])

        # 提取当前股价
        current_price = None
        if price_data is not None and not price_data.empty:
            close_col = "Close" if "Close" in price_data.columns else price_data.columns[0]
            current_price = float(price_data[close_col].iloc[-1])

        # Phase 6: 行业特有财务指标（NIM/NPL/NAV 等）
        industry_specific_ratios = {}
        if industry_cls is not None:
            industry_specific_ratios = self._compute_industry_specific_ratios(
                income, balance, cashflow, years, industry_cls
            )

        # 分类整理
        result = {
            "ratios": ratios,
            "growth_rates": growth_rates,
            "raw_metrics": raw_metrics,
            "fcf_history": fcf_history,
            "current_price": current_price,
            "industry_specific_ratios": industry_specific_ratios,
            "dupont_breakdown": dupont_breakdown,
            "roic": roic_series,
            "valuation_context": pe_percentile,
            "periods": [str(y) for y in years],
            "metadata": {
                "company_name": (company_info or {}).get("name", ""),
                "ticker": (company_info or {}).get("ticker", ""),
                "period_count": len(years),
            },
        }

        logger.info("财务分析完成: %d 个比率", len(ratios))
        return result

    def get_ratios_by_category(self, analysis_result: dict) -> dict[str, list]:
        """按类别分组比率"""
        ratios = analysis_result.get("ratios", {})
        categorized = {}
        for category, codes in RATIO_CATEGORIES.items():
            items = []
            for code in codes:
                if code in ratios:
                    name = FINANCIAL_RATIOS.get(code, code)
                    series = ratios[code]
                    items.append({
                        "code": code,
                        "name": name,
                        "series": series,
                        "latest": series[-1]["value"] if series else None,
                    })
            if items:
                categorized[category] = items
        return categorized

    # ── 内部方法 ──────────────────────────────────────────────

    def _get_aligned_years(
        self,
        income: pd.DataFrame,
        balance: pd.DataFrame,
        cashflow: pd.DataFrame,
    ) -> list[int]:
        """获取三张报表共有的年份（并集）"""
        years = set()
        for df in [income, balance, cashflow]:
            if not df.empty:
                years.update(df.index.year.unique())
        return sorted(years)

    def _align_by_year(
        self,
        income: pd.DataFrame,
        balance: pd.DataFrame,
        cashflow: pd.DataFrame,
        years: list[int],
    ) -> pd.DataFrame:
        """按年对齐三张报表，返回 year × metric_code DataFrame"""
        rows = {}
        for year in years:
            row = {}
            for df in [income, balance, cashflow]:
                if df.empty:
                    continue
                year_rows = df[df.index.year == year]
                if not year_rows.empty:
                    for col in year_rows.columns:
                        val = year_rows.iloc[-1][col]
                        if pd.notna(val):
                            row[col] = float(val)
            rows[year] = row
        return pd.DataFrame(rows).T

    def _safe_div(self, a: Optional[float], b: Optional[float]) -> Optional[float]:
        """安全除法"""
        if a is None or b is None:
            return None
        try:
            a_f, b_f = float(a), float(b)
        except (TypeError, ValueError):
            return None
        if abs(b_f) < 1e-12:
            return None
        return a_f / b_f

    def _get_val(
        self, aligned: pd.DataFrame, year: int, metric: str
    ) -> Optional[float]:
        """从对齐数据中安全获取指标值"""
        if year not in aligned.index:
            return None
        if metric not in aligned.columns:
            return None
        val = aligned.loc[year, metric]
        if pd.isna(val):
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def _compute_ratio(
        self,
        code: str,
        aligned: pd.DataFrame,
        years: list[int],
        price_data: Optional[pd.DataFrame] = None,
        company_info: Optional[dict] = None,
    ) -> Optional[list]:
        """计算单个比率的时间序列"""
        series = []

        for year in years:
            value = None

            if code == "ROE":
                ni = self._get_val(aligned, year, "NET_INCOME")
                eq = self._get_val(aligned, year, "TOTAL_EQUITY")
                value = self._safe_div(ni, eq)

            elif code == "ROA":
                ni = self._get_val(aligned, year, "NET_INCOME")
                ta = self._get_val(aligned, year, "TOTAL_ASSETS")
                value = self._safe_div(ni, ta)

            elif code == "GROSS_MARGIN":
                gp = self._get_val(aligned, year, "GROSS_PROFIT")
                rev = self._get_val(aligned, year, "REVENUE")
                value = self._safe_div(gp, rev)

            elif code == "NET_MARGIN":
                ni = self._get_val(aligned, year, "NET_INCOME")
                rev = self._get_val(aligned, year, "REVENUE")
                value = self._safe_div(ni, rev)

            elif code == "OPERATING_MARGIN":
                oi = self._get_val(aligned, year, "OPERATING_INCOME")
                rev = self._get_val(aligned, year, "REVENUE")
                value = self._safe_div(oi, rev)

            elif code == "CURRENT_RATIO":
                ca = self._get_val(aligned, year, "CURRENT_ASSETS")
                cl = self._get_val(aligned, year, "CURRENT_LIABILITIES")
                value = self._safe_div(ca, cl)

            elif code == "QUICK_RATIO":
                ca = self._get_val(aligned, year, "CURRENT_ASSETS")
                inv = self._get_val(aligned, year, "INVENTORY")
                cl = self._get_val(aligned, year, "CURRENT_LIABILITIES")
                quick_assets = (ca - inv) if (ca is not None and inv is not None) else ca
                value = self._safe_div(quick_assets, cl)

            elif code == "DEBT_TO_EQUITY":
                tl = self._get_val(aligned, year, "TOTAL_LIABILITIES")
                ta = self._get_val(aligned, year, "TOTAL_ASSETS")
                # 资产负债率 = 总负债 / 总资产
                value = self._safe_div(tl, ta)

            elif code == "PE":
                # 需要股价和每股收益
                value = self._compute_market_ratio(
                    year, "PE", price_data, aligned, company_info
                )

            elif code == "PB":
                value = self._compute_market_ratio(
                    year, "PB", price_data, aligned, company_info
                )

            elif code == "PS":
                value = self._compute_market_ratio(
                    year, "PS", price_data, aligned, company_info
                )

            elif code == "EV_EBITDA":
                value = self._compute_market_ratio(
                    year, "EV_EBITDA", price_data, aligned, company_info
                )

            elif code == "OPERATING_CF_MARGIN":
                ocf = self._get_val(aligned, year, "OPERATING_CASH_FLOW")
                rev = self._get_val(aligned, year, "REVENUE")
                value = self._safe_div(ocf, rev)

            elif code == "FREE_CASH_FLOW":
                ocf = self._get_val(aligned, year, "OPERATING_CASH_FLOW")
                capex = self._get_val(aligned, year, "CAPEX")
                if ocf is not None and capex is not None:
                    value = ocf - abs(capex)
                elif ocf is not None:
                    value = ocf

            elif code == "FCF_PER_SHARE":
                ocf = self._get_val(aligned, year, "OPERATING_CASH_FLOW")
                capex = self._get_val(aligned, year, "CAPEX")
                fcf = (ocf - abs(capex)) if (ocf is not None and capex is not None) else ocf
                shares = (company_info or {}).get("shares_outstanding")
                if fcf is not None and shares:
                    value = fcf / shares
                elif fcf is not None:
                    value = fcf  # 没股数就显示总量

            elif code == "CAPEX_TO_REVENUE":
                capex = self._get_val(aligned, year, "CAPEX")
                rev = self._get_val(aligned, year, "REVENUE")
                if capex is not None and rev is not None and rev > 0:
                    value = abs(capex) / rev

            elif code == "CASH_CONVERSION_RATIO":
                ocf = self._get_val(aligned, year, "OPERATING_CASH_FLOW")
                capex = self._get_val(aligned, year, "CAPEX")
                ni = self._get_val(aligned, year, "NET_INCOME")
                fcf = (ocf - abs(capex)) if (ocf is not None and capex is not None) else ocf
                value = self._safe_div(fcf, ni)

            elif code == "NET_DEBT_TO_EQUITY":
                tl = self._get_val(aligned, year, "TOTAL_LIABILITIES")
                cash = self._get_val(aligned, year, "CASH_AND_EQUIVALENTS")
                eq = self._get_val(aligned, year, "TOTAL_EQUITY")
                if tl is not None and cash is not None and eq is not None and abs(eq) > 1e-12:
                    value = (tl - cash) / eq
                elif tl is not None and eq is not None:
                    value = self._safe_div(tl, eq)

            elif code == "FCF_YIELD":
                ocf = self._get_val(aligned, year, "OPERATING_CASH_FLOW")
                capex = self._get_val(aligned, year, "CAPEX")
                fcf = (ocf - abs(capex)) if (ocf is not None and capex is not None) else ocf
                market_cap = (company_info or {}).get("market_cap")
                if fcf is not None and market_cap and market_cap > 0:
                    value = fcf / market_cap

            elif code == "BUYBACK_YIELD":
                buyback = self._get_val(aligned, year, "STOCK_BUYBACK")
                market_cap = (company_info or {}).get("market_cap")
                if buyback is not None and market_cap and market_cap > 0:
                    value = abs(buyback) / market_cap

            elif code == "RD_TO_REVENUE":
                rd = self._get_val(aligned, year, "RESEARCH_DEV")
                rev = self._get_val(aligned, year, "REVENUE")
                value = self._safe_div(rd, rev)

            elif code == "ASSET_TURNOVER":
                rev = self._get_val(aligned, year, "REVENUE")
                ta = self._get_val(aligned, year, "TOTAL_ASSETS")
                value = self._safe_div(rev, ta)

            elif code == "EQUITY_MULTIPLIER":
                ta = self._get_val(aligned, year, "TOTAL_ASSETS")
                eq = self._get_val(aligned, year, "TOTAL_EQUITY")
                value = self._safe_div(ta, eq)

            elif code in ("REVENUE_GROWTH", "NET_INCOME_GROWTH"):
                # 增长率单独计算
                pass

            series.append({"period": str(year), "value": value})

        if all(v["value"] is None for v in series):
            return None

        return series

    # 企业税率（NOPAT 近似，显式标注为估算；优先用真实税率）
    _CORPORATE_TAX_RATE = 0.21

    def _compute_dupont(self, aligned: pd.DataFrame, years: list[int]) -> list[dict]:
        """杜邦分解：ROE = 净利率 × 资产周转率 × 权益乘数（逐年，真实财务数据）。"""
        out = []
        for year in years:
            ni = self._get_val(aligned, year, "NET_INCOME")
            rev = self._get_val(aligned, year, "REVENUE")
            ta = self._get_val(aligned, year, "TOTAL_ASSETS")
            eq = self._get_val(aligned, year, "TOTAL_EQUITY")
            if ta is None or eq is None:
                continue
            out.append({
                "period": str(year),
                "net_margin": self._safe_div(ni, rev),
                "asset_turnover": self._safe_div(rev, ta),
                "equity_multiplier": self._safe_div(ta, eq),
                "roe": self._safe_div(ni, eq),
            })
        return out

    def _compute_roic(self, aligned: pd.DataFrame, years: list[int]) -> list[dict]:
        """ROIC ≈ 营业利润×(1-税率) / (总资产−现金−流动负债)。

        invested_capital 用「经营性投入资本」近似（非货币经营资产 − 经营性流动负债），
        显式标注为估算，非公司披露口径。
        """
        out = []
        for year in years:
            oi = self._get_val(aligned, year, "OPERATING_INCOME")
            ta = self._get_val(aligned, year, "TOTAL_ASSETS")
            cash = self._get_val(aligned, year, "CASH_AND_EQUIVALENTS")
            cl = self._get_val(aligned, year, "CURRENT_LIABILITIES")
            if oi is None or ta is None:
                continue
            nopat = oi * (1 - self._CORPORATE_TAX_RATE)
            ic = ta - (cash or 0) - (cl or 0)
            roic = self._safe_div(nopat, ic) if ic and ic > 0 else None
            out.append({"period": str(year), "nopat": round(nopat, 2),
                        "invested_capital": round(ic, 2) if ic is not None else None,
                        "roic": roic})
        return out

    def _compute_pe_percentile(
        self,
        aligned: pd.DataFrame,
        years: list[int],
        price_data: Optional[pd.DataFrame],
        company_info: Optional[dict],
    ) -> dict:
        """当前 PE 相对历史区间分位（近似，显式标注）。"""
        if price_data is None or price_data.empty:
            return {"pe_percentile": None, "note": "缺历史行情"}
        shares = (company_info or {}).get("shares_outstanding")
        if not shares or float(shares) <= 0:
            return {"pe_percentile": None, "note": "缺股本数据"}
        shares = float(shares)
        pairs = []
        for year in years:
            ni = self._get_val(aligned, year, "NET_INCOME")
            if ni is None or ni <= 0:
                continue
            yp = price_data[price_data.index.year == year]
            if yp.empty:
                continue
            close_col = "Close" if "Close" in yp.columns else yp.columns[0]
            avg_price = float(yp[close_col].mean())
            pairs.append((year, avg_price * shares / ni))
        if len(pairs) < 3:
            return {"pe_percentile": None, "note": f"历史 PE 样本不足({len(pairs)})"}
        close_col = "Close" if "Close" in price_data.columns else price_data.columns[0]
        cur_price = float(price_data[close_col].iloc[-1])
        latest_ni = self._get_val(aligned, years[-1], "NET_INCOME")
        if not latest_ni or latest_ni <= 0:
            return {"pe_percentile": None, "note": "最新净利为负，PE 不适用"}
        cur_pe = cur_price * shares / latest_ni
        hist_pe = [p[1] for p in pairs]
        pct = sum(1 for p in hist_pe if p < cur_pe) / len(hist_pe)
        return {
            "pe_percentile": round(pct, 3),
            "current_pe": round(cur_pe, 2),
            "hist_pe": [round(p, 2) for p in hist_pe],
            "note": "近似：股本取当前值、PE=当年均价×股本/当年净利，仅供区间参考",
        }

    def _compute_market_ratio(
        self,
        year: int,
        code: str,
        price_data: Optional[pd.DataFrame],
        aligned: pd.DataFrame,
        company_info: Optional[dict],
    ) -> Optional[float]:
        """计算需要股价的比率"""
        if price_data is None or price_data.empty:
            return None

        # 获取该年平均收盘价
        year_prices = price_data[price_data.index.year == year]
        if year_prices.empty:
            return None

        close_col = "Close" if "Close" in year_prices.columns else year_prices.columns[0]
        avg_price = float(year_prices[close_col].mean())

        market_cap = (company_info or {}).get("market_cap")

        if code == "PE":
            # PE = 股价 / EPS，EPS = NET_INCOME / shares_outstanding
            ni = self._get_val(aligned, year, "NET_INCOME")
            if ni is None or ni <= 0:
                return None
            # 如果没有 shares_outstanding，尝试用 market_cap 推算
            if market_cap and ni:
                return market_cap / ni  # 这其实是 PE 的倒数 * price，近似
            return None

        if code == "PB":
            eq = self._get_val(aligned, year, "TOTAL_EQUITY")
            if eq is None or eq <= 0 or not market_cap:
                return None
            # PB = 市值 / 净资产
            return market_cap / eq

        if code == "PS":
            rev = self._get_val(aligned, year, "REVENUE")
            if rev is None or rev <= 0 or not market_cap:
                return None
            return market_cap / rev

        if code == "EV_EBITDA":
            ebitda = self._get_val(aligned, year, "EBITDA")
            if ebitda is None or ebitda <= 0 or not market_cap:
                return None
            tl = self._get_val(aligned, year, "TOTAL_LIABILITIES") or 0
            cash = self._get_val(aligned, year, "CASH_AND_EQUIVALENTS") or 0
            ev = market_cap + tl - cash
            return ev / ebitda

        return None

    def _compute_growth_rates(
        self,
        aligned: pd.DataFrame,
        income: pd.DataFrame,
        years: list[int],
    ) -> dict[str, list]:
        """计算同比增长率"""
        result = {}

        for code, metric in [("REVENUE_GROWTH", "REVENUE"),
                              ("NET_INCOME_GROWTH", "NET_INCOME")]:
            series = []
            for i, year in enumerate(years):
                if i == 0:
                    series.append({"period": str(year), "value": None})
                else:
                    curr = self._get_val(aligned, year, metric)
                    prev = self._get_val(aligned, years[i - 1], metric)
                    if curr is not None and prev is not None and abs(prev) > 1e-12:
                        growth = (curr - prev) / abs(prev)
                        series.append({"period": str(year), "value": growth})
                    else:
                        series.append({"period": str(year), "value": None})

            if any(v["value"] is not None for v in series):
                result[code] = series

        return result

    def _compute_industry_specific_ratios(
        self,
        income_stmt: pd.DataFrame,
        balance_sheet: pd.DataFrame,
        cf_stmt: pd.DataFrame,
        years: list[int],
        industry_cls: IndustryClass,
    ) -> dict:
        """计算行业特有财务指标（Phase 6）

        目前支持:
        - FINANCIAL: NIM（净息差）, NPL_RATIO（不良贷款率）
        - REAL_ESTATE: NAV（净资产价值）

        yfinance 数据字段因公司而异，数据不可用时静默降级。
        """
        result = {}

        # 银行/金融特有指标
        if industry_cls == IndustryClass.FINANCIAL:
            # NIM ≈ 利息收入 / 生息资产
            interest_income = self._safe_series(income_stmt, "Interest Income")
            earning_assets = self._safe_series(balance_sheet, "Earning Assets")
            if not earning_assets.empty and not interest_income.empty:
                aligned = earning_assets.align(interest_income, join="inner")
                nim_vals = []
                for y in years:
                    ii = interest_income.get(y)
                    ea = earning_assets.get(y)
                    if pd.notna(ii) and pd.notna(ea) and ea > 0:
                        nim_vals.append({"period": str(y), "value": round(float(ii) / float(ea), 6)})
                if nim_vals:
                    result["NIM"] = nim_vals

            # NPL 比率 ≈ 不良贷款 / 贷款总额
            npl = self._safe_series(balance_sheet, "Non Performing Loans")
            total_loans = self._safe_series(balance_sheet, "Total Loans")
            if not npl.empty and not total_loans.empty:
                npl_vals = []
                for y in years:
                    nv = npl.get(y)
                    tv = total_loans.get(y)
                    if pd.notna(nv) and pd.notna(tv) and tv > 0:
                        npl_vals.append({"period": str(y), "value": round(float(nv) / float(tv), 6)})
                if npl_vals:
                    result["NPL_RATIO"] = npl_vals

        # 地产特有指标
        elif industry_cls == IndustryClass.REAL_ESTATE:
            # NAV ≈ 总资产 - 总负债
            total_assets = self._safe_series(balance_sheet, "Total Assets")
            total_liab = self._safe_series(balance_sheet, "Total Liabilities Net Minority Interest")
            if total_liab.empty:
                total_liab = self._safe_series(balance_sheet, "Total Liabilities")
            if not total_assets.empty and not total_liab.empty:
                nav_vals = []
                for y in years:
                    ta = total_assets.get(y)
                    tl = total_liab.get(y)
                    if pd.notna(ta) and pd.notna(tl):
                        nav_vals.append({"period": str(y), "value": round(float(ta) - float(tl), 2)})
                if nav_vals:
                    result["NAV"] = nav_vals

        if result:
            logger.info("行业特有财务指标计算完成: %s", list(result.keys()))
        return result

    def _safe_series(
        self, df: pd.DataFrame, col: str
    ) -> pd.Series:
        """安全地从 DataFrame 提取列 Series（按年份索引）"""
        if df.empty or col not in df.columns:
            return pd.Series(dtype=float)
        return pd.to_numeric(df[col], errors="coerce")

    def _empty_result(self) -> dict:
        return {
            "ratios": {},
            "growth_rates": {},
            "periods": [],
            "metadata": {},
        }
