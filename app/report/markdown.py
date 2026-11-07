"""Markdown 报告生成 —— 将分析管线结果（AnalysisResult）转为结构化中文研报。

字段名以 app/analysis/pipeline.AnalysisOrchestrator.run() 实际输出为准；
缺失字段显式标注"暂无"，不编造。数值全部来自真实计算/数据源。
"""
from __future__ import annotations

from datetime import datetime

from app.compliance.rules import check_advice, content_hash
from app.constants import DATA_SOURCE_DISCLAIMER

# 比率显示格式：百分比 vs 倍数
_PCT_CODES = {
    "ROE", "ROA", "GROSS_MARGIN", "NET_MARGIN", "OPERATING_MARGIN",
    "REVENUE_GROWTH", "NET_INCOME_GROWTH",
    "OPERATING_CF_MARGIN", "CAPEX_TO_REVENUE",
    "CASH_CONVERSION_RATIO", "RD_TO_REVENUE",
    "NET_DEBT_TO_EQUITY", "FCF_YIELD",
}
_X_CODES = {"ASSET_TURNOVER", "EQUITY_MULTIPLIER"}

_RATIO_LABELS: dict[str, str] = {
    "ROE": "ROE 净资产收益率", "ROA": "ROA 总资产收益率",
    "GROSS_MARGIN": "毛利率", "NET_MARGIN": "净利率", "OPERATING_MARGIN": "营业利润率",
    "CURRENT_RATIO": "流动比率", "QUICK_RATIO": "速动比率", "DEBT_TO_EQUITY": "资产负债率",
    "PE": "PE 市盈率", "PB": "PB 市净率", "PS": "PS 市销率", "EV_EBITDA": "EV/EBITDA",
    "REVENUE_GROWTH": "营收增长率", "NET_INCOME_GROWTH": "净利润增长率",
    "OPERATING_CF_MARGIN": "经营现金流利润率", "FREE_CASH_FLOW": "自由现金流",
    "FCF_PER_SHARE": "每股自由现金流", "CAPEX_TO_REVENUE": "资本支出占比",
    "CASH_CONVERSION_RATIO": "现金流转化率", "RD_TO_REVENUE": "研发投入占比",
    "ASSET_TURNOVER": "资产周转率", "EQUITY_MULTIPLIER": "权益乘数",
    "NET_DEBT_TO_EQUITY": "净负债权益比", "FCF_YIELD": "自由现金流收益率",
}

_RATIOS_MAIN = [
    "ROE", "ROA", "GROSS_MARGIN", "NET_MARGIN", "OPERATING_MARGIN",
    "CURRENT_RATIO", "QUICK_RATIO", "DEBT_TO_EQUITY",
    "PE", "PB", "PS", "EV_EBITDA", "RD_TO_REVENUE",
    "OPERATING_CF_MARGIN", "FREE_CASH_FLOW",
]


def _fmt_ratio(code: str, v: float) -> str:
    if v is None:
        return "—"
    if code in _PCT_CODES:
        return f"{v:.1%}"
    if code in _X_CODES:
        return f"{v:.2f}x"
    if code == "FREE_CASH_FLOW":
        return f"{v:,.0f}"
    return f"{v:.2f}"


def _num(v, default: float | None = None) -> float | None:
    """安全转 float；None / 非数字返回 default（默认 None）。"""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _latest(series: list[dict]):
    if not series:
        return None
    for s in reversed(series):
        if s.get("value") is not None:
            return s["value"]
    return None


def _series(data: dict, key: str) -> list[dict]:
    s = data.get(key) if isinstance(data, dict) else None
    return s or []


def _fmt_cap(cap: float | None, currency: str = "USD") -> str:
    if cap is None:
        return "暂无"
    if currency == "CNY":
        if cap >= 1e12:
            return f"¥{cap/1e12:.2f}万亿"
        if cap >= 1e8:
            return f"¥{cap/1e8:.2f}亿"
        return f"¥{cap:,.0f}"
    if cap >= 1e12:
        return f"${cap/1e12:.2f}T"
    if cap >= 1e9:
        return f"${cap/1e9:.2f}B"
    return f"${cap/1e6:.2f}M"


def _fmt_premium(premium: float | None) -> str:
    if premium is None:
        return "—"
    return f"{premium:+.1%}"


def _trend(series: list[dict]) -> str:
    vals = [s["value"] for s in series if s["value"] is not None]
    if len(vals) < 2:
        return ""
    d = vals[-1] - vals[-2]
    return "↑" if d > 0.005 else ("↓" if d < -0.005 else "→")


# ── 章节构建 ──────────────────────────────────────────────────


