"""📈 深度研究报告"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from app.config import BASE_DIR
from ui import api_client as api
from ui import components

REPORT_DIR = BASE_DIR / "data" / "reports"

# 评级徽章颜色（中文红涨绿跌约定；中性灰兜底）
_RATING_COLORS = {
    "强烈看多": "#c0392b", "看多": "#e74c3c", "中性": "#7f8c8d",
    "看空": "#16a34a", "强烈看空": "#145a32",
}


def _pricing_scope(result: dict) -> tuple[str | None, str]:
    """产物里的行情时点 → (交易日, 源)。缺失返回 (None, "")，不猜。"""
    pricing = result.get("pricing")
    if not isinstance(pricing, dict):
        return None, ""
    date = pricing.get("price_as_of")
    source = pricing.get("source")
    return (date if isinstance(date, str) and date else None,
            source if isinstance(source, str) else "")


def _strategy_banner(tp: dict, price_as_of: str | None = None) -> None:
    """策略方向横幅（徽章式）。数据来自 result.trade_plan，缺失显式 —，不编造。"""
    if not isinstance(tp, dict) or not tp.get("available"):
        st.info("暂无策略信号（trade_plan 未生成）。")
        return
    rating = tp.get("rating") or "中性"
    color = _RATING_COLORS.get(rating, "#7f8c8d")
    pos = tp.get("position_hint") or "—"
    cur = tp.get("current_price")
    cur_s = f"{cur:,.2f}" if cur is not None else "—"
    if price_as_of:
        cur_s += (f'<span style="color:#6b7280;font-size:.85rem">'
                  f'&nbsp;{price_as_of} 收盘</span>')
    ez = tp.get("entry_zone") or [None, None]
    ez_s = "—"
    if len(ez) > 1 and ez[0] is not None and ez[1] is not None:
        ez_s = f"{ez[0]:,.2f} ~ {ez[1]:,.2f}"
    sl = tp.get("stop_loss")
    sl_s = f"{sl:,.2f}" if sl is not None else "—"
    tpv = tp.get("take_profit")
    tp_s = f"{tpv:,.2f}" if tpv is not None else "—"
    conf = tp.get("confidence_level")
    conf_s = f"置信度 {conf}" if conf else ""
    rr = tp.get("risk_reward_ratio")
    rr_s = f"盈亏比 {rr:.2f}" if rr is not None else ""
    st.markdown(
        f"""<div style="border:1px solid #d9dde3;border-left:5px solid {color};"""
        f"""border-radius:8px;padding:10px 14px;background:#fafbfc;margin-bottom:6px">"""
        f"""<div style="display:flex;gap:18px;flex-wrap:wrap;align-items:center;margin-bottom:6px">"""
        f"""<span style="background:{color};color:#fff;padding:2px 12px;border-radius:12px;"""
        f"""font-weight:600">策略方向 · {rating}</span>"""
        f"""<span><b>建议仓位</b>&nbsp;{pos}</span>"""
        f"""<span><b>当前价格</b>&nbsp;{cur_s}</span>"""
        f"""<span><b>{conf_s}</b></span><span><b>{rr_s}</b></span>"""
        f"""</div><div style="display:flex;gap:24px;flex-wrap:wrap;font-size:0.92rem">"""
        f"""<span><b>进场区间</b>&nbsp;{ez_s}</span>"""
        f"""<span><b>止损位</b>&nbsp;{sl_s}</span>"""
        f"""<span><b>止盈位</b>&nbsp;{tp_s}</span>"""
        f"""</div></div>""",
        unsafe_allow_html=True,
    )


def _completeness_banner(result: dict) -> None:
    """核心章节缺失提示。只读产物里的 completeness，不在 UI 侧重新判定。"""
    comp = result.get("completeness")
    if not isinstance(comp, dict) or comp.get("complete") is not False:
        return
    from app.trust.completeness import SECTION_LABELS
    gaps = comp.get("gaps") or []
    if not gaps:
        return
    parts = []
    for g in gaps:
        label = SECTION_LABELS.get(g.get("section"), g.get("section") or "未知章节")
        parts.append(f"{label}：{g.get('reason') or '数据缺失'}")
    st.warning(f"本次分析有 {len(gaps)} 章缺失 —— {'；'.join(parts)}。以下结论仅供参考。")


