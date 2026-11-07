"""PDF 报告生成 —— reportlab 实现。

两部分：
  1. result_to_pdf(): 分析管线结果 → 结构化深度研报 PDF（封面/章节/图表/页脚）。
  2. markdown_to_pdf(): 通用 Markdown（如 Violetz 对话报告）→ PDF。

中文字体统一走 app.report.fonts.reportlab_fonts()；缺失时回退，报告仍可生成。
"""
from __future__ import annotations

import re
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
    Image as RLImage, PageBreak, HRFlowable,
)

from app.compliance.rules import check_advice, content_hash
from app.report.fonts import reportlab_fonts
from app.report.markdown import (
    _RATIOS_MAIN, _RATIO_LABELS, _fmt_ratio, _latest,
    _series, _fmt_cap, _trend, pe_percentile_scope,
)

A4_W, A4_H = A4
MARGIN_LEFT = MARGIN_RIGHT = 20 * mm
MARGIN_TOP = 22 * mm
MARGIN_BOTTOM = 20 * mm

_MAX_PERIODS = 8

C_PRIMARY = colors.HexColor("#1e3a5f")
C_ACCENT = colors.HexColor("#2563eb")
C_GRAY = colors.HexColor("#666666")
C_LIGHT_GRAY = colors.HexColor("#f3f4f6")
C_BORDER = colors.HexColor("#d1d5db")
C_WHITE = colors.white
C_BLACK = colors.black


def _styles(fonts: dict[str, str]) -> dict[str, ParagraphStyle]:
    h, b, t = fonts.get("heading"), fonts.get("body"), fonts.get("title")
    return {
        "cover_title": ParagraphStyle("CoverTitle", fontName=t, fontSize=26, leading=36,
                                      alignment=TA_CENTER, textColor=C_PRIMARY),
        "cover_sub": ParagraphStyle("CoverSub", fontName=b, fontSize=13, leading=20,
                                    alignment=TA_CENTER, textColor=C_GRAY),
        "cover_info": ParagraphStyle("CoverInfo", fontName=b, fontSize=10.5, leading=17,
                                     alignment=TA_CENTER, textColor=C_GRAY),
        "h1": ParagraphStyle("H1", fontName=h, fontSize=17, leading=24,
                             spaceBefore=16, spaceAfter=8, textColor=C_PRIMARY),
        "h2": ParagraphStyle("H2", fontName=h, fontSize=13, leading=19,
                             spaceBefore=10, spaceAfter=5, textColor=C_PRIMARY),
        "body": ParagraphStyle("Body", fontName=b, fontSize=10.5, leading=16,
                               alignment=TA_JUSTIFY, spaceAfter=4),
        "small": ParagraphStyle("Small", fontName=b, fontSize=8.5, leading=13,
                                textColor=C_GRAY, spaceBefore=2, spaceAfter=4),
        "th": ParagraphStyle("TH", fontName=h, fontSize=9, alignment=TA_CENTER,
                             textColor=C_WHITE),
        "td": ParagraphStyle("TD", fontName=b, fontSize=9, alignment=TA_CENTER),
        "tdl": ParagraphStyle("TDL", fontName=b, fontSize=9, alignment=TA_LEFT, leading=13),
    }


def _table_style(head_font: str, body_font: str) -> list:
    return [
        ("BACKGROUND", (0, 0), (-1, 0), C_PRIMARY),
        ("TEXTCOLOR", (0, 0), (-1, 0), C_WHITE),
        ("FONTNAME", (0, 0), (-1, 0), head_font),
        ("FONTNAME", (0, 1), (-1, -1), body_font),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, C_BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_WHITE, C_LIGHT_GRAY]),
    ]


def _clean_cell(text) -> str:
    """转义 HTML 特殊字符，同时把 **加粗** 转成 reportlab 段落 <b> 标签。"""
    s = str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    return s


def _escape(text) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── 深度研报 PDF ──────────────────────────────────────────────


