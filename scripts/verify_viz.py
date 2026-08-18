"""看板可视化模块离线验证（无需后端）。

构造结构忠实的合成分析结果，逐个调用 ui/viz_charts.py 各图表函数：
断言折线/柱状/箱型返回 Figure 且含 trace、饼图空数据返回 None、表格行列正确、
空数据优雅降级不崩溃。

运行: D:\\python\\python.exe scripts\\verify_viz.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PERIODS = ["2019", "2020", "2021", "2022", "2023"]


def _series(scale: float = 1.0) -> list[dict]:
    vals = [0.25, 0.28, 0.31, 0.30, 0.34]
    return [{"period": p, "value": v * scale}
            for p, v in zip(PERIODS, vals)]


def build_fin() -> dict:
    return {
        "ratios": {
            "ROE": _series(),
            "GROSS_MARGIN": _series(2.0),
            "PE": [{"period": p, "value": v * 100} for p, v in zip(PERIODS, [18, 20, 22, 19, 21])],
        },
        "growth_rates": {"REVENUE_GROWTH": _series(3.0)},
        "raw_metrics": {
            "REVENUE": [{"period": p, "value": v * 1e11} for p, v in zip(PERIODS, [1.0, 1.2, 1.4, 1.6, 1.8])],
            "NET_INCOME": [{"period": p, "value": v * 1e10} for p, v in zip(PERIODS, [4.5, 5.0, 5.5, 6.0, 6.5])],
        },
        "fcf_history": [1e10] * 5,
        "periods": PERIODS,
        "current_price": 1345.0,
        "metadata": {"company_name": "贵州茅台", "ticker": "600519.SS", "period_count": 5},
    }


def build_val() -> dict:
    return {
        "relative_valuation": {
            "pe": {"company": 19.8, "industry_avg": 28.0, "premium": -0.29},
            "pb": {"company": 8.1, "industry_avg": 5.0, "premium": 0.62},
            "ps": {"company": 9.5, "industry_avg": 6.2, "premium": 0.53},
            "ev_ebitda": {"company": 15.0, "industry_avg": 18.0, "premium": -0.17},
        },
        "dcf": {"fair_value": 2354.02, "upside": 0.75},
    }


def build_market() -> dict:
    return {
        "company": {"name": "贵州茅台", "revenue": 1.5e11},
        "market_share": 0.42,
        "industry_concentration": {"cr3": 0.6, "cr5": 0.8, "hhi": 0.2},
        "competitors": [
            {"name": "五粮液", "revenue": 8e10, "share": 0.22},
            {"name": "泸州老窖", "revenue": 3e10, "share": 0.08},
        ],
    }


def build_hist() -> dict:
    return {"records": [{"date": f"2026-08-{d:02d}", "close": 1345.0 + d}
                        for d in range(1, 6)], "count": 5}


def main() -> None:
    import ui.viz_charts as vz

    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  {'PASS' if cond else 'FAIL'}  {name} {detail}")

    print("[verify_viz] 图表构建（合成结果）")

    # 折线
    fig = vz.price_line_fig(build_hist())
    check("price_line_fig", fig is not None and len(fig.data) == 1,
          f"(traces={len(fig.data) if fig else 0})")
    check("price_line_fig(空)", vz.price_line_fig({"records": []}) is None)

    fin = build_fin()
    cats = vz.available_categories(fin)
    check("available_categories", "盈利能力" in cats and "市场估值" in cats,
          f"(cats={cats})")
    check("available_categories(空)", vz.available_categories({}) == [])

    fig = vz.ratio_trend_fig(fin, "盈利能力")
    check("ratio_trend_fig", fig is not None and len(fig.data) >= 1,
          f"(traces={len(fig.data) if fig else 0})")
    check("ratio_trend_fig(空分类)", len(vz.ratio_trend_fig({}, "盈利能力").data) == 0)

    # 柱状
    fig = vz.raw_metric_bars_fig(fin, ["REVENUE", "NET_INCOME"])
    check("raw_metric_bars_fig", fig is not None and len(fig.data) == 2,
          f"(traces={len(fig.data) if fig else 0})")
    fig = vz.valuation_bar_fig(build_val())
    check("valuation_bar_fig", fig is not None and len(fig.data) == 2,
          f"(traces={len(fig.data) if fig else 0})")
    check("valuation_bar_fig(空)", vz.valuation_bar_fig({}) is None)

    # 饼图
    fig = vz.market_pie_fig(build_market())
    check("market_pie_fig", fig is not None and len(fig.data) == 1)
    check("market_pie_fig(空竞品)", vz.market_pie_fig(
        {"company": {"name": "X"}, "competitors": []}) is None)
    check("market_pie_fig(None)", vz.market_pie_fig(None) is None)

    # 箱型（≥3 年数据）
    fig = vz.ratio_box_fig(fin, "盈利能力")
    check("ratio_box_fig", fig is not None and len(fig.data) >= 1,
          f"(traces={len(fig.data) if fig else 0})")
    short = {"ratios": {"ROE": [{"period": "2020", "value": 0.1},
                                {"period": "2021", "value": 0.2}]}}
    check("ratio_box_fig(不足3年)", len(vz.ratio_box_fig(short, "盈利能力").data) == 0)

    # 表格
    df1 = vz.raw_metrics_df(fin)
    check("raw_metrics_df", len(df1) == 2 and list(df1.columns) == PERIODS,
          f"(shape={df1.shape})")
    check("raw_metrics_df(空)", vz.raw_metrics_df({}).empty)
    df2 = vz.ratios_df(fin)
    check("ratios_df", len(df2) >= 2 and list(df2.columns) == PERIODS,
          f"(shape={df2.shape})")

    # 金额缩放：REVENUE 1.8e11 → 1800 亿
    check("金额缩放/亿", abs(df1.loc["营业收入", "2023"] - 1800.0) < 1e-6)

    print(f"[verify_viz] 总体: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