_CARRIED_LABELS = {
    "company_info": "公司信息", "financial_analysis": "财务",
    "sotp_analysis": "SOTP", "market_analysis": "市场",
    "competitive_analysis": "竞争", "swot_analysis": "SWOT",
    "fact_checks": "事实核查", "geopolitical_analysis": "地缘",
    "supply_chain_analysis": "供应链", "news_analysis": "新闻",
    "meeting_analysis": "会议", "knowledge": "知识库",
    "sector_adapter": "行业适配",
}


def _refresh_banner(result: dict) -> None:
    """增量刷新提示：哪些是新算的，哪些是哪一天的旧结果。只读产物里的 refresh 块。"""
    r = result.get("refresh")
    if not isinstance(r, dict):
        return
    names: list[str] = []
    for c in r.get("carried_over") or []:
        if not isinstance(c, dict):
            continue
        top = str(c.get("section") or "").split(".")[0]
        label = _CARRIED_LABELS.get(top, top)
        if label and label not in names:
            names.append(label)
    base = str(r.get("base_generated_at") or "")[:10] or "上一期"
    st.info(
        f"⚡ 行情增量刷新：价格取至 {r.get('effective_price_date') or '—'}，"
        f"重算了行情序列 / 估值空间 / 交易信号；"
        f"{'、'.join(names) or '其余章节'} 沿用 {base} 的计算结果。")


_DIFF_CATEGORIES = (
    ("value_changed", "改值"), ("added", "新增"), ("removed", "消失"),
    ("reprovenanced", "换源"), ("degraded_flip", "降级反转"),
)


def _diff_rows(entries: list[dict]) -> list[dict]:
    rows = []
    for e in entries:
        prov_old, prov_new = e.get("provenance_old"), e.get("provenance_new")
        rows.append({
            "字段": e.get("key"),
            "旧值": "—" if e.get("old_value") is None else e.get("old_value"),
            "新值": "—" if e.get("new_value") is None else e.get("new_value"),
            "幅度": ("—" if e.get("rel_delta") is None
                     else f"{e['rel_delta'] * 100:.2f}%"),
            "出处": (prov_old or "—") if prov_old == prov_new
                    else f"{prov_old or '—'} → {prov_new or '—'}",
        })
    return rows


def _show_fact_diff(ticker: str, result: dict) -> None:
    """事实级跨 run 对比：当前结果 vs 某一份历史产物（按路径 + 期间对齐）。"""
    import pandas as pd

    try:
        items = api.thesis_artifacts(ticker)
    except api.ApiError as e:
        st.caption(f"取历史产物失败：{e}")
        return
    if not items:
        st.caption("暂无历史产物，无法对比。")
        return

    refs = [it["ref"] for it in items]
    names = {it["ref"]: it.get("name") or it["ref"] for it in items}
    old = st.selectbox("基线产物", refs, index=min(1, len(refs) - 1),
                       format_func=lambda r: names.get(r, r), key=f"fd_old_{ticker}")
    try:
        d = api.diff_analysis(old_ref=old)
    except api.ApiError:
        if len(refs) < 2:
            st.warning("没有可对比的当前结果，历史产物也只有一份。")
            return
        try:
            d = api.diff_analysis(old_ref=refs[1], new_ref=refs[0])
        except api.ApiError as e:
            st.warning(f"对比失败：{e}")
            return

    s = d["summary"]
    c = st.columns(5)
    c[0].metric("新增", s["added"])
    c[1].metric("消失", s["removed"])
    c[2].metric("改值", s["value_changed"], f"实质 {s['material']}")
    c[3].metric("换源", s["reprovenanced"])
    c[4].metric("降级反转", s["degraded_flip"])
    st.caption(f"改值幅度分档：实质 {s['material']} · 微小 {s['minor']} · "
               f"噪声 {s['noise']}（float 往返）· 非有限 {s['undefined']}。"
               "按「路径 + 期间」对齐，行情序列按日期而非下标对齐。")

    keys = [k for k, _ in _DIFF_CATEGORIES]
    labels = {k: v for k, v in _DIFF_CATEGORIES}
    cat = st.radio("变化类型", keys, horizontal=True,
                   format_func=lambda k: labels.get(k, k), key=f"fd_cat_{ticker}")
    entries = d["changes"].get(cat) or []
    if cat == "value_changed":
        only_material = st.checkbox("只看实质变化（相对幅度 ≥ 1%）", value=True,
                                    key=f"fd_mat_{ticker}")
        if only_material:
            entries = [e for e in entries if e.get("class") == "material"]
    if not entries:
        st.caption("该类无变化。")
        return
    st.dataframe(pd.DataFrame(_diff_rows(entries)), use_container_width=True,
                 height=min(420, 40 + 32 * min(len(entries), 12)))