def _build_cover(story, fonts, st, result: dict):
    ci = result.get("company_info", {})
    ticker = result.get("ticker", "")
    name = ci.get("name", ticker)
    story.append(Spacer(1, 60))
    story.append(Paragraph(f"深度研究报告", st["cover_title"]))
    story.append(Spacer(1, 16))
    story.append(Paragraph(f"{_escape(name)}（{_escape(ticker)}）", st["cover_sub"]))
    story.append(Spacer(1, 6))
    lines = [f"行业: {_escape(ci.get('industry') or '暂无')}",
             f"地区: {_escape(ci.get('country') or '暂无')}",
             f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    if result.get("pipeline_time"):
        lines.append(f"管线耗时: {result['pipeline_time']:.1f}s")
    for ln in lines:
        story.append(Paragraph(ln, st["cover_info"]))
    story.append(Spacer(1, 30))
    story.append(HRFlowable(width="40%", thickness=1, color=C_ACCENT,
                            hAlign="CENTER", spaceAfter=20))
    story.append(Paragraph("基于真实数据源与真实计算自动生成", st["cover_info"]))


def _section_summary(story, st, result: dict, fa: dict, va: dict, swot: dict):
    ci = result.get("company_info", {})
    currency = ci.get("currency", "USD")
    story.append(Paragraph("一、执行摘要", st["h1"]))
    price = fa.get("current_price")
    bullets = []
    if price is not None:
        bullets.append(f"当前价格: {currency} {price:,.2f}")
    mc = ci.get("market_cap")
    if mc:
        bullets.append(f"市值: {_fmt_cap(float(mc), currency)}")
    dcf = va.get("dcf") if isinstance(va.get("dcf"), dict) else None
    if dcf and dcf.get("fair_value") is not None:
        ups = _num(dcf.get("upside"))
        ups_s = f"（upside {ups * 100:+.1f}%）" if ups is not None else ""
        bullets.append(f"DCF 内在价值: {currency} {dcf['fair_value']:,.2f}{ups_s}")
    rel = va.get("relative_valuation") if isinstance(va.get("relative_valuation"), dict) else None
    if rel and rel.get("pe"):
        pe = rel["pe"]
        pe_c, pe_a = _num(pe.get("company")), _num(pe.get("industry_avg"))
        pe_s = "相对估值 PE: "
        if pe_c is not None:
            pe_s += f"公司 {pe_c:.1f}"
        if pe_a is not None:
            pe_s += f" vs 行业均值 {pe_a:.1f}"
        pe_s += f"（溢价 {_fmt_premium(pe.get('premium'))}）"
        bullets.append(pe_s)
    rating = swot.get("rating") if isinstance(swot, dict) else None
    if rating:
        bullets.append(f"SWOT 综合评级: {rating}")
    for b in bullets:
        story.append(Paragraph(f"• {_escape(b)}", st["body"]))
    summary = va.get("summary")
    if summary:
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"估值结论: {_escape(summary)}", st["body"]))


def _fmt_premium(p: float | None) -> str:
    return "—" if p is None else f"{p:+.1%}"