def _section_summary(result: dict, fa: dict, va: dict, swot: dict) -> list[str]:
    ci = result.get("company_info", {})
    ticker = result.get("ticker", "")
    name = ci.get("name", ticker)
    currency = ci.get("currency", "USD")
    price = fa.get("current_price")
    lines = ["## 一、执行摘要", ""]

    lines.append(f"- **公司**: {name} ({ticker})")
    lines.append(f"- **行业**: {ci.get('industry') or '暂无'} | **地区**: {ci.get('country') or '暂无'}")
    if price is not None:
        lines.append(f"- **当前价格**: {currency} {price:,.2f}")
    mc = ci.get("market_cap")
    if mc:
        lines.append(f"- **市值**: {_fmt_cap(float(mc), currency)}")

    dcf = va.get("dcf") if isinstance(va.get("dcf"), dict) else None
    rel = va.get("relative_valuation") if isinstance(va.get("relative_valuation"), dict) else None
    if dcf and dcf.get("fair_value") is not None:
        # upside 可能没有：缺现价（或现价是 NaN）时算不出来。缺就标 —，
        # 不能假设它一定存在，否则整份报告渲染就崩在这一个数上。
        up = _num(dcf.get("upside"))
        tail = f"（upside {up * 100:+.1f}%）" if up is not None else "（upside —，缺现价）"
        lines.append(f"- **DCF 内在价值**: {currency} {dcf['fair_value']:,.2f}{tail}")
    if rel and rel.get("pe"):
        pe = rel["pe"]
        pe_c, pe_a = _num(pe.get("company")), _num(pe.get("industry_avg"))
        seg = "- **相对估值 PE**: "
        if pe_c is not None:
            seg += f"公司 {pe_c:.1f}"
        if pe_a is not None:
            seg += f" vs 行业均值 {pe_a:.1f}"
        seg += f"（溢价 {_fmt_premium(pe.get('premium'))}）"
        lines.append(seg)
    rating = swot.get("rating") if isinstance(swot, dict) else None
    if rating:
        lines.append(f"- **SWOT 综合评级**: {rating}")
    elif isinstance(swot, dict) and swot.get("degraded"):
        # 不写出来会让人以为「没评级 = 没这项」，实际是没生成成功
        reason = swot.get("reason") or "LLM 未返回有效结果"
        lines.append(f"- **SWOT 综合评级**: 未生成（{reason}）")
    if result.get("pipeline_time"):
        lines.append(f"- **管线耗时**: {result['pipeline_time']:.1f}s")

    summary = va.get("summary")
    if summary:
        lines += ["", "**估值结论**:", "", str(summary)]
    lines.append("")
    return lines


_MAX_PERIODS = 8


def pe_percentile_scope(vc: dict, pricing: dict | None) -> str:
    """分位句里的口径括注：样本年份 + 算这个分位时的现价日期。

    两者都拿不到时返回空串（退化为改动前的写法，不写「—」占位）。刷新产物里
    分位可能比现价旧，这时必须把两个日期都说出来，否则读者会把旧分位当今日值。
    """
    periods = vc.get("hist_pe_periods")
    span = ""
    if (isinstance(periods, list) and periods
            and all(isinstance(p, str) and p for p in periods)):
        span = (f"样本 {periods[0]}" + (f"–{periods[-1]}" if len(periods) > 1 else "")
                + " 年度均价")

    price_as_of = pricing.get("price_as_of") if isinstance(pricing, dict) else None
    if not isinstance(price_as_of, str) or not price_as_of:
        price_as_of = None
    pct_as_of = vc.get("pe_percentile_as_of")
    if not isinstance(pct_as_of, str) or not pct_as_of:
        pct_as_of = price_as_of

    if pct_as_of and price_as_of and pct_as_of != price_as_of:
        date_part = f"该分位按 {pct_as_of} 收盘价计算，现价已更新至 {price_as_of}"
    elif pct_as_of:
        date_part = f"现价截至 {pct_as_of}"
    else:
        date_part = ""

    bits = [b for b in (span, date_part) if b]
    return f"（{'；'.join(bits)}）" if bits else ""