def _render_md_with_toc(md: str, key: str) -> None:
    """渲染研报：侧栏章节导航 + 主区章节折叠（选中章节展开，默认完整报告）。"""
    from ui import report_diff as rd
    preamble, sections = rd.split_sections(md)
    if not sections:
        st.markdown(md)
        return
    titles = ["完整报告"] + [t for t, _ in sections]
    with st.sidebar:
        sel = st.radio("📑 章节跳转", titles, index=0, key=f"toc_{key}")
    if sel == "完整报告":
        st.markdown(md)
        return
    if preamble.strip():
        st.markdown(preamble)
    for i, (title, body) in enumerate(sections):
        with st.expander(title, expanded=(title == sel)):
            st.markdown(body)


def _render_report_diff(md_items: dict, ticker: str) -> None:
    """同 ticker 两期研报 diff：章节级行/字符高亮 + 变更统计（纯函数，不重算数值）。"""
    from ui import report_diff as rd
    if len(md_items) < 2:
        st.caption("历史版本不足 2 份，无法对比（重新生成一次报告后即可）。")
        return

    def _label(name: str) -> str:
        mt = md_items[name].get("modified")
        t = (datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")
             if mt else "?")
        return f"{name}  ({t})"

    names = list(md_items.keys())
    c1, c2 = st.columns(2)
    old_sel = c1.selectbox("旧版本", names, index=min(1, len(names) - 1),
                           format_func=_label, key=f"diff_old_{ticker}")
    new_sel = c2.selectbox("新版本", names, index=0,
                           format_func=_label, key=f"diff_new_{ticker}")
    if old_sel == new_sel:
        st.info("新旧版本相同，请选择两个不同版本进行对比。")
        return
    try:
        old = (REPORT_DIR / old_sel).read_text(encoding="utf-8")
        new = (REPORT_DIR / new_sel).read_text(encoding="utf-8")
    except OSError as e:
        st.warning(f"读取报告失败: {e}")
        return

    diff = rd.diff_reports(old, new)
    stats = diff["stats"]
    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("章节数", stats["sections_total"])
    sc2.metric("变动章节", stats["sections_changed"])
    sc3.metric("新增行", stats["added"])
    sc4.metric("删除行", stats["removed"])
    if stats["added"] == 0 and stats["removed"] == 0:
        st.success("两期报告内容一致，无差异。")
        return
    for sec in diff["sections"]:
        if not sec["changed"]:
            continue
        tag = "新增章节" if sec["added"] else ("整章删除" if sec["removed"] else "内容变动")
        label = f"{sec['title']} · {tag}（+{sec['n_added']} / −{sec['n_removed']}）"
        with st.expander(label, expanded=bool(sec["added"] or sec["removed"])):
            st.markdown(
                f'<div style="font-family:Consolas,monospace;font-size:0.85rem;'
                f'line-height:1.6">{sec["html"]}</div>',
                unsafe_allow_html=True)


def _stream_progress(ticker: str) -> None:
    """SSE 消费：progress → 进度条 + 阶段文本；status/done/error 收尾。"""
    try:
        resp = api.analysis_stream(ticker)
    except api.ApiError as e:
        st.error(f"SSE 连接失败: {e}")
        return
    resp.encoding = "utf-8"
    bar = st.progress(0.0)
    evt = None
    for raw in resp.iter_lines(decode_unicode=True):
        line = (raw or "").strip()
        if line.startswith("event:"):
            evt = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            try:
                data = json.loads(line.split(":", 1)[1].strip())
            except Exception:
                continue
            if evt == "progress":
                pct = max(0.0, min(1.0, float(data.get("pct", 0)) / 100.0))
                bar.progress(pct, text=f"{data.get('label', '')} — {data.get('message', '')}")
            elif evt == "error":
                bar.empty()
                st.error(f"分析异常: {data.get('message', '')}")
                return
            elif evt == "status":
                if data.get("status") == "completed":
                    bar.progress(1.0)
                    st.success("✅ 分析完成")
                elif data.get("status") == "failed":
                    bar.empty()
                    st.error("分析失败，请查看后端日志。")
                return
            elif evt == "done":
                return