def _num(v, default: float | None = None) -> float | None:
    """安全转 float；None / 非数字返回 default（默认 None）。"""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _section_financial(story, st, fa: dict, charts: dict | None = None,
                       pricing: dict | None = None):
    ratios = fa.get("ratios", {})
    all_periods = fa.get("periods", []) or []
    story.append(Paragraph("二、财务分析", st["h1"]))
    if not all_periods:
        story.append(Paragraph("暂无财务数据。", st["body"]))
        return
    # 表格仅展示最近 N 期（年份过多会导致列数超宽），趋势用全量
    periods = all_periods[-_MAX_PERIODS:]
    story.append(Paragraph(f"覆盖期间: {periods[0]} ~ {periods[-1]}"
                           f"（显示最近 {len(periods)} 期，年报口径）", st["body"]))
    header = [Paragraph("指标", st["th"])] + [Paragraph(_escape(p), st["th"]) for p in periods] + [Paragraph("趋势", st["th"])]
    rows = [header]
    for code in _RATIOS_MAIN:
        s = _series(ratios, code)
        if not s:
            continue
        row = [Paragraph(_escape(_RATIO_LABELS.get(code, code)), st["tdl"])]
        row += [Paragraph(_fmt_ratio(code, x.get("value")), st["td"]) for x in s[-_MAX_PERIODS:]]
        row.append(Paragraph(_trend(s), st["td"]))
        rows.append(row)
    if len(rows) > 1:
        story.append(Table(rows, repeatRows=1, style=_table_style(*_st_fonts(st))))

    gr = fa.get("growth_rates", {})
    grow_rows = [header]
    for code in ("REVENUE_GROWTH", "NET_INCOME_GROWTH"):
        s = _series(gr, code)
        if s:
            row = [Paragraph(_escape(_RATIO_LABELS.get(code, code)), st["tdl"])]
            row += [Paragraph(_fmt_ratio(code, x.get("value")), st["td"]) for x in s[-_MAX_PERIODS:]]
            row.append(Paragraph(_trend(s), st["td"]))
            grow_rows.append(row)
    if len(grow_rows) > 1:
        story.append(Spacer(1, 6))
        story.append(Paragraph("成长能力", st["h2"]))
        story.append(Table(grow_rows, repeatRows=1, style=_table_style(*_st_fonts(st))))

    # Phase 4：近 4 季度营收/净利趋势 + 同比（A 股，真实季度财报）
    qt = fa.get("quarterly_trend")
    if isinstance(qt, dict):
        if qt.get("available"):
            story.append(Spacer(1, 6))
            story.append(Paragraph("近季度营收/净利趋势（报告期累计口径）", st["h2"]))
            qrows = [[Paragraph(x, st["th"]) for x in ("报告期", "营收", "营收同比", "净利", "净利同比")]]
            for p in qt.get("periods", []):
                rev = f"{p['revenue']:,.0f}" if p.get("revenue") is not None else "—"
                ni = f"{p['net_income']:,.0f}" if p.get("net_income") is not None else "—"
                ry = f"{p['revenue_yoy']:+.1%}" if p.get("revenue_yoy") is not None else "—"
                ny = f"{p['net_income_yoy']:+.1%}" if p.get("net_income_yoy") is not None else "—"
                qrows.append([Paragraph(_escape(f"{p['period']} ({p.get('report_date','')})"), st["td"]),
                              Paragraph(_escape(rev), st["td"]), Paragraph(_escape(ry), st["td"]),
                              Paragraph(_escape(ni), st["td"]), Paragraph(_escape(ny), st["td"])])
            story.append(Table(qrows, repeatRows=1, style=_table_style(*_st_fonts(st))))
            story.append(Paragraph(f"同比=同报告期 vs 上年同期。{qt.get('source') or ''}", st["small"]))
        elif qt.get("reason"):
            story.append(Spacer(1, 4))
            story.append(Paragraph(f"季度趋势: {qt.get('reason')}", st["small"]))

    # ROIC（近似口径，显式标注）
    roic = fa.get("roic") or []
    roic_lines = [f"{item['period']}: ROIC {item['roic']:.1%}"
                  for item in roic[-_MAX_PERIODS:] if item.get("roic") is not None]
    if roic_lines:
        story.append(Spacer(1, 6))
        story.append(Paragraph("ROIC（NOPAT / 经营性投入资本，近似口径）", st["h2"]))
        for line in roic_lines:
            story.append(Paragraph(f"• {line}", st["body"]))

    # 杜邦分解（真实财务数据）
    dup = fa.get("dupont_breakdown") or []
    if dup:
        story.append(Spacer(1, 6))
        story.append(Paragraph("杜邦分解（ROE = 净利率 × 资产周转率 × 权益乘数）", st["h2"]))
        rows = [[Paragraph(x, st["th"]) for x in ("年份", "ROE", "净利率", "资产周转", "权益乘数")]]
        for item in dup[-_MAX_PERIODS:]:
            nm = f"{item['net_margin']:.1%}" if item.get("net_margin") is not None else "—"
            at = f"{item['asset_turnover']:.2f}" if item.get("asset_turnover") is not None else "—"
            em = f"{item['equity_multiplier']:.2f}" if item.get("equity_multiplier") is not None else "—"
            roe = f"{item['roe']:.1%}" if item.get("roe") is not None else "—"
            rows.append([Paragraph(item["period"], st["td"]), Paragraph(roe, st["td"]),
                         Paragraph(nm, st["td"]), Paragraph(at, st["td"]), Paragraph(em, st["td"])])
        story.append(Table(rows, repeatRows=1, style=_table_style(*_st_fonts(st))))

    # 估值历史分位（近似，显式标注）
    vc = fa.get("valuation_context") or {}
    if vc.get("pe_percentile") is not None:
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"估值分位（近似）: 当前 PE {vc.get('current_pe')} 处于历史区间"
            f"第 {vc['pe_percentile'] * 100:.0f}% 分位"
            f"{pe_percentile_scope(vc, pricing)}。{vc.get('note') or ''}", st["small"]))

    # Phase 2：杜邦分解可视化（报告 PNG）
    if charts and charts.get("dupont"):
        story.append(Spacer(1, 6))
        story.append(Paragraph("杜邦分解可视化（对数瀑布 + ROE 趋势）", st["h2"]))
        story.append(RLImage(charts["dupont"], width=150 * mm, height=102 * mm))


def _st_fonts(st):
    return st["th"].fontName, st["td"].fontName


