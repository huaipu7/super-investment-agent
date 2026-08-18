"""看板 Excel 导出（纯函数，离线可测）。

- financial_xlsx(result): 财务规模 / 财务比率 / 成长能力 / 杜邦 / ROIC / DCF敏感性 / 相对估值
  → xlsx bytes（openpyxl 引擎，openpyxl 已可用）。
- backtest_single_xlsx(r) / backtest_batch_xlsx(br): 回测结果导出。

只做格式编排，不重算任何数值（数据真实性红线：原始数值原样落盘，缺失留空）。
"""
from __future__ import annotations

import io

import pandas as pd

# 原始财务指标代码 → 中文名
_RAW_LABELS = {
    "NET_INCOME": "净利润", "TOTAL_EQUITY": "股东权益", "TOTAL_ASSETS": "总资产",
    "REVENUE": "营业收入", "TAX_PROVISION": "所得税", "INTEREST_EXPENSE": "利息费用",
    "STOCK_BUYBACK": "股票回购", "OPERATING_INCOME": "营业利润", "EBITDA": "EBITDA",
    "NET_DEBT": "净负债", "CASH_AND_EQUIVALENTS": "现金及等价物",
    "TOTAL_LIABILITIES": "总负债",
}

_RATIO_LABELS = {
    "ROE": "ROE净资产收益率", "ROA": "ROA总资产收益率", "GROSS_MARGIN": "毛利率",
    "NET_MARGIN": "净利率", "OPERATING_MARGIN": "营业利润率", "CURRENT_RATIO": "流动比率",
    "QUICK_RATIO": "速动比率", "DEBT_TO_EQUITY": "资产负债率", "PE": "PE市盈率",
    "PB": "PB市净率", "PS": "PS市销率", "EV_EBITDA": "EV/EBITDA",
    "REVENUE_GROWTH": "营收增长率", "NET_INCOME_GROWTH": "净利润增长率",
    "OPERATING_CF_MARGIN": "经营现金流利润率", "FREE_CASH_FLOW": "自由现金流",
    "FCF_PER_SHARE": "每股自由现金流", "CAPEX_TO_REVENUE": "资本支出占比",
    "CASH_CONVERSION_RATIO": "现金流转化率", "RD_TO_REVENUE": "研发投入占比",
    "ASSET_TURNOVER": "资产周转率", "EQUITY_MULTIPLIER": "权益乘数",
    "NET_DEBT_TO_EQUITY": "净负债权益比", "FCF_YIELD": "自由现金流收益率",
}


def _series_wide(series_map: dict, labels: dict) -> pd.DataFrame:
    """[{code: [{"period","value"}, ...]}, ...] → 宽表（行=期间，列=中文名）。"""
    rows: dict[str, dict] = {}
    for code, series in (series_map or {}).items():
        name = labels.get(code, code)
        for s in series or []:
            p = s.get("period", "?")
            rows.setdefault(p, {})[name] = s.get("value")
    df = pd.DataFrame(rows).T
    if not df.empty:
        df.index.name = "期间"
    return df