def _section_financial(fa: dict, pricing: dict | None = None) -> list[str]:
    ratios = fa.get("ratios", {})
    all_periods = fa.get("periods", []) or []
    lines = ["## 二、财务分析", ""]
    if not all_periods:
        lines.append("暂无财务数据。")
        return lines

    # 表格仅展示最近 N 期（年份过多会导致列数超宽），趋势用全量
    periods = all_periods[-_MAX_PERIODS:]
    lines.append(f"覆盖期间: {periods[0]} ~ {periods[-1]}"
                 f"（显示最近 {len(periods)} 期，年报口径）")
    lines.append("")
    lines.append("| 指标 | " + " | ".join(periods) + " | 趋势 |")
    lines.append("| --- | " + " | ".join(["---"] * len(periods)) + " | :--: |")
    for code in _RATIOS_MAIN:
        s = _series(ratios, code)
        if not s:
            continue
        vals = [_fmt_ratio(code, x.get("value")) for x in s[-_MAX_PERIODS:]]
        lines.append(f"| {_RATIO_LABELS.get(code, code)} | " + " | ".join(vals) + f" | {_trend(s)} |")

    gr = fa.get("growth_rates", {})
    growth_rows = []
    for code in ("REVENUE_GROWTH", "NET_INCOME_GROWTH"):
        s = _series(gr, code)
        if s:
            vals = [_fmt_ratio(code, x.get("value")) for x in s[-_MAX_PERIODS:]]
            growth_rows.append(f"| {_RATIO_LABELS.get(code, code)} | " + " | ".join(vals) + f" | {_trend(s)} |")
    if growth_rows:
        lines += ["", "**成长能力**:", "", "| 指标 | " + " | ".join(periods) + " | 趋势 |",
                  "| --- | " + " | ".join(["---"] * len(periods)) + " | :--: |"]
        lines += growth_rows

    # Phase 4：近 4 季度营收/净利趋势 + 同比（A 股，真实季度财报）
    qt = fa.get("quarterly_trend")
    if isinstance(qt, dict):
        if qt.get("available"):
            qps = qt.get("periods", [])
            lines += ["", "**近季度营收/净利趋势**（报告期累计口径）:", "",
                      "| 报告期 | 营收 | 营收同比 | 净利 | 净利同比 |",
                      "| --- | ---: | ---: | ---: | ---: |"]
            for p in qps:
                rev = f"{p['revenue']:,.0f}" if p.get("revenue") is not None else "—"
                ni = f"{p['net_income']:,.0f}" if p.get("net_income") is not None else "—"
                ry = f"{p['revenue_yoy']:+.1%}" if p.get("revenue_yoy") is not None else "—"
                ny = f"{p['net_income_yoy']:+.1%}" if p.get("net_income_yoy") is not None else "—"
                lines.append(f"| {p['period']} ({p.get('report_date', '')}) | {rev} | {ry} | {ni} | {ny} |")
            lines.append(f"> 同比=同报告期 vs 上年同期。{qt.get('source') or ''}")
            lines.append("")
        else:
            lines.append(f"> 季度趋势: {qt.get('reason') or '暂无'}")
            lines.append("")

    fcfh = fa.get("fcf_history")
    if fcfh:
        rows = []
        for item in fcfh[-4:]:
            if isinstance(item, dict):
                p = item.get("period", item.get("fiscal_period", "?"))
                v = item.get("value", item.get("fcf"))
                if v is not None:
                    rows.append(f"- {p}: {v:,.0f}")
            elif isinstance(item, (int, float)):
                rows.append(f"- {item:,.0f}")
        if rows:
            lines += ["", "**自由现金流历史**（最近几期）:", ""] + rows

    # ROIC（近似口径，显式标注）
    roic = fa.get("roic") or []
    roic_rows = []
    for item in roic[-_MAX_PERIODS:]:
        if item.get("roic") is not None:
            roic_rows.append(f"- {item['period']}: ROIC {item['roic']:.1%}")
    if roic_rows:
        lines += ["", "**ROIC**（NOPAT / 经营性投入资本，近似口径）:", ""] + roic_rows

    # 杜邦分解（真实财务数据）
    dup = fa.get("dupont_breakdown") or []
    if dup:
        lines += ["", "**杜邦分解**（ROE = 净利率 × 资产周转率 × 权益乘数）:", "",
                  "| 年份 | ROE | 净利率 | 资产周转 | 权益乘数 |",
                  "| --- | ---: | ---: | ---: | ---: |"]
        for item in dup[-_MAX_PERIODS:]:
            nm = f"{item['net_margin']:.1%}" if item.get("net_margin") is not None else "—"
            at = f"{item['asset_turnover']:.2f}" if item.get("asset_turnover") is not None else "—"
            em = f"{item['equity_multiplier']:.2f}" if item.get("equity_multiplier") is not None else "—"
            roe = f"{item['roe']:.1%}" if item.get("roe") is not None else "—"
            lines.append(f"| {item['period']} | {roe} | {nm} | {at} | {em} |")
        lines.append("")

    # 估值历史分位（近似，显式标注）
    vc = fa.get("valuation_context") or {}
    if vc.get("pe_percentile") is not None:
        scope = pe_percentile_scope(vc, pricing)
        lines.append(f"> 估值分位（近似）: 当前 PE {vc.get('current_pe')} 处于历史区间"
                     f"第 {vc['pe_percentile'] * 100:.0f}% 分位{scope}。{vc.get('note') or ''}")
        lines.append("")
    lines.append("")
    return lines