def _section_valuation(story, st, result: dict, va: dict, sotp: dict,
                       charts: dict | None = None):
    ci = result.get("company_info", {})
    currency = ci.get("currency", "USD")
    story.append(Paragraph("三、估值分析", st["h1"]))
    dcf = va.get("dcf") if isinstance(va.get("dcf"), dict) else None
    if dcf:
        fv = dcf.get("fair_value")
        if fv is not None:
            ups = _num(dcf.get("upside"))
            ups_s = f"（upside {ups * 100:+.1f}%）" if ups is not None else ""
            story.append(Paragraph(f"DCF 内在价值: {currency} {fv:,.2f}{ups_s}", st["body"]))
            dcf_parts = []
            wacc = _num(dcf.get("wacc_used"))
            if wacc is not None:
                dcf_parts.append(f"WACC {wacc * 100:.2f}%")
            dcf_parts.append(f"预测期 {dcf.get('projection_years') or '?'} 年")
            tg = _num(dcf.get("terminal_growth"))
            if tg is not None:
                dcf_parts.append(f"永续增长率 {tg * 100:.2f}%")
            story.append(Paragraph("• " + " | ".join(dcf_parts), st["body"]))
        else:
            story.append(Paragraph(f"DCF: 不适用（{_escape(dcf.get('reason') or '数据不足')}）", st["body"]))
    rel = va.get("relative_valuation") if isinstance(va.get("relative_valuation"), dict) else None
    if rel:
        story.append(Spacer(1, 4))
        story.append(Paragraph("相对估值（vs 行业均值）", st["h2"]))
        rows = [[Paragraph(x, st["th"]) for x in ("指标", "公司", "行业均值", "溢价")]]
        for code, label in (("pe", "PE"), ("pb", "PB"), ("ps", "PS"), ("ev_ebitda", "EV/EBITDA")):
            e = rel.get(code)
            if e:
                comp, avg = _num(e.get("company")), _num(e.get("industry_avg"))
                comp_s = f"{comp:.2f}" if comp is not None else "—"
                avg_s = f"{avg:.2f}" if avg is not None else "—"
                rows.append([Paragraph(label, st["tdl"]),
                             Paragraph(comp_s, st["td"]),
                             Paragraph(avg_s, st["td"]),
                             Paragraph(_fmt_premium(e.get("premium")), st["td"])])
        story.append(Table(rows, style=_table_style(*_st_fonts(st))))

    # 估算参数来源（免费源真实数据优先，估算显式标注）
    est = va.get("estimates") or {}
    est_lines = []
    for key, label in (("risk_free_rate", "无风险利率"), ("beta", "Beta"),
                       ("erp", "市场风险溢价(ERP)"), ("industry_pe", "行业PE基准")):
        e = est.get(key)
        if isinstance(e, dict) and e.get("source"):
            val_s = f"{e['value']:.4f}" if e.get("value") is not None else "—"
            est_lines.append(f"{label}: {val_s}（{_escape(e.get('source'))}，"
                             f"置信度 {e.get('confidence') or '—'}）")
    if est_lines:
        story.append(Spacer(1, 6))
        story.append(Paragraph("估算参数来源（不静默编造，缺失显式标注）", st["h2"]))
        for line in est_lines:
            story.append(Paragraph(f"• {line}", st["body"]))

    # Phase 2：DCF 敏感性热力图（报告 PNG）
    sens = (dcf or {}).get("sensitivity") or {}
    if charts and charts.get("dcf_sensitivity") and sens.get("wacc_values"):
        story.append(Spacer(1, 6))
        story.append(Paragraph("DCF 敏感性热力图（WACC × 永续增长率 → 每股价值）", st["h2"]))
        story.append(RLImage(charts["dcf_sensitivity"], width=150 * mm, height=94 * mm))

    if sotp and sotp.get("segments"):
        pl, ph = sotp.get("per_share_low"), sotp.get("per_share_high")
        # 缺现价时摊不到每股，但分部表本身有值；不能因为算不出 per-share
        # 就把整张表连同「模型估计」的声明一起吞掉。
        story.append(Spacer(1, 6))
        if pl is not None and ph is not None:
            story.append(Paragraph(f"SOTP 分部估值区间: {currency} {pl:,.2f} ~ {ph:,.2f}", st["h2"]))
        else:
            story.append(Paragraph("SOTP 分部估值", st["h2"]))
        rows = [[Paragraph(x, st["th"]) for x in ("分部", "类型", "收入占比", "净利润占比", "低估值", "高估值")]]
        for seg in sotp.get("segments", []):
            rs = _num(seg.get("revenue_share"))
            ns = _num(seg.get("net_income_share"))
            vl = _num(seg.get("value_low"))
            vh = _num(seg.get("value_high"))
            rs_s = f"{rs * 100:.0f}%" if rs is not None else "—"
            ns_s = f"{ns * 100:.0f}%" if ns is not None else "—"
            vl_s = f"{currency} {vl / 1e9:,.1f}B" if vl is not None else "—"
            vh_s = f"{currency} {vh / 1e9:,.1f}B" if vh is not None else "—"
            rows.append([Paragraph(_escape(seg.get("name", "?")), st["tdl"]),
                         Paragraph(_escape(seg.get("segment_type") or "—"), st["td"]),
                         Paragraph(rs_s, st["td"]),
                         Paragraph(ns_s, st["td"]),
                         Paragraph(vl_s, st["td"]),
                         Paragraph(vh_s, st["td"])])
        story.append(Table(rows, repeatRows=1, style=_table_style(*_st_fonts(st))))
        # 占比是模型拆的，不是披露值。不标这一句，读者会把估算当财报口径。
        story.append(Paragraph("注：分部占比为模型估计，非公司披露口径。", st["body"]))