def _show_report(ticker: str) -> None:
    try:
        result = api.analysis_result(ticker)
    except api.ApiError as e:
        st.error(f"读取分析结果失败: {e}")
        return
    try:
        rep = api.analysis_report(ticker)
    except api.ApiError as e:
        st.warning(f"报告生成失败: {e}")
        rep = None

    # 摘要卡片
    st.subheader("📋 分析摘要")
    _refresh_banner(result)
    _completeness_banner(result)
    fin = result.get("financial_analysis") or {}
    val = result.get("valuation_analysis") or {}
    dcf = val.get("dcf") or {}
    price_as_of, price_source = _pricing_scope(result)
    price_now = fin.get("current_price")
    dcf_help = (f"以 {price_as_of} 收盘价 {price_now:,.2f} 计算"
                if price_as_of and price_now is not None else None)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("管线耗时", f"{result.get('pipeline_time', 0):.1f}s")
    c2.metric("财务比率", f"{len(fin.get('ratios', {}))} 项")
    fair = dcf.get("fair_value")
    c3.metric("DCF 合理价", components.fmt(fair), help=dcf_help)
    ups = dcf.get("upside")
    c4.metric("DCF 空间", f"{ups:+.1%}" if ups is not None else "—", help=dcf_help)
    c5.metric("报告图表", f"{rep.get('charts_count', '—')} 张" if rep else "—")
    st.caption(f"行情日期 {price_as_of}" + (f" · 源 {price_source}" if price_source else "")
               if price_as_of else "行情日期 —（产物未记录）")

    # 策略方向横幅（来自 trade_plan）
    _strategy_banner(result.get("trade_plan") or {}, price_as_of)

    # 红队段落：只有深度档产物才带（standard/quick 没有这段，不画空壳）
    if result.get("redteam"):
        st.divider()
        components.render_redteam(result.get("redteam"))

    # 数据可视化（复用本次分析结果，plotly 交互式图表）
    st.divider()
    _show_viz(ticker, result)

    # 历史版本下拉：列出该股全部 md，默认最新，可切换回看（侧栏章节导航）
    st.divider()
    try:
        md_files = api.reports_list(kind="md", ticker=ticker)
    except api.ApiError:
        md_files = []
    md_items = {f["name"]: f for f in md_files}
    if md_items:
        def _label(name: str) -> str:
            mt = md_items[name].get("modified")
            t = (datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")
                 if mt else "?")
            return f"{name}  ({t})"
        selected = st.selectbox("📜 历史版本", list(md_items.keys()), index=0,
                                format_func=_label, key=f"res_hist_{ticker}")
        md_file = REPORT_DIR / selected
        if md_file.is_file():
            md = md_file.read_text(encoding="utf-8")
            chart_dir = str(REPORT_DIR / "charts").replace("\\", "/")
            md = md.replace("](charts/", f"]({chart_dir}/")
            _render_md_with_toc(md, f"{ticker}_{selected}")
        else:
            st.warning(f"本地文件缺失: {selected}")

        # 报告 diff：同 ticker 两期报告数值/章节高亮
        with st.expander("🔁 报告对比（两期 diff）", expanded=False):
            _render_report_diff(md_items, ticker)

        # 事实级 diff：按「路径 + 期间」对齐台账事实，看得见哪个数字变了
        with st.expander("🔀 事实级对比（本次 vs 历史产物）", expanded=False):
            _show_fact_diff(ticker, result)
    else:
        st.warning("未找到 Markdown 报告文件，请重新生成。")

    # 下载
    if rep:
        files = {k: v for k, v in rep.get("report", {}).items()}
        if files:
            st.subheader("⬇️ 下载报告")
            dcols = st.columns(len(files))
            mimes = {"pdf": "application/pdf", "html": "text/html", "md": "text/markdown"}
            for col, (ext, path) in zip(dcols, files.items()):
                name = os.path.basename(path)
                try:
                    data = api.reports_download(name)
                    col.download_button(f"下载 {ext.upper()}",
                                        data=data, file_name=name,
                                        mime=mimes.get(ext, "application/octet-stream"),
                                        key=f"dl_{ticker}_{ext}")
                except api.ApiError as e:
                    col.error(str(e))

    # Excel 导出（财务工作簿，openpyxl 本地生成，数值原样不重算）
    st.divider()
    try:
        from ui import export as ui_export
        xlsx = ui_export.financial_xlsx(result)
    except Exception:
        xlsx = None
    if xlsx:
        st.download_button(
            "导出财务 Excel", data=xlsx,
            file_name=f"{ticker}_财务数据.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"xlsx_{ticker}")
    else:
        st.caption("暂无财务数据可导出 Excel。")

    if can_feedback_report(result):
        _report_feedback(result)
    else:
        st.divider()
        st.caption("这份报告生成于反馈功能上线前，缺少关联键，无法归因 —— 不提供反馈入口。")


# ── 反馈（数据飞轮入口）─────────────────────────────────────

_REPORT_ISSUES = ["数字不对", "来源不可信", "结论不成立", "其他"]


def can_feedback_report(result: dict) -> bool:
    """报告需要 report_id + request_id 才能归因；旧缓存产物没有，就不给入口。"""
    r = result or {}
    return bool((r.get("report_id") or "").strip()
                and (r.get("request_id") or "").strip())


def _submit_report_feedback(rid: str, mid: str, label: str, *, scope: str,
                            fact_id: str | None = None,
                            options: list[str] | None = None,
                            note: str | None = None) -> bool:
    try:
        api.feedback(rid, mid, label, scope=scope, fact_id=fact_id,
                     options=options, note=note)
    except api.ApiError as e:
        st.error(f"反馈提交失败：{e}")
        return False
    st.session_state.setdefault("fb_done", {})[mid] = label
    return True


def _report_feedback(result: dict) -> None:
    mid = result["report_id"]
    rid = result["request_id"]
    st.divider()
    st.markdown("**这份报告有用吗？**")

    done = (st.session_state.get("fb_done") or {}).get(mid)
    if done:
        st.caption("✅ 已记录 · 👍 有帮助" if done == "up"
                   else "✅ 已记录 · 👎 进入待审队列，需人工审阅（不会自动进回归集）")
        return

    c1, c2 = st.columns([1, 1])
    if c1.button("👍 有用", key=f"rfb_up_{mid}"):
        if _submit_report_feedback(rid, mid, "up", scope="report"):
            st.rerun()
    if c2.button("👎 有问题", key=f"rfb_down_{mid}"):
        st.session_state["rfb_open"] = mid
        st.rerun()

    if st.session_state.get("rfb_open") != mid:
        return
    with st.form(f"rfb_form_{mid}"):
        where = st.radio("范围", ["整份报告", "某个数字"], horizontal=True,
                         key=f"rfb_scope_{mid}")
        picks = st.multiselect("问题类型", _REPORT_ISSUES, key=f"rfb_opts_{mid}")
        fact = st.text_input("涉及字段/数字（选填，如 F117 / 分部占比）",
                             key=f"rfb_fact_{mid}")
        note = st.text_input("补充说明（选填）", key=f"rfb_note_{mid}")
        if st.form_submit_button("提交反馈"):
            scope = "report" if where == "整份报告" else "fact"
            if _submit_report_feedback(rid, mid, "down", scope=scope,
                                       fact_id=(fact.strip() or None),
                                       options=picks, note=note):
                st.session_state.pop("rfb_open", None)
                st.rerun()


def _render_dcf_recalc(ticker: str, val: dict) -> None:
    """DCF 参数可调重算：WACC / 永续增长率 / 预测期滑块 → 调后端秒回 fair_value。

    免费源内、无 LLM；参数缺失回退分析时基准值；后端不可用时降级提示。
    """
    import ui.viz_charts as vz
    dcf = val.get("dcf") or {}
    est = val.get("estimates") or {}
    base_wacc = dcf.get("wacc_used")
    base_tg = dcf.get("terminal_growth")
    base_years = dcf.get("projection_years")
    st.caption("调整 WACC / 永续增长率 / 预测期年数 → 即时重算每股内在价值"
               "（基于本次分析缓存，确定性计算，无 LLM）。")
    c1, c2, c3 = st.columns(3)
    wacc_pct = c1.slider("WACC (%)", 3.0, 20.0,
                         value=round(base_wacc * 100, 1) if base_wacc else 9.0,
                         step=0.5, key="dcf_recalc_wacc")
    tg_pct = c2.slider("永续增长率 (%)", 0.0, 6.0,
                       value=round(base_tg * 100, 1) if base_tg else 3.0,
                       step=0.5, key="dcf_recalc_tg")
    years = c3.slider("预测期 (年)", 3, 10, value=base_years or 5,
                      key="dcf_recalc_years")
    beta_est = (est.get("beta") or {})
    if beta_est.get("value") is not None:
        st.caption(f"Beta 估算值: {beta_est['value']:.2f}（{beta_est.get('source') or '—'}，"
                   f"置信度 {beta_est.get('confidence') or '—'}）")
    try:
        out = api.recompute_dcf(ticker, wacc=wacc_pct / 100,
                                terminal_growth=tg_pct / 100,
                                projection_years=int(years))
    except api.ApiError as e:
        st.warning(f"DCF 重算失败: {e}")
        return
    rc1, rc2, rc3, rc4 = st.columns(4)
    rc1.metric("重算合理价", components.fmt(out.get("fair_value")))
    rc2.metric("重算空间", f"{out.get('upside'):+.1%}" if out.get("upside") is not None else "—")
    rc3.metric("WACC", f"{out.get('wacc_used', 0) * 100:.2f}%")
    base_fv = out.get("base_fair_value")
    if base_fv is not None and out.get("fair_value") is not None:
        rc4.metric("相对基准", f"{(out['fair_value'] / base_fv - 1):+.1%}")
    else:
        rc4.metric("基准合理价", components.fmt(base_fv))
    fig = vz.dcf_sensitivity_fig({"dcf": out})
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True)
        sens = out.get("sensitivity") or {}
        st.caption(sens.get("note") or "中心点=当前滑块参数，四周=±2pp / ±1.5pp 敏感性。")


