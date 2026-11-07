"""看板共享组件：后端状态、搜索选择器、指标卡、图表渲染。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from ui import api_client as api


# ── 格式化 ────────────────────────────────────────────

def pct(v, plus: bool = True) -> str:
    """百分比数字 → 显示字符串（None → —）"""
    if v is None:
        return "—"
    try:
        return f"{v:+.2f}%" if plus else f"{v:.2f}%"
    except (TypeError, ValueError):
        return str(v)


def fmt(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{v:,}"
    except (TypeError, ValueError):
        return str(v)


def fmt_cap(v) -> str:
    """市值紧凑格式（USD/CNY 通用）"""
    if v is None:
        return "—"
    try:
        v = float(v)
        if abs(v) >= 1e12:
            return f"{v / 1e12:.2f}T"
        if abs(v) >= 1e9:
            return f"{v / 1e9:.2f}B"
        if abs(v) >= 1e6:
            return f"{v / 1e6:.2f}M"
        return f"{v:,.0f}"
    except (TypeError, ValueError):
        return str(v)


# ── 后端状态（侧边栏）─────────────────────────────────

@st.cache_data(ttl=30)
def _backend_health():
    return api.health()


def backend_status() -> None:
    h = _backend_health()
    if h and h.get("status") == "ok":
        llm = "LLM 已就绪" if h.get("llm_ready") else "LLM 密钥未配置"
        st.sidebar.success(f"后端已连接 · {llm}")
        st.sidebar.caption(f"模型: {h.get('model', '')}")
    else:
        st.sidebar.error("后端未连接 — 请先启动: python api/main.py")


# ── 数据源健康（Phase 4）─────────────────────────────────

@st.cache_data(ttl=30)
def _metrics_snapshot():
    return api.metrics()


def data_source_status() -> None:
    """侧边栏数据源健康徽标：请求数 / 错误 / 平均耗时（来自 /api/v1/metrics）。"""
    ds = _metrics_snapshot().get("data_sources", {})
    if not ds:
        st.sidebar.caption("数据源监控: 暂无数据")
        return
    st.sidebar.markdown("**数据源健康**")
    for src in ("akshare", "yfinance", "sina"):
        item = ds.get(src) or {}
        req = item.get("requests", 0)
        err = item.get("errors", 0)
        avg = item.get("avg_seconds")
        avg_s = f" · {avg:.2f}s" if avg is not None else ""
        badge = f"🟢" if err == 0 else f"🔴 {err} 错误"
        st.sidebar.caption(f"{badge} {src}: {req} 请求{avg_s}")


# ── 股票搜索选择器 ────────────────────────────────────

def search_picker(key: str, label: str = "股票代码 / 名称",
                  initial: str | None = None) -> str | None:
    """搜索 + 选择股票，返回选中的 ticker（未选返回 None）。

    选择后固定显示「已选择」，可点「更换」重新搜索。
    """
    if initial and f"{key}_sel" not in st.session_state:
        st.session_state[f"{key}_sel"] = initial

    selected = st.session_state.get(f"{key}_sel")
    if selected:
        c1, c2 = st.columns([4, 1])
        c1.success(f"✅ 已选择: {selected}")
        if c2.button("更换", key=f"{key}_clear", use_container_width=True):
            del st.session_state[f"{key}_sel"]
            st.rerun()
        return selected

    q = st.text_input(label, key=f"{key}_q", placeholder="如 600519 / 苹果 / AAPL")
    if q.strip():
        try:
            results = api.search(q.strip())
        except api.ApiError as e:
            st.warning(str(e))
            return None
        for r in results[:10]:
            label_txt = f"{r['label']} · {r['ticker']}"
            if st.button(label_txt, key=f"{key}_opt_{r['ticker']}",
                         use_container_width=True):
                st.session_state[f"{key}_sel"] = r["ticker"]
                st.rerun()
    return None


# ── 行情卡片 / K线 ────────────────────────────────────

def quote_cards(q: dict) -> None:
    c1, c2, c3, c4, c5 = st.columns(5)
    price = q.get("current_price")
    delta = pct(q.get("change_percent")) if q.get("change_percent") is not None else None
    c1.metric("现价", f"{fmt(price)} {q.get('currency', '')}", delta)
    c2.metric("开盘", fmt(q.get("open")))
    hi = q.get("day_high")
    lo = q.get("day_low")
    c3.metric("最高 / 最低", f"{hi or '—'} / {lo or '—'}")
    c4.metric("成交量", fmt(q.get("volume")))
    c5.metric("市值", fmt_cap(q.get("market_cap")))


def kline_chart(hist: dict) -> None:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    records = (hist or {}).get("records", [])
    if not records:
        st.info("暂无历史行情数据")
        return
    dates = [r["date"] for r in records]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.75, 0.25], vertical_spacing=0.02)
    fig.add_trace(go.Candlestick(
        x=dates,
        open=[r["open"] for r in records],
        high=[r["high"] for r in records],
        low=[r["low"] for r in records],
        close=[r["close"] for r in records],
        name="K线",
    ), row=1, col=1)
    fig.add_trace(go.Bar(x=dates, y=[r["volume"] for r in records], name="成交量",
                         marker_color="rgba(90,120,255,0.45)"), row=2, col=1)
    fig.update_layout(
        height=500, margin=dict(l=0, r=0, t=30, b=0),
        xaxis_rangeslider_visible=False, showlegend=False,
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="量", row=2, col=1)
    st.plotly_chart(fig, use_container_width=True)


# ── 评分 ──────────────────────────────────────────────

def render_violetz(vz: dict | None) -> None:
    st.subheader("🤖 Violetz 推荐评分")
    if not vz:
        st.info("Violetz 评分不可用（数据不足）")
        return
    total = vz.get("total_score")
    c1, c2 = st.columns([1, 2])
    c1.metric("综合评分", f"{total:.1f}" if total is not None else "—", vz.get("rating"))
    stars = vz.get("rating_stars") or ""
    c2.markdown(f"**评级**: {vz.get('rating', '—')} {stars}")
    avail, tot = vz.get("available_metrics", 0), vz.get("total_metrics", 0)
    c2.caption(f"可用指标: {avail}/{tot}")
    breakdown = vz.get("breakdown", {}) or {}
    if breakdown:
        df = pd.DataFrame([
            {"指标": k, "得分": v.get("score"), "说明": v.get("detail")}
            for k, v in breakdown.items()
        ])
        st.dataframe(df, use_container_width=True, hide_index=True)


def render_experts(expert: dict | None) -> None:
    if not expert:
        return
    st.subheader("🧑‍💼 四大师专家评分（A 股）")
    experts = expert.get("experts") or []
    cols = st.columns(len(experts) if experts else 1)
    for col, e in zip(cols, experts):
        with col:
            st.metric(f"{e.get('icon', '')} {e.get('name', '')}",
                      f"{e.get('score')} 分", e.get("decision"))
            st.caption((e.get("summary") or "")[:70])
    comparison = expert.get("comparison") or []
    if comparison:
        with st.expander("指标对比（PE / PB / ROE / 负债率 / 毛利率 / 净利率 / 增速）"):
            st.dataframe(pd.DataFrame(comparison), use_container_width=True, hide_index=True)
    if expert.get("stock"):
        with st.expander("行情 / 财务详情"):
            st.json(expert["stock"])


# ── 财务概览 ──────────────────────────────────────────

def _ratio_to_pct(v):
    """把比率（小数或已是百分数）规范为百分数显示"""
    if v is None:
        return "—"
    try:
        v = float(v)
        if abs(v) <= 5:  # 小数（如 0.4875 → 48.75%）
            return f"{v * 100:.2f}%"
        return f"{v:.2f}%"
    except (TypeError, ValueError):
        return str(v)


def render_financials(fin: dict | None) -> None:
    st.subheader("📑 财务概览")
    if not fin:
        st.info("财务数据不可用")
        return
    cells = [
        ("pe_ratio", "市盈率 (PE)", lambda v: f"{v:.2f}"),
        ("return_on_equity", "ROE", _ratio_to_pct),
        ("revenue_growth", "营收增速", _ratio_to_pct),
        ("profit_margin", "净利润率", _ratio_to_pct),
        ("debt_to_equity", "负债权益比", lambda v: f"{v:.2f}"),
        ("eps", "每股收益", lambda v: f"{v:.2f}"),
    ]
    cols = st.columns(len(cells))
    for col, (key, label, formatter) in zip(cols, cells):
        val = fin.get(key)
        col.metric(label, formatter(val) if val is not None else "—")


# ── 红队 ──────────────────────────────────────────────

_REDTEAM_STATUS = {
    "ok": ("✅", "反方论证全部有真实引用"),
    "insufficient_evidence": ("⚠️", "证据不足，条数不够就说不算"),
    "llm_unavailable": ("⚪", "模型不可用，未生成反方论证"),
}


def render_redteam(rt: dict | None) -> None:
    """展示红队三段。没有红队（quick/standard 档）时什么都不画。"""
    if not isinstance(rt, dict) or not rt:
        return

    icon, label = _REDTEAM_STATUS.get(rt.get("status"), ("•", "状态未知"))
    st.markdown(f"##### 🥊 红队 · {icon} {label}")
    if rt.get("note"):
        st.caption(rt["note"])

    counter = rt.get("counter_arguments") or []
    if counter:
        st.dataframe(pd.DataFrame([{
            "反方论证": c.get("point"),
            "引用": c.get("evidence_ref"),
            "依据": (c.get("evidence") or {}).get("value") or "—",
            "为什么": c.get("why") or "—",
        } for c in counter]), width="stretch", hide_index=True)
    elif rt.get("status") == "llm_unavailable":
        st.caption("没有反方论证——不是因为没有反方，而是模型没给出可核对的说法。")

    dropped = rt.get("dropped") or []
    if dropped:
        with st.expander(f"被丢弃的反方论证（{len(dropped)} 条，引用对不上）",
                         expanded=False):
            st.caption("这些是模型写了但挂不住引用的部分。留着是为了让你看见它编过什么，"
                       "它们不计入任何结论。")
            for d in dropped:
                st.caption(f"　· {d.get('point')}　——　{d.get('reason')}")

    frag = rt.get("fragile_assumption")
    if isinstance(frag, dict):
        st.markdown("**🪜 最脆弱的一环**")
        st.caption(f"{frag.get('assumption')}　（引用 `{frag.get('evidence_ref')}`："
                   f"{(frag.get('evidence') or {}).get('value') or '—'}）")
        if frag.get("why"):
            st.caption(frag["why"])
    else:
        st.caption("🪜 最脆弱的一环：本次没有可判定的已验假设。")

    oq = rt.get("open_questions") or []
    if oq:
        st.markdown("**❓ 未解决问题（不知道就是不知道）**")
        for q in oq:
            st.caption(f"　· {q}")