def _section_market(story, st, ma: dict, comp: dict):
    story.append(Paragraph("四、市场与竞争", st["h1"]))
    bullets = []
    conc = ma.get("industry_concentration") or {}
    cr3, cr5, hhi = _num(conc.get("cr3")), _num(conc.get("cr5")), _num(conc.get("hhi"))
    has_conc = any(v is not None for v in (cr3, cr5, hhi))
    ms = ma.get("market_share")
    if ms is not None:
        label = "市场份额" if has_conc else "份额（已知同业样本内）"
        bullets.append(f"{label}: {ms * 100:.1f}%")
    if has_conc:
        parts = []
        if cr3 is not None:
            parts.append(f"CR3 {cr3 * 100:.1f}%")
        if cr5 is not None:
            parts.append(f"CR5 {cr5 * 100:.1f}%")
        if hhi is not None:
            parts.append(f"HHI {hhi:.0f}")
        bullets.append("行业集中度: " + " | ".join(parts))
    elif conc.get("note"):
        bullets.append(f"行业集中度: {conc['note']}")
    tam = ma.get("tam_estimate")
    if tam:
        bullets.append(f"TAM 市场规模: ${tam / 1e9:,.0f}B")
    for b in bullets:
        story.append(Paragraph(f"• {_escape(b)}", st["body"]))
    global_m = ma.get("markets", {}).get("global", {})
    comp_data = global_m.get("competitor_data", [])
    if comp_data:
        story.append(Spacer(1, 4))
        story.append(Paragraph("主要竞争对手营收对比", st["h2"]))
        share_head = "份额" if has_conc else "份额（样本内）"
        rows = [[Paragraph(x, st["th"]) for x in ("公司", "营收 (USD)", share_head)]]
        for c in comp_data[:8]:
            rev = _num(c.get("revenue"))
            share = _num(c.get("share"))
            rev_s = f"${rev / 1e9:,.1f}B" if rev is not None else "—"
            share_s = f"{share * 100:.1f}%" if share is not None else "—"
            rows.append([Paragraph(_escape(c.get("name", "?")), st["tdl"]),
                         Paragraph(rev_s, st["td"]),
                         Paragraph(share_s, st["td"])])
        story.append(Table(rows, repeatRows=1, style=_table_style(*_st_fonts(st))))


def _section_swot(story, st, swot: dict):
    story.append(Paragraph("五、SWOT 分析", st["h1"]))
    if not isinstance(swot, dict):
        story.append(Paragraph("暂无 SWOT 分析。", st["body"]))
        return
    rating = swot.get("rating")
    summary = swot.get("summary")
    if rating:
        story.append(Paragraph(f"综合评级: {_escape(rating)}", st["body"]))
    if summary:
        story.append(Paragraph(f"总结: {_escape(summary)}", st["body"]))
    data = swot.get("swot", {})
    for key, label in (("strengths", "优势 Strengths"), ("weaknesses", "劣势 Weaknesses"),
                       ("opportunities", "机会 Opportunities"), ("threats", "威胁 Threats")):
        items = data.get(key, [])
        if not items:
            continue
        story.append(Spacer(1, 4))
        story.append(Paragraph(label, st["h2"]))
        for it in items:
            if isinstance(it, dict):
                point = (it.get("point") or it.get("title") or
                         it.get("detail") or it.get("description") or "").strip()
                source = it.get("source")
                text = f"• {point}" if point else "• —"
                if source:
                    text += f"（来源: {source}）"
                story.append(Paragraph(_clean_cell(text), st["body"]))
            else:
                story.append(Paragraph(f"• {_escape(it)}", st["body"]))