def financial_xlsx(result: dict) -> bytes | None:
    """深度研究结果 → 财务工作簿 xlsx bytes；无数据返回 None。"""
    fin = result.get("financial_analysis") or {}
    va = result.get("valuation_analysis") or {}
    sheets: list[tuple[str, pd.DataFrame]] = []

    rm = _series_wide(fin.get("raw_metrics") or {}, _RAW_LABELS)
    if not rm.empty:
        sheets.append(("财务规模", rm))

    ratios = _series_wide(fin.get("ratios") or {}, _RATIO_LABELS)
    if not ratios.empty:
        sheets.append(("财务比率", ratios))

    gr = _series_wide(fin.get("growth_rates") or {}, _RATIO_LABELS)
    if not gr.empty:
        sheets.append(("成长能力", gr))

    dup = fin.get("dupont_breakdown") or []
    if dup:
        sheets.append(("杜邦分解", pd.DataFrame(dup)))

    roic = fin.get("roic") or []
    if roic:
        sheets.append(("ROIC", pd.DataFrame(roic)))

    dcf = va.get("dcf") if isinstance(va.get("dcf"), dict) else None
    if dcf:
        meta = pd.DataFrame([
            {"参数": "每股内在价值", "值": dcf.get("fair_value")},
            {"参数": "upside", "值": dcf.get("upside")},
            {"参数": "WACC", "值": dcf.get("wacc_used")},
            {"参数": "永续增长率", "值": dcf.get("terminal_growth")},
            {"参数": "预测期(年)", "值": dcf.get("projection_years")},
            {"参数": "企业价值(EV)", "值": dcf.get("fair_value_ev")},
        ])
        sheets.append(("DCF估值", meta))
        sens = dcf.get("sensitivity") or {}
        wacc_vals = sens.get("wacc_values") or []
        g_vals = sens.get("growth_values") or []
        matrix = sens.get("matrix") or []
        if wacc_vals and g_vals and matrix:
            sm = pd.DataFrame(
                matrix,
                index=[f"永续g {g * 100:.1f}%" for g in g_vals],
                columns=[f"WACC {w * 100:.1f}%" for w in wacc_vals],
            )
            sm.index.name = "永续g"
            sheets.append(("DCF敏感性", sm))

    rel = va.get("relative_valuation") if isinstance(va.get("relative_valuation"), dict) else {}
    if rel:
        rel_rows = []
        for code, label in (("pe", "PE"), ("pb", "PB"), ("ps", "PS"), ("ev_ebitda", "EV/EBITDA")):
            e = rel.get(code)
            if isinstance(e, dict):
                rel_rows.append({"指标": label, "公司": e.get("company"),
                                 "行业均值": e.get("industry_avg"), "溢价": e.get("premium")})
        if rel_rows:
            sheets.append(("相对估值", pd.DataFrame(rel_rows)))

    if not sheets:
        return None
    return _write_xlsx(sheets)


def backtest_single_xlsx(r) -> bytes | None:
    """单次回测结果 → xlsx bytes（metrics + trades + traces）。"""
    d = r.model_dump(mode="json")
    sheets: list[tuple[str, pd.DataFrame]] = []
    m = d.get("metrics") or {}
    if m:
        sheets.append(("绩效指标", pd.DataFrame([m])))
    trades = d.get("trades") or []
    if trades:
        sheets.append(("交易明细", pd.DataFrame(trades)))
    traces = d.get("traces") or []
    if traces:
        sheets.append(("Agent流水", pd.DataFrame(traces)))
    if not sheets:
        return None
    return _write_xlsx(sheets)


def backtest_batch_xlsx(br) -> bytes | None:
    """批量 / walk-forward 回测 → xlsx bytes（逐期汇总 + 稳定性统计）。"""
    d = br.model_dump(mode="json")
    sheets: list[tuple[str, pd.DataFrame]] = []
    runs = d.get("runs") or []
    if runs:
        rows = []
        for r in runs:
            row = {"期": r.get("period"), "标的": r.get("symbol"),
                   "市场": (r.get("market") or {}).get("value") if isinstance(r.get("market"), dict) else r.get("market")}
            if r.get("cutoff_date"):
                row["cutoff"] = r["cutoff_date"]
            m = r.get("metrics") or {}
            row.update(m)
            rows.append(row)
        sheets.append(("逐期结果", pd.DataFrame(rows)))
    stats = d.get("stats")
    if stats and isinstance(stats, dict) and any(stats.values()):
        sheets.append(("稳定性统计", pd.DataFrame([stats])))
    if not sheets:
        return None
    return _write_xlsx(sheets)


def _write_xlsx(sheets: list[tuple[str, pd.DataFrame]]) -> bytes:
    """sheets 写入 xlsx bytes（sheet 名截断到 openpyxl 31 字符上限）。

    仅当 DataFrame 索引有名字时写为首列（期间 / 永续g 等），其余用默认行号。
    """
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for name, df in sheets:
            df.to_excel(w, sheet_name=str(name)[:31], index=bool(df.index.name))
    return buf.getvalue()
