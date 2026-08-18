"""📦 基金分析"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from ui import api_client as api
from ui.pages.chat import _stream_into


def _fund_picker(key: str) -> str | None:
    if f"{key}_sel" not in st.session_state:
        st.session_state[f"{key}_sel"] = None
    selected = st.session_state.get(f"{key}_sel")
    if selected:
        c1, c2 = st.columns([4, 1])
        c1.success(f"✅ 已选择: {selected}")
        if c2.button("更换", key=f"{key}_clear", use_container_width=True):
            st.session_state[f"{key}_sel"] = None
            st.rerun()
        return selected

    q = st.text_input("基金代码 / 名称", key=f"{key}_q", placeholder="如 005827 / 易方达蓝筹")
    if q.strip():
        try:
            results = api.fund_suggest(q.strip())
        except api.ApiError as e:
            st.warning(str(e))
            return None
        for r in results[:8]:
            code, name = r.get("基金代码"), r.get("基金简称")
            if st.button(f"{code} · {name}", key=f"{key}_opt_{code}",
                         use_container_width=True):
                st.session_state[f"{key}_sel"] = code
                st.rerun()
    return None


def _nav_cards(nav: dict) -> None:
    c1, c2, c3, c4 = st.columns(4)
    daily = nav.get("日增长率")
    c1.metric("单位净值", nav.get("单位净值"),
              f"{daily:+.2f}%" if daily is not None else None)
    c2.metric("累计净值", nav.get("累计净值"))
    c3.metric("净值日期", nav.get("净值日期") or "—")
    c4.metric("申赎状态", f"{nav.get('申购状态') or '—'} / {nav.get('赎回状态') or '—'}")
    st.caption(f"日增长值: {nav.get('日增长值') or '—'} · "
               f"手续费: {nav.get('手续费') or '—'}")


def page() -> None:
    st.header("📦 基金分析")
    st.caption("查看基金实时净值，或发起对话式分析（净值 / 持仓 / 多基金对比）。")

    code = _fund_picker("fund")
    if not code:
        st.info("搜索并选择基金代码（如 005827 / 易方达蓝筹）。")
        return

    try:
        nav = api.fund_quote(code)
    except api.ApiError as e:
        st.error(f"净值获取失败: {e}")
        nav = None
    if nav:
        st.markdown(f"### {nav.get('基金代码')} · {nav.get('基金简称')}")
        _nav_cards(nav)
    else:
        st.warning("未获取到该基金实时净值数据。")

    st.divider()
    if st.button("开始对话式分析 ▶", type="primary", key="fund_analyze"):
        prompt = f"分析 {code}"
        with st.container():
            st.caption("🤖 对话式分析（流式输出）")
            placeholder = st.empty()
            status_ph = st.empty()
            try:
                resp = api.fund_stream(prompt)
                _stream_into(prompt, resp, placeholder, status_ph)
            except api.ApiError as e:
                st.error(str(e))