def _section_risk(story, st, geo: dict, sc: dict, fc: dict):
    story.append(Paragraph("六、风险分析", st["h1"]))
    ts = geo.get("tariff_sensitivity") if isinstance(geo, dict) else None
    if isinstance(ts, dict) and ts.get("applicable"):
        story.append(Paragraph("关税敏感性", st["h2"]))
        story.append(Paragraph(f"• 情景: {_escape(ts.get('scenario') or '—')}", st["body"]))
        if ts.get("china_exposure") is not None:
            story.append(Paragraph(f"• 中国敞口: {ts['china_exposure'] * 100:.1f}%", st["body"]))
        if ts.get("gross_margin_impact") is not None:
            story.append(Paragraph(f"• 毛利率影响: -{ts['gross_margin_impact'] * 100:.1f}pp", st["body"]))
        if ts.get("description"):
            story.append(Paragraph(f"• 说明: {_escape(ts['description'])}", st["body"]))
    ge = geo.get("geopolitical_events") if isinstance(geo, dict) else {}
    events = ge.get("events", []) if isinstance(ge, dict) else []
    if events:
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"地缘政治事件（风险等级: {_escape(ge.get('risk_level') or 'N/A')}）", st["h2"]))
        for ev in events:
            name = ev.get("event") or ev.get("name")
            region = ev.get("region") or "—"
            prob = ev.get("probability") or "—"
            story.append(Paragraph(f"• <b>{_escape(name)}</b>（{_escape(region)}，概率 {_escape(prob)}，模型推断）", st["body"]))
            if ev.get("impact"):
                story.append(Paragraph(f"  影响: {_escape(ev['impact'])}", st["body"]))
            if ev.get("transmission_path"):
                story.append(Paragraph(f"  传导: {_escape(ev['transmission_path'])}", st["body"]))
        # 事件与概率都是模型推演，何况常带未来年份。不标出来会被当成既成事实读。
        story.append(Paragraph("注：以上地缘事件及其概率为模型情景推演，非事实陈述。", st["body"]))
    if isinstance(sc, dict):
        sc_sum = sc.get("summary")
        sup_conc = sc.get("supplier_concentration")
        ds = sc.get("disruption_scenarios")
        if sc_sum or sup_conc or ds:
            story.append(Spacer(1, 4))
            story.append(Paragraph("供应链", st["h2"]))
            if sc_sum:
                story.append(Paragraph(f"• {_escape(sc_sum)}", st["body"]))
            if isinstance(sup_conc, dict):
                story.append(Paragraph(f"• 供应商集中度: {_escape(sup_conc.get('rating') or '—')}。"
                                       f"{_escape(sup_conc.get('description') or '')}", st["body"]))
            if isinstance(ds, dict) and ds.get("scenarios"):
                for scn in ds.get("scenarios", []):
                    prob = _num(scn.get("probability"))
                    prob_s = f"概率 {prob * 100:.0f}%" if prob is not None else "概率 —"
                    story.append(Paragraph(f"• {_escape(scn.get('name'))}（{prob_s}）: "
                                           f"{_escape(scn.get('description') or '')}", st["body"]))
    if isinstance(fc, dict):
        warnings = fc.get("warnings", [])
        if warnings:
            story.append(Spacer(1, 4))
            story.append(Paragraph("事实核查警告", st["h2"]))
            for w in warnings:
                story.append(Paragraph(f"• {_escape(w)}", st["body"]))


def _section_trade_plan(story, st, tp: dict, charts: dict | None = None):
    """投资策略与交易信号章节（PDF 编号：七）。可用性为假时如实渲染"暂无"。
    charts 含 price_action 时章节内嵌 K线+均线叠加图。"""
    story.append(Paragraph("七、投资策略与交易信号", st["h1"]))
    if not isinstance(tp, dict) or not tp.get("available"):
        reason = tp.get("reason") if isinstance(tp, dict) else ""
        text = "暂无投资策略与交易信号。"
        if reason:
            text += f"（{_escape(reason)}）"
        story.append(Paragraph(text, st["body"]))
        return

    rows = [[Paragraph(x, st["th"]) for x in ("项目", "数值")]]
    ez = tp.get("entry_zone") or [None, None]
    ez_low = _num(ez[0]) if len(ez) > 0 else None
    ez_high = _num(ez[1]) if len(ez) > 1 else None
    if ez_low is not None and ez_high is not None:
        ez_s = f"{ez_low:,.2f} ~ {ez_high:,.2f}"
    else:
        ez_s = "—"
    sl = tp.get("stop_loss")
    sl_s = f"{sl:,.2f}" if _num(sl) is not None else "—"
    tp_val = tp.get("take_profit")
    tp_s = f"{tp_val:,.2f}" if _num(tp_val) is not None else "—"
    cur = tp.get("current_price")
    if _num(cur) is not None:
        rows.append([Paragraph("当前价格", st["tdl"]), Paragraph(f"{_num(cur):,.2f}", st["td"])])
    rows.append([Paragraph("综合评级", st["tdl"]),
                 Paragraph(_escape(tp.get("rating") or "—"), st["td"])])
    rows.append([Paragraph("仓位建议", st["tdl"]),
                 Paragraph(_escape(tp.get("position_hint") or "—"), st["td"])])
    if tp.get("confidence_level"):
        rows.append([Paragraph("信号置信度", st["tdl"]),
                     Paragraph(_escape(tp.get("confidence_level")), st["td"])])
    rr = _num(tp.get("risk_reward_ratio"))
    if rr is not None:
        rows.append([Paragraph("风险收益比", st["tdl"]),
                     Paragraph(f"{rr:.2f}", st["td"])])
    wt = tp.get("weekly_trend")
    if wt:
        wr = tp.get("weekly_resonance") or "—"
        ind_trend = (tp.get("indicators") or {}).get("trend") or "—"
        rows.append([Paragraph("周期共振", st["tdl"]),
                     Paragraph(_escape(f"日线{ind_trend} vs 周线{wt}（{wr}）"), st["td"])])
    rows.append([Paragraph("进场区间", st["tdl"]), Paragraph(ez_s, st["td"])])
    rows.append([Paragraph("止损位", st["tdl"]), Paragraph(sl_s, st["td"])])
    rows.append([Paragraph("止盈位", st["tdl"]), Paragraph(tp_s, st["td"])])
    story.append(Table(rows, colWidths=[60 * mm, 110 * mm],
                       repeatRows=1, style=_table_style(*_st_fonts(st))))

    # K线 + 均线叠加图（charts 中由管线 price_history 生成）
    if charts and charts.get("price_action"):
        try:
            img = RLImage(charts["price_action"], width=155 * mm, height=90 * mm,
                          preserveAspectRatio=True, anchor="c")
            story.append(Spacer(1, 8))
            story.append(img)
            story.append(Paragraph("日K线与均线叠加（含成交量副图，红涨绿跌）", st["small"]))
            story.append(Spacer(1, 8))
        except Exception:
            pass

    reasons = tp.get("reasons") or []
    if reasons:
        story.append(Spacer(1, 6))
        story.append(Paragraph("依据（引用真实指标数值）", st["h2"]))
        for r in reasons:
            story.append(Paragraph(f"• {_clean_cell(r)}", st["body"]))

    # 合规：命中投资建议词 → 落复核队列 + 附一行提示
    body = "\n".join(reasons)
    flagged, hits = check_advice(body)
    if flagged:
        try:
            from app.compliance.queue import flag as queue_flag
            queue_flag(source="analysis_report_trade_plan",
                       content_hash=content_hash(body),
                       snippet=body[:200], hit_words=hits, advice_flag=1)
        except Exception:  # noqa: BLE001 合规落库失败不影响报告生成
            pass
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            "⚠️ 合规提示：以上含量化交易信号表述，仅供参考，不构成任何投资建议。"
            "投资有风险，入市需谨慎。", st["small"]))