def _section_valuation(result: dict, va: dict, sotp: dict) -> list[str]:
    ci = result.get("company_info", {})
    currency = ci.get("currency", "USD")
    lines = ["## 三、估值分析", ""]

    dcf = va.get("dcf") if isinstance(va.get("dcf"), dict) else None
    if dcf:
        fv = dcf.get("fair_value")
        if fv is not None:
            lines.append(f"**DCF 内在价值**: {currency} {fv:,.2f}")
            if _num(dcf.get("upside")) is not None:
                lines.append(f"**相对当前价 upside**: {_num(dcf['upside']) * 100:+.1f}%")
            dcf_parts = []
            wacc = _num(dcf.get("wacc_used"))
            if wacc is not None:
                dcf_parts.append(f"WACC {wacc * 100:.2f}%")
            dcf_parts.append(f"预测期 {dcf.get('projection_years') or '?'} 年")
            tg = _num(dcf.get("terminal_growth"))
            if tg is not None:
                dcf_parts.append(f"永续增长率 {tg * 100:.2f}%")
            lines.append(f"- {' | '.join(dcf_parts)}")
            ev = _num(dcf.get("fair_value_ev"))
            pv = _num(dcf.get("pv_terminal"))
            if ev is not None:
                lines.append(f"- 企业价值: {currency} {ev / 1e9:,.1f}B")
            if pv is not None:
                lines.append(f"- 终值现值: {currency} {pv / 1e9:,.1f}B")
        else:
            lines.append(f"**DCF**: 不适用（{dcf.get('reason') or '数据不足'}）")

        # Phase 2：DCF 敏感性矩阵（WACC × 永续增长率 → 每股价值）
        sens = dcf.get("sensitivity") or {}
        wacc_vals = sens.get("wacc_values") or []
        g_vals = sens.get("growth_values") or []
        matrix = sens.get("matrix") or []
        if wacc_vals and g_vals and matrix:
            lines += ["", "**DCF 敏感性**（WACC × 永续增长率 → 每股内在价值）:", "",
                      "| 永续g \\ WACC | "
                      + " | ".join(f"{w * 100:.1f}%" for w in wacc_vals) + " |",
                      "| --- | " + " | ".join(["---"] * len(wacc_vals)) + " |"]
            for j, g in enumerate(g_vals):
                cells = []
                for i in range(len(wacc_vals)):
                    v = matrix[i][j] if i < len(matrix) and j < len(matrix[i]) else None
                    cells.append(f"{v:,.0f}" if v is not None else "—")
                lines.append(f"| {g * 100:.1f}% | " + " | ".join(cells) + " |")
            lines += ["", f"> {sens.get('note') or ''}", ""]

    rel = va.get("relative_valuation") if isinstance(va.get("relative_valuation"), dict) else None
    if rel:
        lines += ["", "**相对估值（vs 行业均值）**:", "",
                  "| 指标 | 公司 | 行业均值 | 溢价 |",
                  "| --- | ---: | ---: | ---: |"]
        for code, label in (("pe", "PE"), ("pb", "PB"), ("ps", "PS"), ("ev_ebitda", "EV/EBITDA")):
            e = rel.get(code)
            if e:
                comp, avg = _num(e.get("company")), _num(e.get("industry_avg"))
                comp_s = f"{comp:.2f}" if comp is not None else "—"
                avg_s = f"{avg:.2f}" if avg is not None else "—"
                lines.append(f"| {label} | {comp_s} | {avg_s} | {_fmt_premium(e.get('premium'))} |")
        lines.append("")

    sp = va.get("supply_chain_risk_premium")
    if sp:
        lines.append(f"**供应链风险溢价**: +{sp * 10000:.0f}bp")

    # 估算参数来源（免费源真实数据优先，估算显式标注）
    est = va.get("estimates") or {}
    est_lines = []
    for key, label in (("risk_free_rate", "无风险利率"), ("beta", "Beta"),
                       ("erp", "市场风险溢价(ERP)"), ("industry_pe", "行业PE基准")):
        e = est.get(key)
        if isinstance(e, dict) and e.get("source"):
            val_s = f"{e['value']:.4f}" if e.get("value") is not None else "—"
            est_lines.append(f"- **{label}**: {val_s}（{e.get('source')}，置信度 {e.get('confidence') or '—'}）")
    if est_lines:
        lines += ["", "**估算参数来源**（不静默编造，缺失显式标注）:", ""] + est_lines + [""]

    if sotp and sotp.get("segments"):
        pl, ph = sotp.get("per_share_low"), sotp.get("per_share_high")
        # 缺现价时摊不到每股，但分部表本身有值；不能因为算不出 per-share
        # 就把整张表连同「模型估计」的声明一起吞掉。
        if pl is not None and ph is not None:
            lines += ["", f"**SOTP 分部估值区间**: {currency} {pl:,.2f} ~ {ph:,.2f}"]
        lines += ["", "| 分部 | 类型 | 收入占比 | 净利润占比 | 低估值 | 高估值 |",
                  "| --- | --- | ---: | ---: | ---: | ---: |"]
        for seg in sotp.get("segments", []):
            rs = _num(seg.get("revenue_share"))
            ns = _num(seg.get("net_income_share"))
            vl = _num(seg.get("value_low"))
            vh = _num(seg.get("value_high"))
            rs_s = f"{rs * 100:.0f}%" if rs is not None else "—"
            ns_s = f"{ns * 100:.0f}%" if ns is not None else "—"
            vl_s = f"{currency} {vl / 1e9:,.1f}B" if vl is not None else "—"
            vh_s = f"{currency} {vh / 1e9:,.1f}B" if vh is not None else "—"
            lines.append(f"| {seg.get('name', '?')} | {seg.get('segment_type') or '—'} | "
                         f"{rs_s} | {ns_s} | {vl_s} | {vh_s} |")
        # 占比是模型拆的，不是披露值。不标这一句，读者会把估算当财报口径。
        lines += ["", "> 注：分部占比为模型估计，非公司披露口径。", ""]
    lines.append("")
    return lines


