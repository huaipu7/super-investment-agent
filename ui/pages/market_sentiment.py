"""🌡 市场情绪 —— A股 / 美股 / 港股 市场级情绪评分（真实多源信号）。

布局（计划书 §4.1）：三市场卡片（评分 + 径向仪表 + 分维度）+ AI 解读 +
信号明细表格 + 数据可用性提示。缺信号维度 → score=None 诚实标注。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import streamlit as st

from ui import api_client as api

_MARKET_ORDER = ["a_share", "us", "hk"]


def _color(score) -> str:
    """评分 → 情绪色：红(恐慌) → 橙(谨慎) → 灰(中性) → 绿(偏暖/贪婪)。"""
    if score is None:
        return "#9e9e9e"
    if score <= -50:
        return "#d32f2f"    # 恐慌
    if score <= -20:
        return "#e08a00"    # 谨慎
    if score >= 50:
        return "#1b5e20"    # 贪婪
    if score >= 20:
        return "#43a047"    # 偏暖
    return "#757575"        # 中性


def _gauge(score: float, label: str):
    """径向仪表（-100..+100，底色分区 = 标签区间）。"""
    import plotly.graph_objects as go

    color = _color(score)
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        number={"valueformat": ".0f", "font": {"size": 44, "color": color}},
        gauge={
            "axis": {"range": [-100, 100], "dtick": 50, "tickcolor": "#bbbbbb",
                     "tickfont": {"size": 11}},
            "bar": {"color": color, "thickness": 0.30},
            "bgcolor": "rgba(0,0,0,0)",
            "borderwidth": 0,
            "steps": [
                {"range": [-100, -50], "color": "#fbe3e3"},
                {"range": [-50, -20], "color": "#fbeede"},
                {"range": [-20, 20], "color": "#f0f0f0"},
                {"range": [20, 50], "color": "#e3f0e1"},
                {"range": [50, 100], "color": "#cbe7c9"},
            ],
        },
        title={"text": label, "font": {"size": 15, "color": color}},
    ))
    fig.update_layout(height=230, margin=dict(l=8, r=8, t=52, b=8))
    return fig


def _card(mkey: str, data: dict) -> None:
    """单一市场卡片：评分 + 径向仪表 + 分维度摘要行。"""
    score = data.get("score")
    label = data.get("label") or "数据不足"
    st.markdown(f"### {data.get('market_label', mkey)}")
    if score is None:
        st.metric("情绪评分", "—", label)
        st.caption("⚠️ 数据源获取失败，评分不可用（诚实标注，未补造数值）")
    else:
        st.plotly_chart(_gauge(score, label), use_container_width=True)
        st.metric("情绪评分", f"{score:+.0f}", label)
    for d in data.get("breakdown", []):
        sc = d.get("score")
        sc_txt = f"{sc:+.0f}" if sc is not None else "—"
        val = (d.get("value") or "—").strip()
        st.caption(f"**{d.get('label', '')}** {sc_txt} · {val}")


def page() -> None:
    st.header("🌡 市场情绪")
    st.caption("A股 / 美股 / 港股 市场级情绪 —— 多源真实信号合成；"
               "缺信号 → 显式标注，绝不补造数值。")

    c1, c2, c3 = st.columns([1, 1, 5])
    with c1:
        llm = st.checkbox("启用 AI 解读", value=True, key="ms_llm")
    with c2:
        if st.button("🔄 刷新", key="ms_refresh"):
            st.rerun()

    try:
        res = api.market_sentiment("all", llm=llm)
    except api.ApiError as e:
        st.error(f"市场情绪获取失败: {e}")
        return
    with c3:
        st.caption(f"观测时点: {res.get('as_of') or '—'} · "
                   f"生成于 {res.get('generated_at') or '—'}")

    markets = res.get("markets") or {}

    # 三市场卡片
    cols = st.columns(len(_MARKET_ORDER))
    for col, m in zip(cols, _MARKET_ORDER):
        with col:
            _card(m, markets.get(m) or {})

    # AI 解读
    st.divider()
    st.subheader("📝 AI 市场解读")
    any_llm = False
    for m in _MARKET_ORDER:
        data = markets.get(m) or {}
        txt = data.get("llm_interpretation")
        with st.expander(data.get("market_label", m), expanded=True):
            if txt:
                any_llm = True
                st.write(txt)
            else:
                st.caption("解读未生成（LLM 未就绪 / 数据不足 / 已关闭）。"
                           "上方评分均来自真实数据，可独立使用。")
    if not any_llm:
        st.caption("本次未启用/未得到 AI 解读。")

    # 信号明细表格
    st.divider()
    st.subheader("💾 信号明细")
    rows = []
    for m in _MARKET_ORDER:
        data = markets.get(m) or {}
        for d in data.get("breakdown", []):
            rows.append({
                "市场": data.get("market_label", m),
                "维度": d.get("label", d.get("dim", "")),
                "原始值": d.get("value") or "—",
                "归一化分": (f"{d['score']:+.0f}"
                             if d.get("score") is not None else "—"),
                "数据源": d.get("source") or "—",
                "as_of": d.get("as_of") or "—",
            })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("暂无信号明细")

    # 数据可用性提示
    st.divider()
    st.subheader("⚠️ 数据可用性")
    missing = []
    for m in _MARKET_ORDER:
        data = markets.get(m) or {}
        for d in data.get("breakdown", []):
            if d.get("score") is None:
                missing.append(
                    f"**{data.get('market_label', m)}·{d.get('label', '')}**: "
                    f"{d.get('note') or '数据源获取失败'}")
    if missing:
        for line in missing:
            st.warning(line)
    else:
        st.success("三市场全部信号就绪")