def _show_viz(ticker: str, result: dict) -> None:
    """数据可视化：折线/柱状/饼/箱型/表格，全部基于本次分析结果。"""
    import ui.viz_charts as vz

    fin = result.get("financial_analysis") or {}
    val = result.get("valuation_analysis") or {}
    market = result.get("market_analysis") or {}

    st.subheader("📊 数据可视化")
    tab_price, tab_line, tab_bar, tab_pie, tab_box, tab_dup, tab_dcf, tab_recalc, tab_table = st.tabs(
        ["K线 & 指标", "折线趋势", "柱状对比", "饼图", "箱型分布",
         "杜邦分解", "DCF 敏感", "DCF 重算", "数据表格"])

    with tab_price:
        freq = st.radio("周期", ["D", "W", "M"], horizontal=True,
                        key="viz_kline_freq",
                        format_func=lambda f: {"D": "日", "W": "周", "M": "月"}[f])
        ph = result.get("price_history")
        fig = vz.kline_ohlc_fig(ph, result.get("trade_plan"), freq)
        if fig is None:
            # 兜底：深度研究无 price_history 时用 /history 实时序列
            try:
                hist = api.history(ticker, period="1y")
            except api.ApiError:
                hist = None
            fig = vz.kline_ohlc_fig(hist, None, freq)
        if fig is not None:
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("暂无历史行情数据（需完成深度研究或后端可用）")

    cats = vz.available_categories(fin)
    with tab_line:
        if not cats:
            st.info("暂无财务比率数据")
        else:
            cat = st.selectbox("比率分类", cats, key="viz_line_cat")
            fig = vz.ratio_trend_fig(fin, cat)
            if fig is not None and len(fig.data) > 0:
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("该分类下无可用的比率")

    with tab_bar:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**估值对比（公司 vs 行业均值）**")
            fig = vz.valuation_bar_fig(val)
            if fig is not None:
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("暂无估值对比数据")
        with c2:
            st.markdown("**财务规模（亿元）**")
            choices = vz.raw_metric_choices(fin)
            if not choices:
                st.info("暂无原始财务数据")
            else:
                sel = st.multiselect("选择指标", choices,
                                     default=choices[:3], key="viz_bar_metrics",
                                     format_func=vz.metric_name)
                if sel:
                    st.plotly_chart(vz.raw_metric_bars_fig(fin, sel),
                                    use_container_width=True)

    with tab_pie:
        fig = vz.market_pie_fig(market)
        if fig is not None:
            st.plotly_chart(fig, use_container_width=True)
            ms = market.get("market_share")
            ic = market.get("industry_concentration") or {}
            if ic.get("cr3") is not None:
                cap = (f"本公司份额 {ms:.1%} · CR3 {ic['cr3']:.1%} · "
                       f"CR5 {(ic.get('cr5') or 0):.1%} · HHI {(ic.get('hhi') or 0):.0f}")
            else:
                cap = (f"本公司份额（已知同业样本内） {ms:.1%} · "
                       f"行业集中度：{ic.get('note') or '缺行业总规模，未计算'}")
            st.caption(cap)
        else:
            st.info("暂无市场份额数据（无竞品营收数据）")

    with tab_box:
        if not cats:
            st.info("暂无财务比率数据")
        else:
            cat = st.selectbox("比率分类", cats, key="viz_box_cat")
            fig = vz.ratio_box_fig(fin, cat)
            if fig is not None and len(fig.data) > 0:
                st.plotly_chart(fig, use_container_width=True)
                st.caption("箱型图表示该指标在分析年份区间内的分布"
                           "（盒须=四分位/极值，点=各年实际值）；非跨公司对比"
                           "（暂无同行财务数据）。")
            else:
                st.info("该分类各比率的年份点不足 3 个，无法绘制箱型图")

    with tab_dup:
        fig = vz.dupont_fig(fin)
        if fig is not None:
            st.plotly_chart(fig, use_container_width=True)
            st.caption("上：最近一年 ROE 对数贡献瀑布（乘性→加性，终值还原为 ROE%）；"
                       "下：逐年 ROE 趋势。")
        else:
            st.info("暂无杜邦分解数据（需至少一年净利率/资产周转率/权益乘数）")

    with tab_dcf:
        fig = vz.dcf_sensitivity_fig(val)
        if fig is not None:
            st.plotly_chart(fig, use_container_width=True)
            sens = (val.get("dcf") or {}).get("sensitivity") or {}
            st.caption(sens.get("note") or "中心点=当前 WACC/永续增长率参数。")
        else:
            st.info("暂无 DCF 敏感性数据（DCF 未生成或敏感性矩阵缺失）")

    with tab_recalc:
        _render_dcf_recalc(ticker, val)

    with tab_table:
        t1 = vz.raw_metrics_df(fin)
        t2 = vz.ratios_df(fin)
        if not t1.empty:
            st.markdown("**财务规模（亿元）**")
            st.dataframe(t1, use_container_width=True)
        if not t2.empty:
            st.markdown("**财务比率**")
            st.dataframe(t2, use_container_width=True)
        if t1.empty and t2.empty:
            st.info("暂无财务数据")