def _section_market(result: dict, ma: dict, comp: dict) -> list[str]:
    lines = ["## 四、市场与竞争", ""]
    conc = ma.get("industry_concentration") or {}
    cr3, cr5, hhi = _num(conc.get("cr3")), _num(conc.get("cr5")), _num(conc.get("hhi"))
    has_conc = any(v is not None for v in (cr3, cr5, hhi))
    ms = ma.get("market_share")
    if ms is not None:
        label = "市场份额" if has_conc else "份额（已知同业样本内）"
        lines.append(f"- **{label}**: {ms * 100:.1f}%")
    if has_conc:
        parts = []
        if cr3 is not None:
            parts.append(f"CR3 {cr3 * 100:.1f}%")
        if cr5 is not None:
            parts.append(f"CR5 {cr5 * 100:.1f}%")
        if hhi is not None:
            parts.append(f"HHI {hhi:.0f}")
        lines.append(f"- **行业集中度**: {' | '.join(parts)}")
    elif conc.get("note"):
        lines.append(f"- **行业集中度**: {conc['note']}")
    tam = ma.get("tam_estimate")
    if tam:
        lines.append(f"- **TAM 市场规模**: ${tam / 1e9:,.0f}B")

    global_m = ma.get("markets", {}).get("global", {})
    comp_data = global_m.get("competitor_data", [])
    company = ma.get("company", {})
    if comp_data or company.get("revenue"):
        share_head = "份额" if has_conc else "份额（样本内）"
        lines += ["", "**主要竞争对手营收对比**:", "",
                  f"| 公司 | 营收 (USD) | {share_head} |",
                  "| --- | ---: | ---: |"]
        for c in comp_data[:8]:
            name = c.get("name", "?")
            rev = _num(c.get("revenue"))
            share = _num(c.get("share"))
            rev_s = f"${rev / 1e9:,.1f}B" if rev is not None else "—"
            share_s = f"{share * 100:.1f}%" if share is not None else "—"
            lines.append(f"| {name} | {rev_s} | {share_s} |")
        lines.append("")

    comparison = comp.get("comparison") if isinstance(comp, dict) else None
    if isinstance(comparison, dict) and comparison:
        lines += ["", "**竞品对比**:", ""]
        for k, v in comparison.items():
            if isinstance(v, (str, int, float)):
                lines.append(f"- **{k}**: {v}")
            elif isinstance(v, list):
                lines.append(f"- **{k}**: {'、'.join(str(x) for x in v)}")
        lines.append("")
    lines.append("")
    return lines


def _section_news(news: dict) -> list[str]:
    """新闻舆情分析章节。available=False 时如实渲染"暂无"，不编造。"""
    lines = ["## 五、新闻舆情", ""]
    if not isinstance(news, dict) or not news.get("available"):
        reason = news.get("reason") if isinstance(news, dict) else ""
        lines.append(f"暂无新闻/公告舆情分析。{('（' + reason + '）') if reason else ''}")
        lines.append("")
        return lines

    lines.append(f"- **数据源**: {news.get('source') or '—'}")
    lines.append(f"- **新闻**: {news.get('article_count') or 0} 条"
                 f" | **业绩说明会/公告**: {news.get('briefing_count') or 0} 条")
    sent = news.get("sentiment")
    score = _num(news.get("sentiment_score"))
    seg = "- **市场情绪**: "
    if sent:
        seg += str(sent)
        if score is not None:
            seg += f"（{score:+.2f}）"
    elif score is not None:
        seg += f"{score:+.2f}"
    else:
        seg += "—"
    lines.append(seg)
    lines.append("")

    drivers = news.get("key_drivers") or []
    if drivers:
        lines += ["**关键驱动因素**:", ""]
        for d in drivers:
            if isinstance(d, dict):
                drv = str(d.get("driver") or d.get("point") or "").strip()
                ev = d.get("evidence")
                ln = f"- **{drv}**" if drv else "- —"
                if ev:
                    ln += f"（依据: {ev}）"
                lines.append(ln)
            else:
                lines.append(f"- {d}")
        lines.append("")

    ris = news.get("risks_opportunities") or []
    if ris:
        lines += ["**风险与机会**:", ""]
        for r in ris:
            if isinstance(r, dict):
                typ = r.get("type")
                point = str(r.get("point") or "").strip()
                ev = r.get("evidence")
                tag = {"利好": "机会", "风险": "风险"}.get(typ, typ or "事项")
                ln = f"- **{tag}**: {point}" if point else f"- **{tag}**"
                if ev:
                    ln += f"（依据: {ev}）"
                lines.append(ln)
            else:
                lines.append(f"- {r}")
        lines.append("")

    articles = news.get("articles") or []
    if articles:
        lines += ["**近期要闻（最近几条）**:", ""]
        for a in articles[:5]:
            if isinstance(a, dict):
                title = (a.get("title") or "").strip()
                if not title:
                    continue
                prefix = f"[{a.get('published') or ''}] " if a.get("published") else ""
                suffix = f"（{a.get('source') or ''}）" if a.get("source") else ""
                lines.append(f"- {prefix}{title}{suffix}")
        lines.append("")

    summary = news.get("summary")
    if summary:
        lines += ["**舆情摘要**:", "", str(summary), ""]
    return lines


