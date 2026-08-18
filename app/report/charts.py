"""图表生成器 —— 迁移自 Research-Agent(report_generation/chart_generator.py)。

使用 matplotlib 生成财务分析图表（中文标签，PNG 输出），用于嵌入 PDF/HTML 报告。
中文字体注册统一走 app.report.fonts。
"""
from __future__ import annotations

import os
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

from app.report.fonts import matplotlib_register
from app.utils.logger import logger

matplotlib_register()


class ChartGenerator:
    """图表生成器

    用法:
        gen = ChartGenerator(output_dir="data/charts")
        chart_paths = gen.generate_all(analysis_result)
    """

    def __init__(self, output_dir: str = "data/charts"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    # ── 公共接口 ───────────────────────────────────────────────

    def generate_all(self, analysis_result: dict) -> dict[str, str]:
        """生成所有图表，返回 {chart_key: file_path}"""
        charts = {}

        try:
            fin = analysis_result.get("financial_analysis", {})
            market = analysis_result.get("market_analysis", {})
            competitive = analysis_result.get("competitive_analysis", {})

            periods = fin.get("periods", [])
            ratios = fin.get("ratios", {})
            growth_rates = fin.get("growth_rates", {})

            # 原有图表
            if periods and ratios:
                path = self._ratios_trend(ratios, periods, "ratios_trend.png")
                if path:
                    charts["ratios_trend"] = path

            if periods and ratios:
                path = self._revenue_growth(ratios, growth_rates,
                                            periods, "revenue_growth.png")
                if path:
                    charts["revenue_growth"] = path

            competitors = market.get("competitors", [])
            company = market.get("company", {})
            if competitors or company.get("revenue"):
                path = self._market_share_pie(competitors, company,
                                              "market_share.png")
                if path:
                    charts["market_share"] = path

            # 使用 global 市场的 competitor_data（含营收数据）
            global_market = market.get("markets", {}).get("global", {})
            comp_competitors_rev = global_market.get("competitor_data", [])
            if comp_competitors_rev:
                path = self._competitor_bar(comp_competitors_rev, company,
                                            "competitor_comparison.png")
                if path:
                    charts["competitor_comparison"] = path

            # ── 4 张分类折线图 ──
            if periods and ratios:
                path = self._line_chart(ratios, periods,
                    ["ROE", "ROA", "GROSS_MARGIN", "NET_MARGIN", "OPERATING_MARGIN"],
                    ["ROE", "ROA", "毛利率", "净利率", "营业利润率"],
                    ["#2563eb", "#16a34a", "#d97706", "#dc2626", "#8b5cf6"],
                    "盈利能力趋势", "profitability_trend.png")
                if path: charts["profitability_trend"] = path

                path = self._line_chart(ratios, periods,
                    ["CURRENT_RATIO", "QUICK_RATIO", "DEBT_TO_EQUITY"],
                    ["流动比率", "速动比率", "资产负债率"],
                    ["#2563eb", "#16a34a", "#d97706"],
                    "偿债能力趋势", "solvency_trend.png")
                if path: charts["solvency_trend"] = path

                path = self._line_chart(ratios, periods,
                    ["PE", "PB", "PS", "EV_EBITDA"],
                    ["PE", "PB", "PS", "EV/EBITDA"],
                    ["#2563eb", "#16a34a", "#d97706", "#dc2626"],
                    "市场估值趋势", "valuation_trend.png")
                if path: charts["valuation_trend"] = path

                merged = {**ratios, **growth_rates}
                path = self._line_chart(merged, periods,
                    ["REVENUE_GROWTH", "NET_INCOME_GROWTH"],
                    ["营收增长率", "净利润增长率"],
                    ["#16a34a", "#d97706"],
                    "成长能力趋势", "growth_trend.png")
                if path: charts["growth_trend"] = path

            # ── 3 张三市场营收对比柱状图 ──
            markets_data = market.get("markets", {})
            for mk_key, mk_label in [("global", "全球"), ("us", "美国"), ("china", "中国")]:
                mk = markets_data.get(mk_key, {})
                mk_competitors = mk.get("competitor_data", [])
                mk_company = mk.get("company", {})
                if mk_competitors or mk_company.get("revenue"):
                    path = self._market_bar_chart(
                        mk_competitors, mk_company,
                        f"market_{mk_key}_comparison.png",
                        f"{mk_label}市场营收对比")
                    if path:
                        charts[f"market_{mk_key}_comparison"] = path

            # ── R&D 柱状图 ──
            if periods and ratios:
                path = self._rd_chart(ratios, periods, "rd_investment.png")
                if path:
                    charts["rd_investment"] = path

            # ── 现金流趋势图 ──
            if periods and ratios:
                path = self._cash_flow_chart(ratios, periods, "cash_flow_trend.png")
                if path:
                    charts["cash_flow_trend"] = path

            # ── K线 + 均线叠加（投资策略章节配图）──
            path = self._price_action_chart(analysis_result, "price_action.png")
            if path:
                charts["price_action"] = path

            # ── Phase 2：DCF 敏感性热力图 + 杜邦分解瀑布 ──
            path = self._dcf_heatmap(analysis_result, "dcf_sensitivity.png")
            if path:
                charts["dcf_sensitivity"] = path
            path = self._dupont_chart(analysis_result, "dupont.png")
            if path:
                charts["dupont"] = path

        except Exception as e:
            logger.warning("图表生成部分失败: %s", e)

        logger.info("生成 %d 个图表", len(charts))
        return charts

    # ── 通用折线图 ─────────────────────────────────────────────

    def _line_chart(
        self,
        ratios: dict,
        periods: list[str],
        codes: list[str],
        labels: list[str],
        colors: list[str],
        title: str,
        filename: str,
    ) -> Optional[str]:
        """通用折线图"""
        fig, ax = plt.subplots(figsize=(8, 4.5))
        x = np.arange(len(periods))

        has_data = False
        for code, label, color in zip(codes, labels, colors):
            series = ratios.get(code, [])
            values = [s["value"] for s in series if s["value"] is not None]
            if not values:
                continue
            full = [s["value"] for s in series]
            ax.plot(x, full, marker="o", label=label, color=color,
                    linewidth=1.8, markersize=4)
            has_data = True

        if not has_data:
            plt.close(fig)
            return None

        ax.set_xticks(x)
        ax.set_xticklabels(periods, fontsize=9)
        ax.set_ylabel("比率值", fontsize=10)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(
            lambda v, _: f"{v:.0%}" if abs(v) < 10 else f"{v:.1f}"
        ))
        ax.legend(fontsize=9, loc="best")
        ax.grid(True, alpha=0.3)
        ax.set_title(title, fontsize=12, fontweight="bold")
        fig.tight_layout()

        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        logger.debug("图表已保存: %s", path)
        return path

    # ── 原有图表 ───────────────────────────────────────────────

    def _ratios_trend(
        self,
        ratios: dict,
        periods: list[str],
        filename: str,
    ) -> Optional[str]:
        """关键财务比率趋势图"""
        return self._line_chart(
            ratios, periods,
            ["ROE", "GROSS_MARGIN", "NET_MARGIN", "CURRENT_RATIO"],
            ["ROE", "毛利率", "净利率", "流动比率"],
            ["#2563eb", "#16a34a", "#d97706", "#dc2626"],
            "关键财务比率趋势", filename,
        )

    def _revenue_growth(
        self,
        ratios: dict,
        growth_rates: dict,
        periods: list[str],
        filename: str,
    ) -> Optional[str]:
        """营收增长趋势（增长率柱状图）"""
        growth_series = growth_rates.get("REVENUE_GROWTH", [])
        growth_values = [s["value"] for s in growth_series]

        if not growth_values or all(v is None for v in growth_values):
            return None

        fig, ax = plt.subplots(figsize=(8, 4))
        x = np.arange(len(periods))

        vals = [v if v is not None else 0 for v in growth_values]
        colors = ["#16a34a" if v >= 0 else "#dc2626" for v in vals]
        bars = ax.bar(x, vals, color=colors, width=0.5, alpha=0.85)

        ax.set_xticks(x)
        ax.set_xticklabels(periods, fontsize=9)
        ax.set_ylabel("同比增长率", fontsize=10)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(
            lambda v, _: f"{v:+.0%}"
        ))
        ax.axhline(y=0, color="gray", linewidth=0.5)
        ax.set_title("营收同比增长率", fontsize=12, fontweight="bold")
        ax.grid(True, axis="y", alpha=0.3)

        for bar, val in zip(bars, vals):
            if abs(val) > 0.01:
                ax.text(bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + (0.01 if val >= 0 else -0.04),
                        f"{val:+.1%}", ha="center", va="bottom" if val >= 0 else "top",
                        fontsize=8)

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path

    def _market_share_pie(
        self,
        competitors: list[dict],
        company: dict,
        filename: str,
    ) -> Optional[str]:
        """市场份额饼图"""
        rev = company.get("revenue")
        if not rev and not competitors:
            return None

        labels = []
        sizes = []
        colors_list = ["#2563eb",
                       "#16a34a", "#d97706", "#dc2626",
                       "#8b5cf6", "#ec4899", "#06b6d4",
                       "#f97316", "#84cc16"]

        labels.append(f"{company.get('name', '目标公司')}")
        sizes.append(float(rev) if rev else 0)

        other_colors = colors_list[1:]
        for i, c in enumerate(competitors[:7]):
            if c.get("revenue"):
                labels.append(c.get("name", f"竞品{i+1}")[:12])
                sizes.append(float(c["revenue"]))
                if i >= len(other_colors):
                    break

        if not sizes or sum(sizes) == 0:
            return None

        fig, ax = plt.subplots(figsize=(6, 5))
        wedges, texts, autotexts = ax.pie(
            sizes, labels=labels, autopct="%1.1f%%",
            colors=colors_list[:len(labels)],
            startangle=90, pctdistance=0.78,
            textprops={"fontsize": 8},
        )
        for at in autotexts:
            at.set_fontsize(8)
        ax.set_title("市场份额分布（按营收）", fontsize=12, fontweight="bold")

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path

    def _competitor_bar(
        self,
        competitors: list[dict],
        company: dict,
        filename: str,
    ) -> Optional[str]:
        """竞品营收对比柱状图"""
        names = []
        revs = []

        company_rev = company.get("revenue")
        if company_rev:
            names.append(company.get("name", "目标公司")[:12])
            revs.append(float(company_rev))

        for c in competitors[:8]:
            if c.get("revenue"):
                names.append(c.get("name", "?")[:12])
                revs.append(float(c["revenue"]))

        if len(names) < 2:
            return None

        fig, ax = plt.subplots(figsize=(8, 4.5))
        x = np.arange(len(names))
        bars = ax.bar(x, revs, color="#2563eb", width=0.55, alpha=0.85)

        bars[0].set_color("#dc2626")

        ax.set_xticks(x)
        ax.set_xticklabels(names, fontsize=8, rotation=20, ha="right")
        ax.set_ylabel("营收", fontsize=10)
        ax.set_title("竞品营收对比", fontsize=12, fontweight="bold")
        ax.grid(True, axis="y", alpha=0.3)

        for bar, val in zip(bars, revs):
            ax.text(bar.get_x() + bar.get_width() / 2,
                    bar.get_height(),
                    f"{val:.1e}", ha="center", va="bottom",
                    fontsize=7)

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path

    # ── 三市场营收对比柱状图 ─────────────────────────────────

    def _market_bar_chart(
        self,
        competitor_data: list[dict],
        company: dict,
        filename: str,
        title: str = "营收对比",
    ) -> Optional[str]:
        """特定市场营收对比柱状图"""
        names = []
        revs = []

        company_rev = company.get("revenue")
        if company_rev:
            names.append(company.get("name", "目标公司")[:14])
            revs.append(float(company_rev))

        for c in competitor_data[:8]:
            if c.get("revenue"):
                names.append(c.get("name", "?")[:14])
                revs.append(float(c["revenue"]))

        if not names:
            return None

        fig, ax = plt.subplots(figsize=(8, 4.5))
        x = np.arange(len(names))
        bars = ax.bar(x, revs, color="#2563eb", width=0.55, alpha=0.85)
        if bars:
            bars[0].set_color("#dc2626")

        ax.set_xticks(x)
        ax.set_xticklabels(names, fontsize=8, rotation=20, ha="right")
        ax.set_ylabel("营收 (USD)", fontsize=10)
        ax.set_title(title, fontsize=12, fontweight="bold")
        ax.grid(True, axis="y", alpha=0.3)

        for bar, val in zip(bars, revs):
            ax.text(bar.get_x() + bar.get_width() / 2,
                    bar.get_height(),
                    f"{val:.1e}", ha="center", va="bottom",
                    fontsize=7)

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path

    # ── R&D 研发投入柱状图 ────────────────────────────────────

    def _rd_chart(
        self,
        ratios: dict,
        periods: list[str],
        filename: str,
    ) -> Optional[str]:
        """研发投入柱状图"""
        rd_series = ratios.get("RD_TO_REVENUE", [])
        rd_values = [s["value"] for s in rd_series]

        if not rd_values or all(v is None for v in rd_values):
            return None

        fig, ax = plt.subplots(figsize=(8, 4))
        x = np.arange(len(periods))
        vals = [v if v is not None else 0 for v in rd_values]
        bars = ax.bar(x, vals, color="#8b5cf6", width=0.5, alpha=0.85)

        ax.set_xticks(x)
        ax.set_xticklabels(periods, fontsize=9)
        ax.set_ylabel("研发投入占比", fontsize=10)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(
            lambda v, _: f"{v:.1%}"
        ))
        ax.set_title("研发投入占比趋势", fontsize=12, fontweight="bold")
        ax.grid(True, axis="y", alpha=0.3)

        for bar, val in zip(bars, vals):
            if val > 0.001:
                ax.text(bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + max(vals) * 0.02,
                        f"{val:.1%}", ha="center", va="bottom",
                        fontsize=8)

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path

    # ── 现金流趋势图 ──────────────────────────────────────────

    def _cash_flow_chart(
        self,
        ratios: dict,
        periods: list[str],
        filename: str,
    ) -> Optional[str]:
        """现金流趋势折线图（双 Y 轴：左轴经营现金流利润率%，右轴自由现金流绝对值）"""
        fig, ax1 = plt.subplots(figsize=(8, 4.5))
        x = np.arange(len(periods))

        has_data = False
        ocf_margin = ratios.get("OPERATING_CF_MARGIN", [])
        fcf_series = ratios.get("FREE_CASH_FLOW", [])

        # 左轴：经营现金流利润率（%）
        if ocf_margin:
            vals = [s["value"] if s["value"] is not None else 0 for s in ocf_margin]
            ax1.plot(x, vals, marker="o", label="经营现金流利润率", color="#2563eb",
                     linewidth=1.8, markersize=4)
            ax1.set_ylabel("经营现金流利润率", fontsize=10, color="#2563eb")
            ax1.yaxis.set_major_formatter(mticker.FuncFormatter(
                lambda v, _: f"{v:.0%}"
            ))
            has_data = True

        # 右轴：自由现金流（绝对值）
        ax2 = ax1.twinx()
        if fcf_series:
            vals = [s["value"] if s["value"] is not None else 0 for s in fcf_series]
            ax2.plot(x, vals, marker="s", label="自由现金流", color="#16a34a",
                     linewidth=1.8, markersize=4)
            ax2.set_ylabel("自由现金流 (USD)", fontsize=10, color="#16a34a")
            ax2.yaxis.set_major_formatter(mticker.FuncFormatter(
                lambda v, _: f"{v:.1e}"
            ))
            has_data = True

        if not has_data:
            plt.close(fig)
            return None

        ax1.set_xticks(x)
        ax1.set_xticklabels(periods, fontsize=9)

        # 合并图例（从两个轴收集 handles + labels）
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, fontsize=9, loc="best")

        ax1.grid(True, alpha=0.3)
        ax1.set_title("现金流趋势", fontsize=12, fontweight="bold")
        fig.tight_layout()

        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path

    # ── K线 + 均线叠加（投资策略章节配图）───────────────────────

    def _price_action_chart(self, analysis_result: dict, filename: str) -> Optional[str]:
        """日K线 + MA5/MA20/MA60 + 布林带 + 交易计划价位叠加图。

        数据来自管线 result["price_history"]（真实历史行情，列式）。
        红涨绿跌（中文约定）。任一关键缺失 → 返回 None（不抛异常）。
        """
        ph = analysis_result.get("price_history") or {}
        if not ph.get("available") or not ph.get("close"):
            return None
        try:
            close = np.asarray(ph["close"], dtype=float)
            if len(close) < 30 or not np.all(np.isfinite(close)):
                return None
            dates = pd.to_datetime(ph["dates"])
            df = pd.DataFrame({
                "open": np.asarray(ph["open"], dtype=float),
                "high": np.asarray(ph["high"], dtype=float),
                "low": np.asarray(ph["low"], dtype=float),
                "close": close,
            }, index=dates)
        except Exception:
            return None

        # 均线 / 布林带（与 trade_plan 同口径）
        ma5 = df["close"].rolling(5).mean()
        ma20 = df["close"].rolling(20).mean()
        ma60 = df["close"].rolling(60).mean()
        mid = df["close"].rolling(20).mean()
        sd = df["close"].rolling(20).std()
        boll_hi = mid + 2 * sd
        boll_lo = mid - 2 * sd

        n = len(df)
        x = np.arange(n)
        up_color, down_color = "#e74c3c", "#16a34a"

        fig, (ax, axv) = plt.subplots(2, 1, figsize=(9, 6.2), sharex=True,
                                      gridspec_kw={"height_ratios": [3, 1],
                                                   "hspace": 0.08})
        # 蜡烛：影线 + 实体（红涨绿跌，中文约定）
        for i in range(n):
            o, h, l, c = (df["open"].iloc[i], df["high"].iloc[i],
                          df["low"].iloc[i], df["close"].iloc[i])
            color = up_color if c >= o else down_color
            ax.vlines(i, l, h, color=color, linewidth=0.8)
            w = 0.35
            body = mpatches.Rectangle((i - w, min(o, c)), 2 * w,
                                      max(abs(c - o), 0.01), color=color, alpha=0.9)
            ax.add_patch(body)
        # 成交量副图（红涨绿跌，量纲自适应缩放）
        if "volume" in df.columns:
            vols = df["volume"].astype(float).fillna(0.0).values
            vcolors = [up_color if c >= o else down_color
                       for o, c in zip(df["open"], df["close"])]
            scale = 1.0
            if vols.max() > 0:
                scale = (close.max() - close.min()) / vols.max() if close.max() != close.min() else 1.0
            axv.bar(x, vols * scale, width=0.7, color=vcolors, alpha=0.7)
            axv.set_ylabel("成交量", fontsize=9)
            axv.yaxis.set_major_formatter(mticker.FuncFormatter(
                lambda v, _: f"{v / scale:,.0f}"))
        # 布林带填充
        ax.fill_between(x, boll_lo, boll_hi, color="#2563eb", alpha=0.08, label="布林带(20,2σ)")
        # 均线
        ax.plot(x, ma5, color="#f59e0b", linewidth=1.4, label="MA5")
        ax.plot(x, ma20, color="#2563eb", linewidth=1.4, label="MA20")
        ax.plot(x, ma60, color="#8b5cf6", linewidth=1.4, label="MA60")

        # 交易计划价位（来自 trade_plan，真实计算值）
        tp = analysis_result.get("trade_plan") or {}
        if tp.get("available"):
            ez = tp.get("entry_zone") or [None, None]
            if len(ez) > 0 and ez[0] is not None:
                ax.axhspan(ez[0], ez[1] if len(ez) > 1 and ez[1] is not None else ez[0],
                           color="#16a34a", alpha=0.12, label="进场区间")
            sl = tp.get("stop_loss")
            if sl is not None:
                ax.axhline(sl, color="#dc2626", linewidth=1.2, linestyle="--",
                           label=f"止损位 {sl:,.2f}")
            tpv = tp.get("take_profit")
            if tpv is not None:
                ax.axhline(tpv, color="#2563eb", linewidth=1.2, linestyle="--",
                           label=f"止盈位 {tpv:,.2f}")

        # x 轴日期刻度（约 6 个，显示在底部成交量面板）
        step = max(1, n // 6)
        ticks = list(range(0, n, step))
        ax.set_xticks(ticks)
        ax.set_xticklabels([])
        axv.set_xticks(ticks)
        axv.set_xticklabels([dates[i].strftime("%Y-%m-%d") for i in ticks], fontsize=8)
        axv.tick_params(axis="x", labelsize=8)
        ax.set_ylabel("价格", fontsize=10)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(
            lambda v, _: f"{v:,.0f}"
        ))
        ax.grid(True, alpha=0.25)
        ax.legend(fontsize=8, loc="best", ncol=2)
        ax.set_title(
            f"日K线与均线叠加（红涨绿跌）· {dates[0].strftime('%Y-%m-%d')} ~ "
            f"{dates[-1].strftime('%Y-%m-%d')}", fontsize=11, fontweight="bold")
        fig.tight_layout()

        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        logger.debug("K线图已保存: %s", path)
        return path

    # ── Phase 2：DCF 敏感性热力图 ─────────────────────────────

    def _dcf_heatmap(self, analysis_result: dict, filename: str) -> Optional[str]:
        """DCF 敏感性热力图（WACC × 永续增长率 → 每股内在价值）。

        数据来自 valuation_analysis.dcf.sensitivity（compute_sensitivity 输出）。
        缺失返回 None（不编造）。
        """
        va = analysis_result.get("valuation_analysis") or {}
        dcf = va.get("dcf") or {}
        sens = dcf.get("sensitivity") or {}
        wacc_vals = sens.get("wacc_values") or []
        g_vals = sens.get("growth_values") or []
        matrix = sens.get("matrix") or []
        if not wacc_vals or not g_vals or not matrix:
            return None
        Z = np.array([[np.nan if v is None else v for v in row] for row in matrix],
                     dtype=float)

        fig, ax = plt.subplots(figsize=(8, 5))
        im = ax.imshow(Z, cmap="RdYlGn", aspect="auto", origin="lower")
        ax.set_xticks(range(len(g_vals)))
        ax.set_xticklabels([f"{g * 100:.1f}%" for g in g_vals], fontsize=9)
        ax.set_yticks(range(len(wacc_vals)))
        ax.set_yticklabels([f"{w * 100:.1f}%" for w in wacc_vals], fontsize=9)
        ax.set_xlabel("永续增长率 g", fontsize=10)
        ax.set_ylabel("WACC", fontsize=10)
        ax.set_title("DCF 敏感性：每股内在价值（WACC × 永续增长率）",
                     fontsize=12, fontweight="bold")
        for i in range(len(wacc_vals)):
            for j in range(len(g_vals)):
                v = Z[i, j]
                if np.isfinite(v):
                    ax.text(j, i, f"{v:,.0f}", ha="center", va="center", fontsize=8)
        fig.colorbar(im, ax=ax, label="每股价值")

        base_fv = sens.get("base_fair_value")
        if base_fv is not None:
            bw = sens.get("base_wacc")
            bg = sens.get("base_terminal_growth")
            bi = int(np.argmin([abs(w - (bw or 0)) for w in wacc_vals]))
            bj = int(np.argmin([abs(g - (bg or 0)) for g in g_vals]))
            ax.add_patch(plt.Rectangle((bj - 0.5, bi - 0.5), 1, 1, fill=False,
                                       edgecolor="black", linewidth=2))

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path

    # ── Phase 2：杜邦分解瀑布 ────────────────────────────────

    def _dupont_chart(self, analysis_result: dict, filename: str) -> Optional[str]:
        """杜邦分解：最近一年 ROE 对数贡献瀑布 + 逐年 ROE 柱状。

        数据来自 financial_analysis.dupont_breakdown（净利率×周转率×乘数=ROE）。
        """
        fa = analysis_result.get("financial_analysis") or {}
        dup = fa.get("dupont_breakdown") or []
        items = [d for d in dup if d.get("roe") is not None]
        if not items:
            return None
        latest = items[-1]

        def _ln(v):
            return float(np.log(v)) if v is not None and v > 0 else None

        nm, at, em, roe_ln = (_ln(latest.get("net_margin")),
                              _ln(latest.get("asset_turnover")),
                              _ln(latest.get("equity_multiplier")),
                              _ln(latest.get("roe")))
        if None in (nm, at, em, roe_ln):
            return None

        fig, (axw, axb) = plt.subplots(2, 1, figsize=(8, 5.4),
                                       gridspec_kw={"hspace": 0.35})
        # 上：对数贡献瀑布（乘性 → 加性）
        comps = [("净利率", nm, latest.get("net_margin"), "{:.1%}"),
                 ("资产周转", at, latest.get("asset_turnover"), "×{:.2f}"),
                 ("权益乘数", em, latest.get("equity_multiplier"), "×{:.2f}")]
        labels, bars, bottoms, texts = [], [], [], []
        running = 0.0
        for label, lnv, orig, fmt in comps:
            labels.append(label)
            bottoms.append(running)
            bars.append(lnv)
            texts.append(fmt.format(orig))
            running += lnv
        # ROE total 柱
        labels.append("ROE")
        bottoms.append(0.0)
        bars.append(roe_ln)
        texts.append(f"{latest['roe']:.1%}")
        colors = ["#2563eb", "#d97706", "#8b5cf6", "#e74c3c"]
        for i in range(4):
            if i == 3:
                axw.bar(i, bars[i], bottom=0, color=colors[i], alpha=0.9, width=0.5)
                axw.text(i, bars[i], texts[i], ha="center", va="bottom", fontsize=9)
            else:
                axw.bar(i, bars[i], bottom=bottoms[i], color=colors[i], alpha=0.85, width=0.5)
                axw.text(i, bottoms[i] + bars[i] / 2, texts[i],
                         ha="center", va="center", fontsize=9)
        axw.axhline(0, color="gray", linewidth=0.5)
        axw.set_xticks(range(4))
        axw.set_xticklabels(labels, fontsize=9)
        axw.set_ylabel("ln(因子)", fontsize=9)
        axw.set_title(f"{latest['period']} ROE 拆解（对数贡献，终值还原为 ROE）",
                      fontsize=11, fontweight="bold")
        axw.grid(True, axis="y", alpha=0.3)

        # 下：逐年 ROE 柱状
        years = [d["period"] for d in items]
        roes = [d["roe"] * 100 for d in items]
        axb.bar(range(len(items)), roes, color="#e74c3c", alpha=0.85, width=0.5)
        axb.axhline(0, color="gray", linewidth=0.5)
        axb.set_xticks(range(len(items)))
        axb.set_xticklabels(years, fontsize=9)
        axb.set_ylabel("ROE %", fontsize=9)
        axb.set_title("ROE 逐年趋势", fontsize=11, fontweight="bold")
        for i, v in enumerate(roes):
            if abs(v) > 0.01:
                axb.text(i, v + 0.5, f"{v:.1f}%", ha="center", va="bottom", fontsize=8)
        axb.grid(True, axis="y", alpha=0.3)

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return path