def page() -> None:
    st.header("📈 深度研究报告")
    st.caption("对目标公司执行完整研究管线：财务 / 估值 DCF / SOTP / 市场 / 竞争 / 新闻舆情 / "
               "股东大会 / SWOT / 风险，约 1-2 分钟（后台执行，进度实时推送）。")

    ticker = components.search_picker("research", "选择股票")
    if not ticker:
        st.info("在左侧搜索并选择股票后，点击「开始深度研究」。")
        return

    try:
        status = api.analysis_status(ticker).get("status", "pending")
    except api.ApiError as e:
        st.error(f"后端不可用: {e}")
        return

    if status == "running":
        st.warning("已有分析任务正在执行中，请稍候…")
        st.progress(0.5)
        if st.button("刷新状态", key="res_refresh"):
            st.rerun()

    elif status == "failed":
        st.error("上次分析失败。可重新开始。")
        if st.button("重新开始", type="primary", key="res_retry"):
            try:
                api.analysis_run(ticker)
                _stream_progress(ticker)
                st.rerun()
            except api.ApiError as e:
                st.error(str(e))

    elif status == "completed":
        _show_report(ticker)
        st.divider()
        c_px, c_full = st.columns(2)
        if c_px.button("⚡ 行情刷新（不调 LLM，几秒）", key="res_refresh_px"):
            try:
                out = api.refresh_analysis(ticker)
                st.success(f"已刷新：价格取至 "
                           f"{(out.get('refresh') or {}).get('effective_price_date')}，"
                           f"产物 {out.get('artifact_ref')}")
                st.rerun()
            except api.ApiError as e:
                st.error(f"刷新失败：{e}")
        if c_full.button("🔄 重新分析", key="res_rerun"):
            try:
                api.analysis_run(ticker)
                _stream_progress(ticker)
                st.rerun()
            except api.ApiError as e:
                st.error(str(e))

    else:  # pending（进程重启后任务丢失）
        st.info("该股票尚未分析。点击开始执行完整研究管线。")
        if st.button("开始深度研究", type="primary", key="res_start"):
            try:
                api.analysis_run(ticker)
                _stream_progress(ticker)
                st.rerun()
            except api.ApiError as e:
                st.error(str(e))