def _section_disclaimer(story, st):
    story.append(PageBreak())
    story.append(Paragraph("免责声明", st["h1"]))
    story.append(Paragraph(
        "本报告由系统基于公开数据源与真实计算自动生成，所有数值均来自真实抓取或真实计算；"
        "缺失字段已显式标注。报告仅供参考，不构成任何投资建议或承诺。市场有风险，投资需谨慎。",
        st["small"]))


def _embed_charts(story, st, charts: dict[str, str]):
    if not charts:
        return
    story.append(PageBreak())
    story.append(Paragraph("附录：图表", st["h1"]))
    for key, path in charts.items():
        if key == "price_action":  # 已在投资策略章节内嵌，避免重复
            continue
        try:
            img = RLImage(path, width=155 * mm, height=90 * mm, preserveAspectRatio=True, anchor="c")
            story.append(img)
            story.append(Paragraph(_escape(key), st["small"]))
            story.append(Spacer(1, 8))
        except Exception:
            continue


def result_to_pdf(result: dict, charts: dict | None = None, output_path: str = "report.pdf") -> str:
    """生成深度研报 PDF。"""
    charts = charts or {}
    fonts = reportlab_fonts()
    st = _styles(fonts)

    story = []
    _build_cover(story, fonts, st, result)
    story.append(PageBreak())
    fa = result.get("financial_analysis", {})
    va = result.get("valuation_analysis", {})
    sotp = result.get("sotp_analysis", {})
    ma = result.get("market_analysis", {})
    comp = result.get("competitive_analysis", {})
    swot = result.get("swot_analysis", {})
    geo = result.get("geopolitical_analysis", {})
    sc = result.get("supply_chain_analysis", {})
    fc = result.get("fact_checks", {})

    _section_summary(story, st, result, fa, va, swot)
    story.append(PageBreak())
    _section_financial(story, st, fa, charts, result.get("pricing"))
    story.append(Spacer(1, 10))
    _section_valuation(story, st, result, va, sotp, charts)
    story.append(PageBreak())
    _section_market(story, st, ma, comp)
    story.append(Spacer(1, 10))
    _section_swot(story, st, swot)
    story.append(Spacer(1, 10))
    _section_risk(story, st, geo, sc, fc)
    story.append(Spacer(1, 10))
    _section_trade_plan(story, st, result.get("trade_plan") or {}, charts)
    _embed_charts(story, st, charts)
    _section_disclaimer(story, st)

    ticker = result.get("ticker", "")
    name = result.get("company_info", {}).get("name", ticker)

    doc = BaseDocTemplate(
        output_path, pagesize=A4,
        leftMargin=MARGIN_LEFT, rightMargin=MARGIN_RIGHT,
        topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOTTOM,
    )
    frame = Frame(MARGIN_LEFT, MARGIN_BOTTOM,
                  A4_W - MARGIN_LEFT - MARGIN_RIGHT,
                  A4_H - MARGIN_TOP - MARGIN_BOTTOM, id="normal")

    def add_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont(fonts.get("body", "Times-Roman"), 8)
        canvas.setFillColor(C_GRAY)
        canvas.drawString(MARGIN_LEFT, 12 * mm, f"深度研报 | {ticker} | {name}")
        canvas.drawRightString(A4_W - MARGIN_RIGHT, 12 * mm, f"— {doc_obj.page} —")
        canvas.restoreState()

    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=add_footer)])
    doc.build(story)
    return output_path