def _section_meeting(meeting: dict) -> list[str]:
    """股东大会分析章节。available=False 时如实渲染"暂无"，不编造。"""
    lines = ["## 六、股东大会", ""]
    if not isinstance(meeting, dict) or not meeting.get("available"):
        reason = meeting.get("reason") if isinstance(meeting, dict) else ""
        lines.append(f"暂无股东大会信息。{('（' + reason + '）') if reason else ''}")
        lines.append("")
        return lines

    lines.append(f"- **数据源**: {meeting.get('source') or '—'}")
    lines.append(f"- **会议场次**: {meeting.get('meeting_count') or 0} 场")
    lines.append("")

    meetings = meeting.get("meetings") or []
    if meetings:
        lines += ["**近期股东大会**:", ""]
        for m in meetings[:5]:
            if not isinstance(m, dict):
                continue
            name = m.get("name") or m.get("title") or "股东大会"
            date = m.get("start_date") or ""
            head = f"- **{name}**" if not date else f"- **{name}**（{date}）"
            lines.append(head)
            dates = []
            if m.get("record_date"):
                dates.append(f"股权登记日 {m['record_date']}")
            if m.get("resolution_date"):
                dates.append(f"决议公告日 {m['resolution_date']}")
            if dates:
                lines.append(f"  - {' | '.join(dates)}")
            if m.get("proposals"):
                lines.append(f"  - 提案: {m['proposals']}")
        lines.append("")

    props = meeting.get("key_proposals") or []
    if props:
        lines += ["**重大提案解读**:", ""]
        for p in props:
            if isinstance(p, dict):
                proposal = str(p.get("proposal") or "").strip()
                sig = p.get("significance")
                ln = f"- **{proposal}**" if proposal else "- —"
                if sig:
                    ln += f" —— {sig}"
                lines.append(ln)
            else:
                lines.append(f"- {p}")
        lines.append("")

    signals = meeting.get("governance_signals")
    if signals:
        lines += ["**治理信号**:", "", str(signals), ""]

    risks = meeting.get("governance_risks") or []
    if risks:
        lines += ["**治理风险**:", ""]
        for r in risks:
            lines.append(f"- {r}")
        lines.append("")

    summary = meeting.get("summary")
    if summary:
        lines += ["**会议评估**:", "", str(summary), ""]
    return lines


def _section_swot(swot: dict) -> list[str]:
    lines = ["## 七、SWOT 分析", ""]
    if not isinstance(swot, dict):
        lines.append("暂无 SWOT 分析。")
        return lines
    rating = swot.get("rating")
    summary = swot.get("summary")
    if rating:
        lines.append(f"**综合评级**: {rating}")
    if summary:
        lines.append("")
        lines.append(f"**总结**: {summary}")
    data = swot.get("swot", {})
    if not data:
        lines.append("")
        lines.append("暂无 SWOT 条目。")
        return lines
    for key, label in (("strengths", "优势 Strengths"), ("weaknesses", "劣势 Weaknesses"),
                       ("opportunities", "机会 Opportunities"), ("threats", "威胁 Threats")):
        items = data.get(key, [])
        lines += ["", f"**{label}**:", ""]
        for it in items:
            if isinstance(it, dict):
                point = (it.get("point") or it.get("title") or
                         it.get("detail") or it.get("description") or "").strip()
                source = it.get("source")
                ln = f"- {point}" if point else "- —"
                if source:
                    ln += f"（来源: {source}）"
                lines.append(ln)
            else:
                lines.append(f"- {it}")
    lines.append("")
    return lines


