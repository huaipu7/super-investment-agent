"""⭐ 自选股（股票 + 基金）"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from ui import api_client as api
from ui import components


def _goto_quotes(ticker: str) -> None:
    st.session_state["quotes_initial"] = ticker
    st.session_state["nav"] = "📊 行情与评分"
    st.rerun()


def _stock_tab() -> None:
    st.markdown("##### ➕ 添加自选股")
    q = st.text_input("股票代码 / 名称", key="wl_stock_q", placeholder="如 600519 / 苹果")
    if q.strip():
        try:
            results = api.search(q.strip())
        except api.ApiError as e:
            st.warning(str(e))
            results = []
        for r in results[:8]:
            if st.button(f"➕ {r['label']} · {r['ticker']}",
                         key=f"wl_add_{r['ticker']}", use_container_width=True):
                if api.watchlist_add(r["ticker"]):
                    st.success(f"已添加 {r['ticker']}")
                    st.rerun()
                else:
                    st.warning(f"{r['ticker']} 已在自选中")

    st.divider()
    st.markdown("##### ⭐ 自选列表")
    try:
        items = api.watchlist_data()
    except api.ApiError as e:
        st.error(str(e))
        return
    if not items:
        st.info("暂无自选股。")
        return
    for it in items:
        c1, c2, c3, c4, c5, c6 = st.columns([3, 2, 1.3, 1.3, 1, 1])
        name = it.get("name") or it.get("ticker")
        price = it.get("price")
        c1.markdown(f"**{it.get('ticker')}** {name}")
        c2.metric("现价", components.fmt(price))
        c3.write(components.pct(it.get("change_1d_pct")))
        c4.write(components.pct(it.get("change_7d_pct")))
        if c5.button("详情", key=f"wl_det_{it['ticker']}"):
            _goto_quotes(it["ticker"])
        if c6.button("✕", key=f"wl_rm_{it['ticker']}"):
            api.watchlist_remove(it["ticker"])
            st.rerun()

    _news_panel()


def _news_panel() -> None:
    """自选股最新新闻 / 业绩说明会 / 股东大会面板"""
    st.divider()
    with st.expander("📰 最新新闻 / 公告 / 股东大会", expanded=False):
        try:
            items = api.watchlist_news()
        except api.ApiError as e:
            st.warning(str(e))
            return
        if not items:
            st.info("暂无自选股新闻。")
            return
        for it in items:
            st.markdown(f"**{it.get('ticker')}** {it.get('name')}")
            news = it.get("news") or {}
            arts = news.get("articles") or []
            briefs = news.get("briefings") or []
            meets = it.get("meetings") or []
            if not arts and not briefs and not meets:
                st.caption("暂无新闻 / 公告 / 股东大会数据。")
            for a in arts[:5]:
                src = f"（{a.get('source') or ''}）" if a.get("source") else ""
                st.markdown(f"- [{a.get('published') or ''}] {a.get('title') or ''}{src}")
            if briefs:
                st.markdown("**业绩说明会 / 投资者关系公告**")
                for b in briefs[:3]:
                    st.markdown(f"- [{b.get('published') or ''}] {b.get('title') or ''}")
            if meets:
                st.markdown("**股东大会**")
                for m in meets[:2]:
                    st.markdown(f"- {m.get('name') or ''}（{m.get('start_date') or ''}）")
            st.divider()


def _fund_tab() -> None:
    st.markdown("##### ➕ 添加自选基金")
    q = st.text_input("基金代码 / 名称", key="wl_fund_q", placeholder="如 005827 / 易方达蓝筹")
    if q.strip():
        try:
            results = api.fund_suggest(q.strip())
        except api.ApiError as e:
            st.warning(str(e))
            results = []
        for r in results[:8]:
            code, name = r.get("基金代码"), r.get("基金简称")
            if st.button(f"➕ {code} · {name}", key=f"wl_fadd_{code}",
                         use_container_width=True):
                ok = api.fund_watchlist_add(code, name)
                if ok:
                    st.success(f"已添加 {code}")
                    st.rerun()
                else:
                    st.warning(f"{code} 已在自选中")

    st.divider()
    st.markdown("##### ⭐ 自选基金")
    try:
        items = api.fund_watchlist_data()
    except api.ApiError as e:
        st.error(str(e))
        return
    if not items:
        st.info("暂无自选基金。")
        return
    for it in items:
        c1, c2, c3, c4, c5 = st.columns([3, 2, 1.5, 1.5, 1])
        c1.markdown(f"**{it.get('fund_code')}** {it.get('fund_name')}")
        daily = it.get("daily_change")
        c2.metric("单位净值", it.get("unit_nav") or "—",
                  f"{daily:+.2f}%" if daily is not None else None)
        c3.write(it.get("nav_date") or "—")
        if c5.button("✕", key=f"wl_frm_{it['fund_code']}"):
            api.fund_watchlist_remove(it["fund_code"])
            st.rerun()


def page() -> None:
    st.header("⭐ 自选股")
    st.caption("维护常用股票与基金，一键跳转行情 / 评分。")
    t_stock, t_fund = st.tabs(["📈 股票自选", "📦 基金自选"])
    with t_stock:
        _stock_tab()
    with t_fund:
        _fund_tab()