# ── 通用 Markdown → PDF（对话报告）─────────────────────────────

_EMOJI_RE = re.compile("[📈📉📊✅❌⚠️⭐💰🛡️📰⚪🟢🔴🔥🟡]")
_HEAD_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_HR_RE = re.compile(r"^\s*(---+|\*\*\*+)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_VZ_TAG_RE = re.compile(r"\[__[A-Z_]+__\]")


def _parse_markdown_lines(md: str):
    """把 markdown 拆成 (type, content) 序列，供 reportlab 渲染。"""
    blocks: list[tuple[str, str | list]] = []
    table: list[str] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if _VZ_TAG_RE.search(line):
            line = _VZ_TAG_RE.sub("", line).strip()
        if not line.strip():
            if table:
                blocks.append(("table", table))
                table = []
            continue
        if _TABLE_ROW_RE.match(line):
            table.append(line)
            continue
        if table:
            blocks.append(("table", table))
            table = []
        m = _HEAD_RE.match(line)
        if m:
            blocks.append(("h", (len(m.group(1)), m.group(2).strip())))
            continue
        if _HR_RE.match(line):
            blocks.append(("hr", ""))
            continue
        m = _BULLET_RE.match(line)
        if m:
            blocks.append(("li", m.group(1)))
            continue
        blocks.append(("p", line))
    if table:
        blocks.append(("table", table))
    return blocks


def _table_from_rows(rows: list[str]):
    cells = []
    for r in rows:
        parts = [c.strip() for c in r.strip().strip("|").split("|")]
        if all(set(p) <= {"-", ":"} for p in parts):
            continue  # 分隔行
        cells.append(parts)
    return cells


def markdown_to_pdf(md: str, output_path: str = "report.pdf", title: str = "投研报告") -> str:
    """将通用 Markdown 转为 PDF（支持标题/段落/列表/表格/分隔线）。"""
    fonts = reportlab_fonts()
    st = _styles(fonts)
    story = []
    story.append(Paragraph(_escape(title), st["cover_sub"]))
    story.append(HRFlowable(width="100%", thickness=1, color=C_ACCENT, spaceAfter=10))

    for kind, content in _parse_markdown_lines(md):
        if kind == "h":
            level, text = content
            if level == 1:
                story.append(Paragraph(_clean_cell(text), st["h1"]))
            elif level == 2:
                story.append(Paragraph(_clean_cell(text), st["h2"]))
            else:
                story.append(Paragraph(f"<b>{_clean_cell(text)}</b>", st["body"]))
        elif kind == "p":
            text = _EMOJI_RE.sub("", content)
            story.append(Paragraph(_clean_cell(text), st["body"]))
        elif kind == "li":
            text = _EMOJI_RE.sub("", content)
            story.append(Paragraph(f"• {_clean_cell(text)}", st["body"]))
        elif kind == "hr":
            story.append(HRFlowable(width="100%", thickness=0.7, color=C_BORDER,
                                    spaceBefore=6, spaceAfter=6))
        elif kind == "table":
            rows = _table_from_rows(content)
            if not rows:
                continue
            width = max(len(r) for r in rows)
            padded = [r + [""] * (width - len(r)) for r in rows]
            header = [Paragraph(_clean_cell(c), st["th"]) for c in padded[0]]
            body = [[Paragraph(_EMOJI_RE.sub("", _clean_cell(c)), st["td"]) for c in r]
                    for r in padded[1:]]
            story.append(Table([header] + body, style=_table_style(*_st_fonts(st))))

    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", thickness=0.7, color=C_BORDER, spaceBefore=6))
    story.append(Paragraph("本报告仅供参考，不构成投资建议。数据来自真实数据源。", st["small"]))

    doc = BaseDocTemplate(
        output_path, pagesize=A4,
        leftMargin=MARGIN_LEFT, rightMargin=MARGIN_RIGHT,
        topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOTTOM,
    )
    frame = Frame(MARGIN_LEFT, MARGIN_BOTTOM,
                  A4_W - MARGIN_LEFT - MARGIN_RIGHT,
                  A4_H - MARGIN_TOP - MARGIN_BOTTOM, id="normal")

    def add_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont(fonts.get("body", "Times-Roman"), 8)
        canvas.setFillColor(C_GRAY)
        canvas.drawString(MARGIN_LEFT, 12 * mm, title)
        canvas.drawRightString(A4_W - MARGIN_RIGHT, 12 * mm, f"— {doc_obj.page} —")
        canvas.restoreState()

    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=add_footer)])
    doc.build(story)
    return output_path