def _section_risk(result: dict, geo: dict, sc: dict, fc: dict) -> list[str]:
    lines = ["## 八、风险分析", ""]

    ts = geo.get("tariff_sensitivity") if isinstance(geo, dict) else None
    if isinstance(ts, dict) and ts.get("applicable"):
        lines.append("**关税敏感性**:")
        lines.append(f"- 情景: {ts.get('scenario') or '—'}")
        if ts.get("china_exposure") is not None:
            lines.append(f"- 中国敞口: {ts['china_exposure'] * 100:.1f}%")
        if ts.get("gross_margin_impact") is not None:
            lines.append(f"- 毛利率影响: -{ts['gross_margin_impact'] * 100:.1f}pp")
        if ts.get("description"):
            lines.append(f"- 说明: {ts['description']}")
        lines.append("")

    ge = geo.get("geopolitical_events") if isinstance(geo, dict) else {}
    events = ge.get("events", []) if isinstance(ge, dict) else []
    if events:
        lines.append(f"**地缘政治事件**（风险等级: {ge.get('risk_level') or 'N/A'}）:")
        for ev in events:
            lines.append(f"- **{ev.get('event') or ev.get('name')}**（{ev.get('region') or '—'}，"
                         f"概率 {ev.get('probability') or '—'}，模型推断）")
            if ev.get("impact"):
                lines.append(f"  - 影响: {ev['impact']}")
            if ev.get("transmission_path"):
                lines.append(f"  - 传导: {ev['transmission_path']}")
        # 事件与概率都是模型推演，何况常带未来年份。不标出来会被当成既成事实读。
        lines += ["", "> 注：以上地缘事件及其概率为模型情景推演，非事实陈述。", ""]

    if isinstance(sc, dict):
        sc_sum = sc.get("summary")
        if sc_sum:
            lines.append("**供应链**:")
            lines.append(f"- {sc_sum}")
        sup_conc = sc.get("supplier_concentration")
        if isinstance(sup_conc, dict):
            lines.append(f"- 供应商集中度: {sup_conc.get('rating') or '—'}。{sup_conc.get('description') or ''}")
        ds = sc.get("disruption_scenarios")
        if isinstance(ds, dict) and ds.get("scenarios"):
            lines.append("- 中断情景:")
            for scn in ds.get("scenarios", []):
                prob = _num(scn.get("probability"))
                prob_s = f"概率 {prob * 100:.0f}%" if prob is not None else "概率 —"
                lines.append(f"  - {scn.get('name')}（{prob_s}）: {scn.get('description') or ''}")
        lines.append("")

    if isinstance(fc, dict):
        warnings = fc.get("warnings", [])
        if warnings:
            lines.append("**事实核查警告**:")
            for w in warnings:
                lines.append(f"- {w}")
        lines.append("")
    return lines


def _section_knowledge(knowledge: dict) -> list[str]:
    """知识库参考章节（RAG 检索片段）。available=False 时如实渲染「暂无」，不编造。"""
    lines = ["## 九、知识库参考", ""]
    if not isinstance(knowledge, dict) or not knowledge.get("available"):
        reason = knowledge.get("reason") if isinstance(knowledge, dict) else ""
        lines.append(f"暂无知识库参考内容。{('（' + reason + '）') if reason else ''}")
        lines.append("")
        return lines

    lines.append(f"- **数据源**: {knowledge.get('source') or '—'}")
    lines.append(f"- **检索片段**: {knowledge.get('count') or 0} 条")
    lines.append("")

    snippets = knowledge.get("snippets") or []
    for i, s in enumerate(snippets, 1):
        if not isinstance(s, dict):
            continue
        meta = s.get("metadata") or {}
        kind = str(meta.get("kind") or "")
        section = str(meta.get("section") or "")
        # 研报 source 是相对路径（data/reports/xxx.md），只保留文件名
        source = str(meta.get("source") or "").replace("\\", "/").split("/")[-1]
        date = str(meta.get("date") or "")[:10]
        score = _num(meta.get("score"))

        head = f"**{i}. "
        head += section or kind or "片段"
        head += "**"
        tags = [t for t in (kind, source, date) if t]
        if tags:
            head += f"（{' | '.join(tags)}）"
        if score is not None:
            head += f" 相关度 {score:.3f}"
        lines.append(head)

        content = str(s.get("content") or "").strip().replace("\n", " ")
        if content:
            shown = content if len(content) <= 160 else content[:160] + "…"
            lines.append(f"  > {shown}")
        lines.append("")
    return lines


