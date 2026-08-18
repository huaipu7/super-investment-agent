"""🆚 多股横向对比"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import streamlit as st

from ui import api_client as api
from ui import components

_PCT_KEYS = {"return_on_equity", "profit_margin", "revenue_growth"}


def _fmt_metric(v, key: str) -> str:
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if key in _PCT_KEYS:
        return f"{v * 100:.2f}%"
    return f"{v:.2f}"


def _render(data: dict) -> None:
    items = data.get("items", [])
    metric_rows = data.get("metric_rows", [])
    errors = data.get("errors", {}) or {}
    if not items:
        st.error("对比失败，未获取到任何股票数据。")
        for t, e in errors.items():
            st.warning(f"{t}: {e}")
        return

    # 财务 / 估值指标矩阵（行 = 指标，列 = 股票）
    st.subheader("📊 财务 / 估值指标矩阵")
    rows = []
    for row in metric_rows:
        key = row["key"]
        cells = {it["ticker"]: _fmt_metric(it.get("metrics", {}).get(key), key)
                 for it in items}
        rows.append({"指标": row["label"], **cells})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # 实时行情
    st.subheader("💱 实时行情")
    cols = st.columns(len(items))
    for col, it in zip(cols, items):
        q = it.get("quote") or {}
        price = q.get("current_price")
        delta = (components.pct(q.get("change_percent"))
                 if q.get("change_percent") is not None else None)
        with col:
            st.markdown(f"**{it.get('name')}** · {it.get('ticker')}")
            st.metric("现价", f"{components.fmt(price)} {it.get('currency', '')}"
                      if price is not None else "—", delta)
            st.caption(f"市值: {components.fmt_cap(q.get('market_cap'))}")

    # Violetz 评分卡
    st.subheader("🤖 Violetz 推荐评分")
    cols = st.columns(len(items))
    for col, it in zip(cols, items):
        vz = it.get("violetz_score") or {}
        with col:
            total = vz.get("total_score")
            st.metric("综合评分", f"{total:.1f}" if total is not None else "—",
                      vz.get("rating"))
            st.caption(f"评级: {vz.get('rating', '—')} {vz.get('rating_stars', '')}")

    # 四大师专家评分（A股）
    has_expert = any(it.get("expert_score") for it in items)
    if has_expert:
        st.subheader("🧑‍💼 四大师专家评分（A 股）")
        cols = st.columns(len(items))
        for col, it in zip(cols, items):
            with col:
                st.markdown(f"**{it.get('ticker')}**")
                exp = it.get("expert_score")
                if not exp:
                    st.caption("非 A 股 / 无数据")
                    continue
                for e in (exp.get("experts") or [])[:4]:
                    st.caption(f"{e.get('name', '')}: {e.get('score')} 分 "
                               f"({e.get('decision', '')})")

    if errors:
        st.warning("以下股票获取失败：" + "；".join(f"{k} {v}" for k, v in errors.items()))


def page() -> None:
    st.header("🆚 多股横向对比")
    st.caption("对比 2 只以上股票的财务 / 估值 / 评分。A股 与 美股 均可混排。")
    if "cmp_tickers" not in st.session_state:
        st.session_state["cmp_tickers"] = []

    st.markdown("##### 🔍 搜索添加股票")
    new_tk = components.search_picker("cmp_add", "搜索股票")
    if new_tk:
        if new_tk not in st.session_state["cmp_tickers"]:
            st.session_state["cmp_tickers"].append(new_tk)
        st.session_state.pop("cmp_add_sel", None)  # 重置选择器，便于连续添加
        st.rerun()

    txt = st.text_input("或批量输入代码（逗号分隔）", key="cmp_batch",
                        placeholder="如 600519.SS, 000858.SZ")
    if txt.strip() and st.button("✓ 应用批量输入", key="cmp_apply"):
        parsed = [t.strip().upper() for t in txt.split(",") if t.strip()]
        st.session_state["cmp_tickers"] = list(dict.fromkeys(parsed))
        st.rerun()

    ticks = st.session_state["cmp_tickers"]
    if ticks:
        st.markdown("##### 📌 已选股票")
        for t in list(ticks):
            c1, c2 = st.columns([5, 1])
            c1.write(t)
            if c2.button("✕", key=f"cmp_rm_{t}"):
                st.session_state["cmp_tickers"].remove(t)
                st.rerun()

    if not ticks:
        st.info("请先添加至少 2 只股票。")
        return

    disabled = len(ticks) < 2
    if st.button("🆚 开始对比", type="primary", disabled=disabled, key="cmp_go"):
        try:
            data = api.compare(ticks)
        except api.ApiError as e:
            st.error(str(e))
        else:
            _render(data)
