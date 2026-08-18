"""📊 行情与专家评分"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from ui import api_client as api
from ui import components

_PERIODS = ["5d", "1mo", "3mo", "6mo", "1y", "2y"]


def page() -> None:
    st.header("📊 行情与专家评分")
    st.caption("实时报价 / K线 / Violetz 评分 /（A 股）四大师专家评分 / 财务概览。")

    ticker = components.search_picker("quotes", initial=st.session_state.pop("quotes_initial", None))
    if not ticker:
        st.info("在左侧搜索并选择股票。")
        return

    # 报价
    try:
        q = api.quote(ticker)
    except api.ApiError as e:
        st.error(f"报价获取失败: {e}")
        return
    st.markdown(f"### {q.get('company_name', ticker)} · {q.get('ticker', ticker)} · "
                f"{q.get('currency', '')}　截至 {q.get('regular_market_time') or '实时'}")
    components.quote_cards(q)

    # 历史 K线
    period = st.select_slider("历史区间", options=_PERIODS, value="1mo",
                              key="hist_period")
    try:
        hist = api.history(ticker, period)
        if hist.get("summary"):
            s = hist["summary"]
            st.caption(f"{s.get('start_date')} → {s.get('end_date')} · {hist.get('count')} 根 · "
                       f"区间涨跌 {components.pct(s.get('change_pct'))}")
    except api.ApiError as e:
        hist = None
        st.warning(f"历史数据获取失败: {e}")
    if hist:
        components.kline_chart(hist)

    # 评分
    st.divider()
    st.caption("评分计算中，A 股专家评分约需 30-60 秒…")
    try:
        sc = api.score(ticker)
    except api.ApiError as e:
        st.warning(f"评分获取失败: {e}")
        sc = None
    if sc:
        c1, c2 = st.columns(2)
        with c1:
            components.render_violetz(sc.get("violetz_score"))
        with c2:
            components.render_experts(sc.get("expert_score"))

    # 财务概览
    st.divider()
    try:
        fin = api.financials(ticker)
    except api.ApiError as e:
        fin = None
        st.warning(f"财务数据获取失败: {e}")
    if fin:
        components.render_financials(fin)

    # 公司信息（折叠）
    try:
        info = api.info(ticker)
    except api.ApiError:
        info = None
    if info:
        with st.expander("🏢 公司信息"):
            st.json(info)