def _section_trade_plan(tp: dict, charts: dict | None = None) -> list[str]:
    """投资策略与交易信号章节。可用性为假时如实渲染"暂无"，不编造。

    策略文字含「进场/止损位/止盈位/仓位」等词，命中合规检测时落复核队列并
    附一行合规提示（沿用 apply_gate 机制，仅精简文案避免与尾部免责重复堆叠）。
    charts 含 price_action 时章节内嵌 K线+均线叠加图。
    """
    lines = ["## 十、投资策略与交易信号", ""]
    if not isinstance(tp, dict) or not tp.get("available"):
        reason = tp.get("reason") if isinstance(tp, dict) else ""
        lines.append(f"暂无投资策略与交易信号。{('（' + reason + '）') if reason else ''}")
        lines.append("")
        return lines

    cur = tp.get("current_price")

    lines.append(f"- **综合评级**: {tp.get('rating') or '—'}")
    lines.append(f"- **仓位建议**: {tp.get('position_hint') or '—'}")
    if tp.get("confidence_level"):
        lines.append(f"- **信号置信度**: {tp.get('confidence_level')}（技术/估值方向一致性）")
    rr = tp.get("risk_reward_ratio")
    if rr is not None:
        lines.append(f"- **风险收益比**: {rr:.2f}（止盈/止损空间比）")
    if cur is not None:
        lines.append(f"- **当前价格**: {cur:,.2f}")
    wt = tp.get("weekly_trend")
    if wt:
        ind_trend = (tp.get("indicators") or {}).get("trend") or "—"
        lines.append(f"- **周期共振**: 日线{ind_trend} vs 周线{wt}（{tp.get('weekly_resonance') or '—'}）")

    lines += ["", "**交易计划**:", "",
              "| 项目 | 数值 |",
              "| --- | ---: |"]
    ez = tp.get("entry_zone") or [None, None]
    ez_low, ez_high = ez[0], ez[1] if len(ez) > 1 else None
    rows = [
        ("进场区间", f"{ez_low:,.2f} ~ {ez_high:,.2f}"
         if ez_low is not None and ez_high is not None else "—"),
        ("止损位", f"{tp.get('stop_loss'):,.2f}" if tp.get("stop_loss") is not None else "—"),
        ("止盈位", f"{tp.get('take_profit'):,.2f}" if tp.get("take_profit") is not None else "—"),
    ]
    for k, v in rows:
        lines.append(f"| {k} | {v} |")
    lines.append("")

    # K线 + 均线叠加图（charts 中由管线 price_history 生成）
    if charts and charts.get("price_action"):
        fname = charts["price_action"].replace("\\", "/").split("/")[-1]
        lines.append("![日K线与均线叠加（含成交量副图）](charts/" + fname + ")")
        lines.append("")

    reasons = tp.get("reasons") or []
    if reasons:
        lines += ["**依据（引用真实指标数值）**:", ""]
        for r in reasons:
            lines.append(f"- {r}")
        lines.append("")

    lines.append("")

    # 合规：命中投资建议词 → 落复核队列 + 附一行提示
    body = "\n".join(lines)
    flagged, hits = check_advice(body)
    if flagged:
        try:
            from app.compliance.queue import flag as queue_flag
            queue_flag(source="analysis_report_trade_plan",
                       content_hash=content_hash(body),
                       snippet=body[:200], hit_words=hits, advice_flag=1)
        except Exception:  # noqa: BLE001 合规落库失败不影响报告生成
            pass
        lines.append("> ⚠️ 合规提示：以上含量化交易信号表述，仅供参考，不构成任何投资建议。"
                     "投资有风险，入市需谨慎。")
        lines.append("")
    return lines


def _section_charts(charts: dict) -> list[str]:
    if not charts:
        return []
    lines = ["## 附录：图表", ""]
    for key, path in charts.items():
        if key == "price_action":  # 已在投资策略章节内嵌，避免重复
            continue
        fname = path.replace("\\", "/").split("/")[-1]
        lines.append(f"### {key}")
        lines.append(f"![{key}](charts/{fname})")
        lines.append("")
    return lines


def result_to_markdown(result: dict, charts: dict | None = None) -> str:
    """将分析管线结果转为结构化 Markdown 报告。"""
    charts = charts or {}
    ci = result.get("company_info", {})
    ticker = result.get("ticker", "")
    name = ci.get("name", ticker)
    fa = result.get("financial_analysis", {})
    va = result.get("valuation_analysis", {})
    sotp = result.get("sotp_analysis", {})
    ma = result.get("market_analysis", {})
    comp = result.get("competitive_analysis", {})
    news = result.get("news_analysis", {})
    meeting = result.get("meeting_analysis", {})
    swot = result.get("swot_analysis", {})
    geo = result.get("geopolitical_analysis", {})
    sc = result.get("supply_chain_analysis", {})
    fc = result.get("fact_checks", {})
    knowledge = result.get("knowledge", {})

    parts = [
        f"# 深度研究报告：{name}（{ticker}）",
        "",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ 管线耗时: "
        f"{result.get('pipeline_time') or '—'}s",
        "",
        *(_section_summary(result, fa, va, swot)),
        *(_section_financial(fa, result.get("pricing"))),
        *(_section_valuation(result, va, sotp)),
        *(_section_market(result, ma, comp)),
        *(_section_news(news)),
        *(_section_meeting(meeting)),
        *(_section_swot(swot)),
        *(_section_risk(result, geo, sc, fc)),
        *(_section_knowledge(knowledge)),
        *(_section_trade_plan(result.get("trade_plan") or {}, charts)),
        *(_section_charts(charts)),
        "---",
        "",
        f"*{DATA_SOURCE_DISCLAIMER}*",
        "",
        "*本报告由系统基于真实数据源与真实计算自动生成，仅供参考，不构成投资建议。*",
    ]
    return "\n".join(parts)
