# -*- coding: utf-8 -*-
"""固收起投研看板（Phase 5，JD 三职责）。

3 个 tab 分别对应：
  ① 主体信用评级（JD 职责①）—— Altman Z'' / Ohlson O / 关键比率 / Merton 违约距离
  ② 利率与情景引擎（JD 职责②）—— NSS 因子 / 政策立场 / 情景库 / Monte Carlo / 压力重演
  ③ AI 固收对话（JD 职责③）—— NL 问句 → agent 编排 → 投研判断 + 建议 + 风险信号

诚实边界（约束#7）：
  - 数值一律来自真实引擎（EDGAR / Treasury / FOMC 官方源 + 真实计算），不可用如实标 "—"/原因。
  - 首屏只会做轻量单日拟合（day_fit，&lt;1s）做到秒开；全路径因子 / Monte Carlo / 压力重演 /
    agent 均为「按钮触发 + spinner + 缓存」，避免 NSS 全量 28s 卡死加载。

启动：D:\\python\\python.exe -m streamlit run ui/dashboard.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import streamlit as st

from app.config import get_settings
from app.credit import features as F
from app.credit import structural as S
from app.data import fixed_income
from app.rates import curve as C
from app.rates import policy as P
from app.rates import scenario as SC


# ── 格式化 ──────────────────────────────────────────────

def _num(v, nd=3) -> str:
    """None / NaN → '—'，否则保留 nd 位。"""
    try:
        if v is None or pd.isna(v):
            return "—"
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _pct(v, nd=2) -> str:
    """小数比率 → 百分数。"""
    try:
        if v is None or pd.isna(v):
            return "—"
        return f"{float(v) * 100:.{nd}f}%"
    except (TypeError, ValueError):
        return str(v)


# ── ① 主体信用 ──────────────────────────────────────────

@st.cache_data(ttl=600, show_spinner="拉取 EDGAR 财务与结构分…")
def _credit(ticker: str) -> dict:
    fi = fixed_income.get_fixed_income_data()
    try:
        panel = fi.get_credit_panel(ticker)
    except Exception as e:
        return {"ok": False, "reason": f"EDGAR 不可用：{type(e).__name__}: {e}"}
    if panel is None or panel.empty:
        return {"ok": False, "reason": "EDGAR 财务为空（无此主体财务披露或代码不对）"}
    ff = F.build_feature_frame(panel)
    if ff is None or ff.empty or ff["altman_z"].dropna().empty:
        return {"ok": False, "reason": "结构分不可算（缺 Altman Z'' 输入项）"}

    last = ff.iloc[-1]
    table = ff[
        ["altman_z", "ohlson_o", "debt_ratio", "debt_to_equity", "current_ratio",
         "net_margin", "roa", "roe", "ebit_margin"]
    ].copy()
    table["end"] = [d.strftime("%Y-%m-%d") for d in ff.index]
    table = table.set_index("end")

    zone = "灰/风险区（Altman Z'' < 2.60）" if last["label"] == 1 else "安全区（Z'' ≥ 2.60）"

    # Merton：市场数据可用才给；缺失如实标注（约束#7）
    merton = {"available": False, "reason": "未尝试市场数据"}
    try:
        from app.data.sources.yfinance_source import YFinanceSource
        yf = YFinanceSource()
        info = yf.fetch_company_info(ticker)
        cap = info.get("market_cap")
        h = yf.fetch_price_history(ticker, start="2024-01-01", end="2026-08-31")
        vol = S.equity_vol_from_history(h["Close"].to_numpy()) if not h.empty else None
        # 负债面值取自科目面板（feature frame 不携带 liabilities，取到恒为 None → Merton 恒不可用）。
        p_last = panel.iloc[-1]
        debt = p_last["liabilities"] if pd.notna(p_last.get("liabilities")) else None
        m = S.merton_from_market(cap, debt, vol)
        if m.get("available"):
            merton = {"available": True, "dd": m["dd"], "pd": m["pd"],
                      "sigma_V": m["sigma_V"], "V": m["V"]}
        else:
            merton = {"available": False, "reason": m.get("reason")}
    except Exception as e:
        merton = {"available": False, "reason": f"市场数据不可用（{type(e).__name__}）"}

    return {
        "ok": True, "ticker": ticker,
        "as_of_end": str(panel.index[-1].date()),
        "filed": str(last["filed"])[:10] if pd.notna(last.get("filed")) else "—",
        "altman_z": last["altman_z"], "ohlson_o": last["ohlson_o"],
        "debt_ratio": last.get("debt_ratio"), "net_margin": last.get("net_margin"),
        "debt_to_equity": last.get("debt_to_equity"),
        "current_ratio": last.get("current_ratio"),
        "roa": last.get("roa"), "roe": last.get("roe"),
        "zone": zone, "label": last["label"],
        "table": table, "merton": merton,
    }


def _credit_fig(table: pd.DataFrame) -> object | None:
    import plotly.graph_objects as go
    if table is None or table.empty:
        return None
    idx = table.index.tolist()
    fig = go.Figure()
    for col, color in [("altman_z", "#c0392b"), ("ohlson_o", "#2c3e50")]:
        fig.add_trace(go.Scatter(x=idx, y=table[col], mode="lines+markers",
                                 name=col, line=dict(color=color, width=2)))
    fig.add_hline(y=2.60, line_dash="dash", line_color="#16a34a",
                  annotation_text="Altman Z'' 安全界线 2.60")
    fig.update_layout(height=320, margin=dict(l=0, r=0, t=30, b=0),
                      xaxis_title=None, yaxis_title="结构分",
                      hovermode="x unified")
    return fig


def _page_credit() -> None:
    st.subheader("① 主体信用评级")
    st.caption("JD 职责① —— 用公开财务 + 结构模型（Altman Z'' / Ohlson O / Merton）评估海外发债主体"
               "的信用风险。**代理标签**（Z''<2.60 灰/风险区），非评级机构评级。")

    t = st.text_input("美股债券主体 ticker（如 AAPL / F / W）", value="F",
                      key="fi_credit_ticker").strip().upper()
    if not t:
        return
    payload = _credit(t)
    if not payload.get("ok"):
        st.warning(payload.get("reason", "数据不可用"))
        return

    c0, c1, c2 = st.columns([1, 1, 2])
    c0.info(f"**as_of** 财报年度 {payload['as_of_end']}")
    c0.caption(f"filed {payload['filed']}（真实披露日，防 look-ahead）")
    c1.metric("Altman Z''", _num(payload["altman_z"], 3))
    c1.caption("＞2.60 安全 ｜ 1.10~2.60 灰区 ｜ ＜1.10 风险")
    c2.metric("Ohlson O", _num(payload["ohlson_o"], 3))
    c2.caption("O＞0 提示财务困境（连续风险特征，非标签）")

    c3, c4, c5, c6 = st.columns(4)
    c3.metric("债务比率", _num(payload["debt_ratio"], 3))
    c4.metric("净利率", _pct(payload["net_margin"]))
    c5.metric("负债/权益", _num(payload["debt_to_equity"], 2))
    c6.metric("流动比率", _num(payload["current_ratio"], 2))

    zone_color = "#c0392b" if payload["label"] == 1 else "#16a34a"
    st.markdown(
        f'<span style="background:{zone_color};color:#fff;padding:2px 12px;'
        f'border-radius:12px;font-weight:600">结构风险区　{payload["zone"]}</span>'
        f'　<small>※ 结构模型代理标签，非评级机构评级；无商业评级 feed。</small>',
        unsafe_allow_html=True)

    m = payload["merton"]
    st.markdown("**Merton 违约距离**（股权市值 + 波动率，需市场数据）")
    if m.get("available"):
        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("违约距离 DD", _num(m["dd"], 3))
        mc2.metric("隐含违约概率 PD", _pct(m["pd"], 4))
        mc3.metric("资产波动率 σ_V", _num(m["sigma_V"], 4))
        mc4.metric("资产价值 V", f"{m['V'] / 1e9:.2f}B" if m.get("V") else "—")
    else:
        st.warning(f"Merton 不可用：{m.get('reason')}")

    st.divider()
    fig = _credit_fig(payload["table"])
    if fig is not None:
        st.plotly_chart(fig, width="stretch")
    st.markdown("**关键会计比率（逐年，点对点 filed）**")
    tbl = payload["table"]
    # 全字符串化显示：列内 "—"/浮点混合会让 Arrow 序列化报错，统一转字符串。
    tbl_disp = tbl.map(lambda v: "—" if pd.isna(v) else f"{float(v):.3f}")
    st.dataframe(tbl_disp, width="stretch")


# ── ② 利率与情景 ────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner="构造当前曲线（单日拟合）…")
def _curve_snapshot() -> dict:
    fi = fixed_income.get_fixed_income_data()
    try:
        curve = fi.get_yield_curve(months=13)
    except Exception as e:
        return {"ok": False, "reason": f"Treasury 不可用：{type(e).__name__}: {e}"}
    if curve is None or curve.empty:
        return {"ok": False, "reason": "Treasury 曲线为空"}
    day = C.day_fit(curve)
    tenors = {}
    for tenor in ("2 Yr", "10 Yr", "30 Yr"):
        if tenor in curve.columns:
            v = curve[tenor].iloc[-1]
            tenors[tenor] = None if pd.isna(v) else round(float(v), 3)
    return {"ok": True, "day": day, "as_of": str(curve.index.max().date()),
            "tenors": tenors, "curve_ok": bool(day.get("ok"))}


def _policy_stance() -> dict:
    fi = fixed_income.get_fixed_income_data()
    stmt = fi.get_policy_signal()
    return P.PolicySignal().score_statement(stmt)


@st.cache_data(ttl=600, show_spinner="NSS 因子拟合（约 20~30s）…")
def _factors(months: int = 13) -> pd.DataFrame:
    fi = fixed_income.get_fixed_income_data()
    curve = fi.get_yield_curve(months=months)
    if curve is None or curve.empty:
        return pd.DataFrame()
    return C.fit_curve_path(curve)


@st.cache_data(ttl=300, show_spinner="情景库构造…")
def _scenario(months: int = 13) -> dict:
    factors = _factors(months)
    if factors.empty:
        return {"ok": False, "reason": "因子为空"}
    eng = SC.ScenarioEngine(factors)
    scen = eng.scenarios(forecast_dates=[str(factors.index[-1].date())])
    return {"ok": bool(scen.get("ok")), "scen": scen, "summ": eng.summary()}


@st.cache_data(ttl=600, show_spinner="Monte Carlo 模拟…")
def _monte_carlo(months: int = 13, n_paths: int = 200, horizon: int = 21) -> dict:
    factors = _factors(months)
    if factors.empty:
        return {"ok": False, "reason": "因子为空"}
    return SC.ScenarioEngine(factors).monte_carlo(n_paths=n_paths, horizon=horizon)


@st.cache_data(ttl=600, show_spinner="压力重演（须 84 月长历史）…")
def _stress_replay() -> dict:
    fi = fixed_income.get_fixed_income_data()
    # 长历史：若网络不可达，treasury_source 已设 8s 超时 + 连败 4 次即退，避免长期卡死
    curve = fi.get_yield_curve(months=84)
    if curve is None or curve.empty:
        return {"ok": False, "reason": "长历史 Treasury 为空（缓存到期/网络未回源）"}
    factors = C.fit_curve_path(curve)
    if factors.empty:
        return {"ok": False, "reason": "长历史 NSS 拟合失败"}
    return SC.ScenarioEngine(factors).stress_replay(curve)


def _curve_fig(day: dict) -> object | None:
    import plotly.graph_objects as go
    if not day or not day.get("ok"):
        return None
    mat = day.get("maturities") or []
    yld = day.get("yields") or []
    fitted = day.get("fitted")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=mat, y=yld, mode="markers+lines", name="观测即期收益",
                             marker=dict(color="#2c3e50", size=8)))
    if fitted is not None:
        fig.add_trace(go.Scatter(x=mat, y=fitted, mode="lines", name="NSS 拟合",
                                 line=dict(color="#e74c3c", width=2, dash="dot")))
    fig.update_layout(height=340, margin=dict(l=0, r=0, t=30, b=0),
                      xaxis_title="到期年限 (年)", yaxis_title="收益率 (%)",
                      hovermode="closest",
                      title=dict(text=f"拟合 R² = {day.get('r2')}",
                                 font=dict(size=13)))
    return fig


def _scenario_fig(scen: dict, grid: list) -> object | None:
    import plotly.graph_objects as go
    if not scen or "scenarios" not in scen:
        return None
    fig = go.Figure()
    colors = {"基准": "#2c3e50", "乐观(下行利率)": "#16a34a", "悲观(上行利率)": "#c0392b",
              "极端激进": "#7f1d1d", "极端避险(长端降)": "#1f4e79"}
    for name, d in scen.get("scenarios", {}).items():
        path = d.get("path") or [[]]
        y = path[0]
        fig.add_trace(go.Scatter(x=grid, y=y, mode="lines+markers", name=name,
                                 line=dict(color=colors.get(name, "#7f8c8d"), width=2)))
    fig.update_layout(height=360, margin=dict(l=0, r=0, t=30, b=0),
                      xaxis_title="到期年限 (年)", yaxis_title="收益率 (%)",
                      hovermode="closest",
                      title=dict(text=scen.get("shock_assumption", ""), font=dict(size=11)))
    return fig


def _mc_fig(mc: dict) -> object | None:
    import plotly.graph_objects as go
    if not mc or not mc.get("ok"):
        return None
    x = mc.get("horizon_dates") or []
    pct = mc.get("percentiles") or {}
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=pct.get("level_p90") or [], name="p90",
                             line=dict(color="#7f8c8d", width=1), fill=None))
    fig.add_trace(go.Scatter(x=x, y=pct.get("level_p10") or [], name="p10",
                             line=dict(color="#7f8c8d", width=1)))
    st_band = go.Scatter(x=x + x[::-1],
                         y=(pct.get("level_p90") or []) + (pct.get("level_p10") or [])[::-1],
                         fill="toself", fillcolor="rgba(120,144,156,0.25)",
                         line=dict(color="rgba(255,255,255,0)"), hoverinfo="skip",
                         showlegend=False)
    fig.add_trace(st_band)
    fig.add_trace(go.Scatter(x=x, y=pct.get("level_p50") or [], name="p50",
                             line=dict(color="#e74c3c", width=2)))
    fig.update_layout(height=340, margin=dict(l=0, r=0, t=30, b=0),
                      xaxis_title="未来步", yaxis_title="水平因子 (level)",
                      hovermode="closest")
    return fig


def _stress_table(d: dict) -> pd.DataFrame:
    rows = []
    for name, ev in d.items():
        if ev.get("ok"):
            rows.append({"事件": name, "窗口": f"{ev['window'][0]}~{ev['window'][1]}",
                         "水平Δ(bp)": ev["level_delta_bp"],
                         "斜率Δ(bp)": ev["slope_delta_bp"],
                         "水平始/末": f"{ev.get('level_start')}→{ev.get('level_end')}"})
        else:
            rows.append({"事件": name, "窗口": "—", "水平Δ(bp)": "—", "斜率Δ(bp)": "—",
                         "水平始/末": ev.get("reason", "不可用")})
    return pd.DataFrame(rows)


def _stress_fig(d: dict) -> object | None:
    import plotly.graph_objects as go
    df = _stress_table(d)
    ok = df[df["水平Δ(bp)"].apply(lambda v: isinstance(v, (int, float)))]
    if ok.empty:
        return None
    fig = go.Figure()
    fig.add_trace(go.Bar(name="水平Δ(bp)", x=ok["事件"], y=ok["水平Δ(bp)"],
                         marker_color="#e74c3c"))
    fig.add_trace(go.Bar(name="斜率Δ(bp)", x=ok["事件"], y=ok["斜率Δ(bp)"],
                         marker_color="#2c3e50"))
    fig.update_layout(barmode="group", height=320,
                      margin=dict(l=0, r=0, t=30, b=0), yaxis_title="Δ(bp)")
    return fig


def _page_rates() -> None:
    st.subheader("② 利率与情景引擎")
    st.caption("JD 职责② —— NSS 把整条国债曲线降维成水平/斜率/曲率，量化政策信号→收益率关系；"
               "情景库/ Monte Carlo / 压力重演做成可解释、可复核的利率情景框架。")

    snap = _curve_snapshot()
    if not snap.get("ok"):
        st.warning(snap.get("reason", "曲线不可用"))
        return
    day = snap["day"]
    p = (day.get("params") or {})
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("当前 as_of", snap["as_of"])
    c2.metric("水平 level", _num(p.get("level"), 3))
    slope = p.get("slope")
    dir_txt = "" if slope is None else ("↑ 正常向上" if slope < 0 else "↓ 倒挂")
    c3.metric("斜率 slope", f"{_num(slope, 3)} {dir_txt}")
    c4.metric("曲率 curvature", _num(p.get("curvature"), 3))
    c5.metric("拟合 R²", _num(day.get("r2"), 4))

    stob = _policy_stance()
    with st.expander("📜 最新 FOMC 政策立场（词典法，非官方口径）", expanded=True):
        if stob.get("ok"):
            psc, psc2, psc3 = st.columns(3)
            psc.metric("立场", stob.get("stance", "—"))
            psc2.metric("鸽鹰分", _num(stob.get("score"), 4))
            psc3.metric("会议日期", stob.get("meeting_date", "—"))
            st.caption(f"鹰词命中 {stob.get('num_hawk_tokens')} · "
                       f"鸽词命中 {stob.get('num_dove_tokens')} · "
                       f"方法：{stob.get('method', '词典法')}")
            hawk = stob.get("hawkish_hits") or {}
            dove = stob.get("dovish_hits") or {}
            if hawk or dove:
                st.markdown(f"**鹰词**: {', '.join(f'{k}×{v}' for k, v in hawk.items()) or '—'}")
                st.markdown(f"**鸽词**: {', '.join(f'{k}×{v}' for k, v in dove.items()) or '—'}")
        else:
            st.warning(stob.get("reason", "FOMC 声明不可用"))

    snap_fig = _curve_fig(day)
    if snap_fig is not None:
        st.plotly_chart(snap_fig, width="stretch")

    ten = snap.get("tenors") or {}
    if ten:
        k1, k2, k3 = st.columns(3)
        k1.metric("2Y", _num(ten.get("2 Yr"), 3))
        k2.metric("10Y", _num(ten.get("10 Yr"), 3))
        k3.metric("30Y", _num(ten.get("30 Yr"), 3))

    st.divider()
    st.markdown("**情景库**（冲击量为分析师假设，非预测）")
    scen = _scenario()
    if scen.get("ok"):
        s = scen["scen"]; grid = s.get("maturities") or []
        fig = _scenario_fig(s, grid)
        if fig is not None:
            st.plotly_chart(fig, width="stretch")
        scen10 = {}
        for name, d in s.get("scenarios", {}).items():
            path = d.get("path") or [[]]
            if grid and path and len(path[0]) == len(grid):
                _, _, _, _, _, ten_idx = _tenor_grid_index(grid)
                scen10[name] = (None if ten_idx is None else
                                round(float(path[0][ten_idx]), 3))
        if scen10:
            cols = st.columns(max(1, len(scen10)))
            for col, (name, v) in zip(cols, scen10.items()):
                col.metric(name, "—" if v is None else f"{v:.2f}%")
    else:
        st.warning(scen.get("reason", "情景库不可用"))

    st.divider()
    st.markdown("**Monte Carlo —— 水平因子路径（差分 VAR + 正则化协方差）**")
    if st.button("生成 200 条路径（21 步）", key="fi_mc_btn"):
        mc = _monte_carlo(n_paths=200, horizon=21)
        if mc.get("ok"):
            fig = _mc_fig(mc)
            if fig is not None:
                st.plotly_chart(fig, width="stretch")
            st.caption(f"{mc.get('n_paths')} 路径 · 最近真实观测出发 · " + mc.get("note", ""))
        else:
            st.warning(mc.get("reason", "Monte Carlo 不可用"))

    st.divider()
    st.markdown("**历史压力重演**（2020 疫情 / 2022 激进加息 / 2008 危机）")
    st.caption("需 84 月长历史 Treasury；网络未回源或缓存到期时如实显示不可用。")
    if st.button("重演历史压力", key="fi_stress_btn"):
        stress = _stress_replay()
        if stress.get("ok"):
            ev = stress.get("events") or {}
            fig = _stress_fig(ev)
            if fig is not None:
                st.plotly_chart(fig, width="stretch")
            sdf = _stress_table(ev)
            for _c in ("水平Δ(bp)", "斜率Δ(bp)"):
                if _c in sdf.columns:
                    sdf[_c] = sdf[_c].map(
                        lambda v: "—" if isinstance(v, str) else f"{float(v):.1f}")
            st.dataframe(sdf, width="stretch", hide_index=True)
            st.caption(stress.get("note", ""))
        else:
            st.warning(stress.get("reason", "压力重演不可用"))


def _tenor_grid_index(grid: list) -> tuple:
    """定位 10Y 在情景 grid 中的下标（grid 为年限数组）。返回 (10, 5, 2, 10, 30, idx)。"""
    arr = np.asarray(grid, dtype=float)
    idx = int(np.argmin(np.abs(arr - 10.0))) if len(arr) else None
    return 10, 5, 2, 10, 30, idx


# ── ③ AI 固收对话 ──────────────────────────────────────

def _agent_run(query: str) -> dict:
    from app.agent import fixed_income as FI
    return FI.run(query)


def _page_agent() -> None:
    st.subheader("③ AI 固收对话")
    st.caption("JD 职责③ —— 自然语言问句 → agent 路由固收工具（①信用 / ②利率情景 / ③RAG）→ "
               "LLM 依据工具返回的真实数值综合出投研判断、可执行建议与风险信号。")

    llm_key = bool(get_settings().LLM_API_KEY.strip())
    if not llm_key:
        st.warning("LLM 密钥未配置（LLM_API_KEY 为空），AI 对话暂不可用。")
        return

    q = st.text_input("问句", key="fi_agent_q",
                      placeholder="如：评估福特(F)信用风险，给出建议与风险信号")
    c1, c2 = st.columns([1, 1])
    run_click = c1.button("跑 Agent", type="primary", key="fi_agent_run")
    c2.button("清空", key="fi_agent_clear")

    if run_click and q.strip():
        with st.spinner("Agent 编排中（工具调用）…"):
            try:
                res = _agent_run(q.strip())
                st.session_state["fi_agent_out"] = res
            except Exception as e:
                st.error(f"Agent 失败：{type(e).__name__}: {e}")

    out = st.session_state.get("fi_agent_out")
    if not out:
        st.info("输入问句并点击「跑 Agent」。")
        return

    calls = out.get("tool_calls") or []
    if calls:
        st.markdown("**工具调用链（LLM 实际路由）**")
        st.markdown(" → ".join(c.get("name", "?") for c in calls))
    text = out.get("text", "")
    if text:
        st.markdown("**Agent 综合输出**")
        st.markdown(text)


_SEV_COLOR = {"高": "#c0392b", "中": "#e67e22", "低": "#7f8c8d"}


@st.cache_data(ttl=600, show_spinner="综合行动方案…")
def _action_payload(ticker: str) -> dict:
    from app.agent import fixed_income as FI
    return json.loads(FI.tool_executor("action_plan", {"ticker": ticker}))


def _badge(text: str, color: str) -> str:
    return (f'<span style="background:{color};color:#fff;padding:2px 8px;'
            f'border-radius:10px;font-size:0.82rem">{text}</span>')


def _page_action() -> None:
    st.subheader("④ 行动 · 风险 · 合规")
    st.caption("JD 职责③第二部分 —— 把信用/利率/政策引擎输出转成**可执行**行动：信用观点 + "
               "久期/曲线倾向 + 风险信号 + 合规复核队列。确定性规则、无 LLM；无债券价格 feed，"
               "动作以久期/曲线层面表达，不给出价格级止损止盈。")

    t = st.text_input("主体 ticker", value="F", key="fi_action_ticker").strip().upper()
    if not t:
        return
    out = _action_payload(t)
    if not out.get("available"):
        st.warning("行动方案不可用。")
        return

    st.caption(f"as_of {out.get('as_of', '—')} · 主体 {out.get('issuer', t)}")

    cred = out.get("credit_view", {})
    rv = out.get("rate_view", {})
    syn = out.get("synthesis", {})
    tier = cred.get("tier")
    tier_color = {"安全区": "#16a34a", "灰区": "#e67e22", "风险区": "#c0392b"}.get(tier, "#7f8c8d")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("信用区", tier or "—")
    c1.caption(f"Z''={cred.get('z', '—')} · O={cred.get('o', '—')}")
    c2.metric("久期倾向", (rv.get("duration_tilt") or "—").split("（")[0])
    c2.caption(f"政策 {rv.get('policy_stance', '—')} · score={rv.get('policy_score', '—')}")
    c3.metric("关注度", syn.get("position_hint", "—"))
    c4.metric("置信度", syn.get("confidence_level", "—"))

    cv = syn.get("tier")
    st.markdown(
        f'<span style="background:{tier_color};color:#fff;padding:2px 12px;'
        f'border-radius:12px;font-weight:600">信用观点 · {tier or "数据不足"}</span>'
        f'　<span style="color:#555">·　久期 {rv.get("duration_tilt", "—")}</span>'
        f'　<span style="color:#555">·　曲线 {rv.get("curve_shape", "—") or "—"}</span>',
        unsafe_allow_html=True)

    st.divider()
    st.markdown("**风险信号**")
    risk = out.get("risk_signals", [])
    if risk:
        for r in risk:
            sev = r.get("severity", "中")
            color = _SEV_COLOR.get(sev, "#7f8c8d")
            st.markdown(
                f'{_badge(sev, color)}　**{r.get("name", "")}**　'
                f'<small>触发：{r.get("trigger", "")} · 来源:{r.get("source", "")}</small>',
                unsafe_allow_html=True)
    else:
        st.info("当前无风险信号。")

    st.divider()
    st.markdown("**合规复核队列**")
    queue_items = out.get("compliance_queue", [])
    for qi in queue_items:
        sev = qi.get("severity", "中")
        color = _SEV_COLOR.get(sev, "#7f8c8d")
        st.markdown(
            f'{_badge(sev, color)}　**{qi.get("item", "")}**　'
            f'<small>{qi.get("reason", "")}</small>', unsafe_allow_html=True)

    if st.button("登记到复核队列（写回 DB）", key="fi_action_flag"):
        from app.compliance import queue as cq
        n = 0
        for qi in queue_items:
            cq.flag(source=f"fixed_income_action:{t}",
                    content_hash=hashlib.sha256(qi.get("item", "").encode("utf-8")).hexdigest()[:16],
                    snippet=qi.get("item", ""), hit_words=[qi.get("reason", "")], advice_flag=0)
            n += 1
        st.success(f"已登记 {n} 项复核。")

    with st.expander("🔎 数据库待复核项（fixed_income）", expanded=False):
        from app.compliance import queue as cq
        rows = [r for r in cq.list_flagged(reviewed=0, limit=100)
                if (r.get("source") or "").startswith("fixed_income_action")]
        if not rows:
            st.info("无待复核的固收记录。可先点「登记到复核队列」。")
        else:
            for r in rows:
                c1_, c2_ = st.columns([1, 5])
                if c1_.checkbox("已复核", key=f"fi_rev_{r['id']}"):
                    cq.mark_reviewed(r["id"])
                c2_.caption(f"#{r['id']} {r.get('snippet', '')[:90]} · 来源 {r.get('source')}")

    st.divider()
    st.markdown("**推荐行动文本**")
    st.markdown(out.get("advice_text", ""))


def page() -> None:
    """固收看板入口：4 个 tab 对应 JD 三职责 + 行动化。"""
    st.header("🧱 固收研究")
    st.caption("JD 三职责：① 主体信用评级 · ② 利率与情景引擎 · ③ AI 固收对话 · "
               "④ 行动·风险·合规。全部基于真实引擎计算；某数据源不可用时如实显示，不编造。")
    tab1, tab2, tab3, tab4 = st.tabs(["① 主体信用", "② 利率与情景", "③ AI 固收对话",
                                      "④ 行动·风险·合规"])
    with tab1:
        _page_credit()
    with tab2:
        _page_rates()
    with tab3:
        _page_agent()
    with tab4:
        _page_action()
