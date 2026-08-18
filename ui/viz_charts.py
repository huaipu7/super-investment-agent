"""看板可视化图表纯函数：输入深度研究 result dict → plotly Figure / DataFrame。

与 streamlit 运行时解耦（本模块不含 st.*），可在无后端时离线单测。
数据全部来自深度研究分析结果，空数据一律优雅降级（返回 None / 空 DataFrame）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from app.analysis.financial import RATIO_CATEGORIES
from app.constants import FINANCIAL_METRICS, FINANCIAL_RATIOS

# 估值对比（valuation_analysis.relative_valuation）的键，按展示顺序
_VAL_KEYS = ["PE", "PB", "PS", "EV_EBITDA"]

# 财务规模柱状图的默认指标顺序（raw_metrics 里的绝对额）
_RAW_ORDER = ["REVENUE", "NET_INCOME", "EBITDA", "OPERATING_INCOME",
              "TOTAL_ASSETS", "TOTAL_EQUITY"]


def metric_name(code: str) -> str:
    """指标代码 → 中文名（财务规模表）。"""
    return FINANCIAL_METRICS.get(code, code)


def _ratio_name(code: str) -> str:
    return FINANCIAL_RATIOS.get(code, code)


def available_categories(fin: dict | None) -> list[str]:
    """返回 result 中实际有数据的比率分类（按 RATIO_CATEGORIES 过滤）。"""
    ratios = (fin or {}).get("ratios", {}) or {}
    return [cat for cat, codes in RATIO_CATEGORIES.items()
            if any(ratios.get(c) for c in codes)]


def raw_metric_choices(fin: dict | None) -> list[str]:
    """返回实际存在的财务规模指标代码（按固定顺序）。"""
    raw = (fin or {}).get("raw_metrics", {}) or {}
    return [c for c in _RAW_ORDER if c in raw]


def _series_values(series: list[dict]) -> tuple[list[str], list]:
    periods, values = [], []
    for s in series or []:
        periods.append(str(s.get("period", "")))
        values.append(s.get("value"))
    return periods, values


# ── 折线 ──────────────────────────────────────────────

def price_line_fig(hist: dict | None):
    """收盘价折线（来自 /history，秒开不依赖分析）。无数据返回 None。"""
    import plotly.graph_objects as go

    records = (hist or {}).get("records", [])
    if not records:
        return None
    dates = [r["date"] for r in records]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dates, y=[r["close"] for r in records],
                             mode="lines", name="收盘价",
                             line=dict(width=2, color="#1f77b4")))
    fig.update_layout(height=420, margin=dict(l=0, r=0, t=30, b=0),
                      xaxis_title="日期", yaxis_title="价格",
                      hovermode="x unified")
    return fig


def ratio_trend_fig(fin: dict | None, category: str):
    """折线：该分类下每个比率的多年趋势。"""
    import plotly.graph_objects as go

    ratios = (fin or {}).get("ratios", {}) or {}
    fig = go.Figure()
    for code in RATIO_CATEGORIES.get(category, []):
        series = ratios.get(code) or []
        if not series:
            continue
        periods, values = _series_values(series)
        fig.add_trace(go.Scatter(x=periods, y=values, mode="lines+markers",
                                 name=_ratio_name(code), connectgaps=False))
    fig.update_layout(height=420, margin=dict(l=0, r=0, t=30, b=0),
                      hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))
    return fig


# ── 柱状 ──────────────────────────────────────────────

def raw_metric_bars_fig(fin: dict | None, codes: list[str]):
    """柱状：财务规模指标按年分组柱（单位：亿元）。"""
    import plotly.graph_objects as go

    raw = (fin or {}).get("raw_metrics", {}) or {}
    fig = go.Figure()
    for code in codes or []:
        series = raw.get(code) or []
        if not series:
            continue
        periods, values = _series_values(series)
        fig.add_trace(go.Bar(x=periods, name=metric_name(code),
                             y=[None if v is None else v / 1e8 for v in values]))
    fig.update_layout(height=420, barmode="group",
                      margin=dict(l=0, r=0, t=30, b=0), yaxis_title="亿元",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))
    return fig


def valuation_bar_fig(val: dict | None):
    """柱状：PE/PB/PS/EV_EBITDA 公司 vs 行业均值。无数据返回 None。"""
    import plotly.graph_objects as go

    rv = (val or {}).get("relative_valuation", {}) or {}
    names, company, industry = [], [], []
    for key in _VAL_KEYS:
        item = rv.get(key.lower())
        if not item:
            continue
        cv, iv = item.get("company"), item.get("industry_avg")
        if cv is None and iv is None:
            continue
        names.append(_ratio_name(key))
        company.append(cv)
        industry.append(iv)
    if not names:
        return None
    fig = go.Figure()
    fig.add_trace(go.Bar(x=names, y=company, name="公司"))
    fig.add_trace(go.Bar(x=names, y=industry, name="行业均值"))
    fig.update_layout(height=420, barmode="group",
                      margin=dict(l=0, r=0, t=30, b=0),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))
    return fig


# ── 饼图 ──────────────────────────────────────────────

def market_pie_fig(market: dict | None):
    """饼图：公司 + 竞品营收份额。无竞品/无营收数据返回 None（不编造）。"""
    import plotly.graph_objects as go

    if not market:
        return None
    comps = market.get("competitors") or []
    comp = market.get("company") or {}
    names, values = [], []
    if comp.get("revenue") is not None:
        names.append(comp.get("name") or "本公司")
        values.append(comp["revenue"])
    for c in comps:
        if c.get("revenue") is not None:
            names.append(c.get("name") or "竞品")
            values.append(c["revenue"])
    if not names:
        return None
    fig = go.Figure(go.Pie(labels=names, values=values, hole=0.4))
    fig.update_layout(height=420, margin=dict(l=0, r=0, t=30, b=0),
                      title_text="营收份额（公司 vs 竞品）")
    return fig


# ── 箱型 ──────────────────────────────────────────────

def ratio_box_fig(fin: dict | None, category: str):
    """箱型：该分类下每个比率的跨年分布（年份点 ≥3 才画）。"""
    import plotly.graph_objects as go

    ratios = (fin or {}).get("ratios", {}) or {}
    fig = go.Figure()
    for code in RATIO_CATEGORIES.get(category, []):
        values = [s.get("value") for s in (ratios.get(code) or [])
                  if s.get("value") is not None]
        if len(values) < 3:
            continue
        fig.add_trace(go.Box(y=values, name=_ratio_name(code),
                             boxpoints="all", jitter=0.3, pointpos=0))
    fig.update_layout(height=420, margin=dict(l=0, r=0, t=30, b=0),
                      showlegend=False)
    return fig


# ── 表格 ──────────────────────────────────────────────

def raw_metrics_df(fin: dict | None) -> pd.DataFrame:
    """财务规模表：行=指标中文名，列=年份，单位：亿元。"""
    raw = (fin or {}).get("raw_metrics", {}) or {}
    rows = {}
    for code, series in raw.items():
        rows[metric_name(code)] = {s["period"]: s["value"]
                                   for s in series if s.get("value") is not None}
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).T / 1e8
    df.index.name = "指标（亿元）"
    return df.round(2)


def ratios_df(fin: dict | None) -> pd.DataFrame:
    """财务比率表：行=比率中文名，列=年份。"""
    ratios = (fin or {}).get("ratios", {}) or {}
    rows = {}
    for code, series in ratios.items():
        rows[_ratio_name(code)] = {s["period"]: s["value"]
                                   for s in series if s.get("value") is not None}
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).T
    df.index.name = "财务比率"
    return df.round(4)


# ── K线交互（Phase 2）────────────────────────────────

def _norm_price_history(x: dict | None) -> dict | None:
    """归一化行情输入 → 列式 dict {dates,open,high,low,close,volume}|None。

    接受两种来源：管线 result.price_history（列式，含 available）或
    /history 返回的 records 列表。结构无效返回 None（不编造）。
    """
    if not isinstance(x, dict):
        return None
    if isinstance(x.get("records"), list) and x["records"]:
        rec = x["records"]
        return {"available": True,
                "dates": [r["date"] for r in rec],
                "open": [r.get("open") for r in rec],
                "high": [r.get("high") for r in rec],
                "low": [r.get("low") for r in rec],
                "close": [r.get("close") for r in rec],
                "volume": [r.get("volume") or 0 for r in rec]}
    if x.get("available") is True and x.get("close"):
        return x
    return None


def _hist_df(price_history: dict | None, freq: str = "D") -> pd.DataFrame | None:
    """列式行情 → OHLCV DataFrame，按 freq 重采样（D/W/M）。不足 30 根返回 None。"""
    ph = _norm_price_history(price_history)
    if ph is None:
        return None
    try:
        n = len(ph["close"])
        df = pd.DataFrame({
            "Open": [float(v) if v is not None else float("nan") for v in ph["open"]],
            "High": [float(v) if v is not None else float("nan") for v in ph["high"]],
            "Low": [float(v) if v is not None else float("nan") for v in ph["low"]],
            "Close": [float(v) if v is not None else float("nan") for v in ph["close"]],
            "Volume": [float(v) if v is not None else 0.0 for v in ph.get("volume", [0] * n)],
        }, index=pd.to_datetime(ph["dates"]))
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 30:
            return None
        if freq in ("W", "M"):
            agg = {"Open": "first", "High": "max", "Low": "min",
                   "Close": "last", "Volume": "sum"}
            offset = "ME" if freq == "M" else "W"  # pandas 3.0 起月线偏移符为 ME
            df = df.resample(offset).agg(agg).dropna()
            if len(df) < 10:
                return None
        return df
    except Exception:
        return None


def kline_ohlc_fig(price_history: dict | None, trade_plan: dict | None = None,
                   freq: str = "D"):
    """交互 K线：主图 K线+MA+交易计划价位，副图 成交量 / MACD / RSI。

    freq ∈ {"D","W","M"} 周期切换。无数据返回 None（降级，不抛异常）。
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    from app.analysis.technical import indicator_series

    df = _hist_df(price_history, freq)
    if df is None:
        return None
    s = indicator_series(df)

    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.03,
        row_heights=[0.50, 0.15, 0.18, 0.17],
        subplot_titles=("日K线（红涨绿跌）", "成交量", "MACD(12,26,9)", "RSI(14)"))

    # 主图：K线 + 均线
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
        name="K线", increasing_line_color="#e74c3c", decreasing_line_color="#16a34a",
        increasing_fillcolor="#e74c3c", decreasing_fillcolor="#16a34a"), row=1, col=1)
    for key, color, name in (("ma5", "#f59e0b", "MA5"),
                             ("ma20", "#2563eb", "MA20"),
                             ("ma60", "#8b5cf6", "MA60")):
        ser = s.get(key)
        if ser is not None and not ser.dropna().empty:
            fig.add_trace(go.Scatter(x=df.index, y=ser, mode="lines", name=name,
                                     line=dict(width=1.2, color=color)), row=1, col=1)

    # 交易计划价位叠加（真实计算值；无则跳过）
    tp = trade_plan or {}
    if tp.get("available"):
        ez = tp.get("entry_zone") or [None, None]
        if len(ez) > 0 and ez[0] is not None:
            fig.add_hrect(y0=ez[0], y1=ez[1] if len(ez) > 1 and ez[1] is not None else ez[0],
                          fillcolor="rgba(22,163,74,0.10)", line_width=0,
                          annotation_text="进场区间", annotation_position="top left",
                          row=1, col=1)
        sl = tp.get("stop_loss")
        if sl is not None:
            fig.add_hline(y=sl, line_dash="dash", line_color="#dc2626", line_width=1,
                          annotation_text=f"止损 {sl:,.2f}", row=1, col=1)
        tpv = tp.get("take_profit")
        if tpv is not None:
            fig.add_hline(y=tpv, line_dash="dash", line_color="#2563eb", line_width=1,
                          annotation_text=f"止盈 {tpv:,.2f}", row=1, col=1)

    # 副图1：成交量（红涨绿跌）
    vol_colors = ["#e74c3c" if c >= o else "#16a34a"
                  for o, c in zip(df["Open"], df["Close"])]
    fig.add_trace(go.Bar(x=df.index, y=df["Volume"], name="成交量",
                         marker_color=vol_colors), row=2, col=1)

    # 副图2：MACD
    if s.get("macd_hist") is not None and not s["macd_hist"].dropna().empty:
        fig.add_trace(go.Bar(x=df.index, y=s["macd_hist"], name="MACD柱",
                             marker_color=["#e74c3c" if v >= 0 else "#16a34a"
                                           for v in s["macd_hist"].fillna(0)]), row=3, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=s["macd_dif"], name="DIF",
                                 line=dict(width=1, color="#2563eb")), row=3, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=s["macd_dea"], name="DEA",
                                 line=dict(width=1, color="#f59e0b")), row=3, col=1)

    # 副图3：RSI + 超买/超卖线
    if s.get("rsi14") is not None and not s["rsi14"].dropna().empty:
        fig.add_trace(go.Scatter(x=df.index, y=s["rsi14"], name="RSI(14)",
                                 line=dict(width=1, color="#8b5cf6")), row=4, col=1)
        fig.add_hline(y=70, line_dash="dot", line_color="#c0c0c0", row=4, col=1)
        fig.add_hline(y=30, line_dash="dot", line_color="#c0c0c0", row=4, col=1)

    freq_label = {"D": "日", "W": "周", "M": "月"}.get(freq, freq)
    fig.update_layout(height=820,
                      title_text=f"{freq_label}K线与技术指标（MA/成交量/MACD/RSI）",
                      xaxis_rangeslider_visible=False,
                      legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0),
                      margin=dict(l=0, r=0, t=46, b=0), hovermode="x unified")
    for r in (2, 3, 4):
        fig.update_yaxes(title_text="", row=r, col=1)
    fig.update_yaxes(title_text="价格", row=1, col=1)
    return fig


# ── DCF 敏感性热力图（Phase 2）─────────────────────────

def dcf_sensitivity_fig(val: dict | None):
    """WACC × 永续增长率 → 每股内在价值热力图。

    数据来自 valuation_analysis.dcf.sensitivity（compute_sensitivity 输出）。
    缺失返回 None。
    """
    import plotly.graph_objects as go

    dcf = (val or {}).get("dcf") or {}
    sens = dcf.get("sensitivity") or {}
    wacc_vals = sens.get("wacc_values") or []
    g_vals = sens.get("growth_values") or []
    matrix = sens.get("matrix") or []
    if not wacc_vals or not g_vals or not matrix:
        return None
    z = [[float(v) if v is not None else float("nan") for v in row] for row in matrix]
    text = [[f"{v:,.0f}" if v is not None else "—" for v in row] for row in matrix]

    fig = go.Figure(go.Heatmap(
        x=g_vals, y=wacc_vals, z=z, text=text, texttemplate="%{text}",
        colorscale="RdYlGn", zmid=0, colorbar=dict(title="每股价值"),
        hovertemplate="永续g=%{x}<br>WACC=%{y}<br>每股价值=%{text}<extra></extra>",
    ))
    base = sens.get("base_fair_value")
    base_w = sens.get("base_wacc")
    base_g = sens.get("base_terminal_growth")
    if base is not None and base_w is not None and base_g is not None:
        fig.add_trace(go.Scatter(
            x=[base_g], y=[base_w], mode="markers",
            marker=dict(size=11, color="black", symbol="diamond"),
            name=f"基准 {base:,.0f}", hovertemplate="基准点<extra></extra>"))
    fig.update_layout(
        height=420, margin=dict(l=0, r=0, t=30, b=0),
        xaxis_title="永续增长率 g", yaxis_title="WACC",
        title_text="DCF 敏感性（WACC × 永续增长率 → 每股内在价值）")
    return fig


# ── 杜邦分解（Phase 2）────────────────────────────────

def dupont_fig(fin: dict | None):
    """杜邦分解可视化：最近一年 log 瀑布（乘性拆解）+ 逐年 ROE 趋势。

    数据来自 financial_analysis.dupont_breakdown（净利率×周转率×乘数=ROE）。
    用自然对数把乘性分解转成加性瀑布贡献，终值还原为 ROE%。缺失返回 None。
    """
    import numpy as np
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    dup = (fin or {}).get("dupont_breakdown") or []
    if not dup:
        return None
    items = [d for d in dup if d.get("roe") is not None]
    if not items:
        return None
    latest = items[-1]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=False,
                        vertical_spacing=0.12, row_heights=[0.6, 0.4],
                        subplot_titles=(f"{latest['period']} ROE 拆解（对数贡献）",
                                        "ROE 逐年趋势"))

    # 瀑布：ln(净利率) + ln(周转率) + ln(乘数) → ln(ROE)
    comps = []
    for key, label in (("net_margin", "净利率"), ("asset_turnover", "资产周转"),
                       ("equity_multiplier", "权益乘数")):
        v = latest.get(key)
        comps.append((label, np.log(v) if v and v > 0 else np.nan))
    final_val = np.log(latest["roe"]) if latest["roe"] > 0 else np.nan

    x_labels = [c[0] for c in comps] + ["ROE"]
    y_vals = [c[1] for c in comps] + [final_val]
    log_vals = [c[1] for c in comps]
    y_pos = [0.0]
    for v in log_vals:
        y_pos.append(y_pos[-1] + (v if np.isfinite(v) else 0.0))
    # waterfall bars: 起点0→第一个因子，随后递增，终值=ROE对数
    bar_vals = [0.0]
    for i in range(len(comps)):
        bar_vals.append(log_vals[i])
    colors = ["#2563eb", "#d97706", "#8b5cf6"]
    fig.add_trace(go.Waterfall(
        x=x_labels, y=[bar_vals[1], bar_vals[2], bar_vals[3], final_val],
        measure=["relative", "relative", "relative", "total"],
        increasing=dict(marker_color="#16a34a"), decreasing=dict(marker_color="#dc2626"),
        text=[f"{c[1]:+.2f}" if np.isfinite(c[1]) else "—" for c in comps]
             + [f"{latest['roe']:.1%}"],
        textposition="outside", name="对数贡献",
        connector=dict(line=dict(color="rgba(0,0,0,0.2)")),
    ), row=1, col=1)

    # ROE 逐年趋势（子图2）
    roes = [(d["period"], d["roe"]) for d in items]
    fig.add_trace(go.Bar(x=[p for p, _ in roes],
                         y=[v * 100 for _, v in roes], name="ROE",
                         marker_color="#e74c3c"), row=2, col=1)

    fig.update_layout(height=520, showlegend=False,
                      margin=dict(l=0, r=0, t=46, b=0),
                      annotations=[dict(
                          text="对数贡献：ln(ROE)=ln(净利率)+ln(周转率)+ln(乘数)，"
                               "终值还原为 ROE%（乘性拆解的可视化等价）",
                          x=0.5, y=-0.08, xref="paper", yref="paper",
                          showarrow=False, font=dict(size=10, color="#666"))])
    fig.update_yaxes(title_text="ln(因子)", row=1, col=1)
    fig.update_yaxes(title_text="ROE %", row=2, col=1)
    return fig


# ── 回测绩效曲线（Phase 2）─────────────────────────────

def backtest_perf_fig(price_records: list[dict] | None,
                      benchmark_records: list[dict] | None = None,
                      trades: list[dict] | None = None):
    """回测绩效曲线：策略净值 vs 买入持有 vs 基准 + 回撤 + 滚动夏普。

    price_records: /history records（[{date,close,...}]，测试窗口内标的收盘）。
    benchmark_records: /benchmark records（同形状，缺则仅画标的线）。
    trades: 回测成交明细（[{entry_date,exit_date,entry_price,exit_price,pnl_pct}]）。
    策略净值 = 多笔持仓按入场价标记到市 + 平仓后计入已实现收益（诚实近似）。
    无数据返回 None。
    """
    import numpy as np
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    if not price_records or len(price_records) < 10:
        return None

    px = pd.Series([r.get("close") for r in price_records],
                   index=pd.to_datetime([r["date"] for r in price_records]),
                   dtype=float).dropna()

    # 策略净值：持仓期内按市价标记，持仓外平直线
    tr = sorted([t for t in (trades or []) if t.get("entry_date") and t.get("exit_date")],
                key=lambda t: t["entry_date"])
    equity = pd.Series(index=px.index, dtype=float)
    level = 100.0
    cursor = 0
    active = False
    for i, d in enumerate(px.index):
        # 处理到期出场：该日收盘后切换持仓
        while cursor < len(tr) and d >= pd.to_datetime(tr[cursor]["exit_date"]):
            t = tr[cursor]
            level = level * (1.0 + float(t.get("pnl_pct", 0.0)) / 100.0)
            active = False
            cursor += 1
        # 处理新进场：该日起建仓
        while cursor < len(tr) and d >= pd.to_datetime(tr[cursor]["entry_date"]):
            t = tr[cursor]
            active = True
            break
        if active and cursor < len(tr):
            t = tr[cursor]
            ep = float(t.get("entry_price") or px.iloc[0])
            if ep and ep > 0:
                equity.iloc[i] = level * (px.iloc[i] / ep)
            else:
                equity.iloc[i] = level
        else:
            equity.iloc[i] = level
    eq = equity.dropna()

    bh = px / px.iloc[0] * 100.0  # 买入持有
    bench = None
    if benchmark_records and len(benchmark_records) >= 10:
        bdf = pd.Series([r.get("close") for r in benchmark_records],
                        index=pd.to_datetime([r["date"] for r in benchmark_records]),
                        dtype=float).dropna()
        bdf = bdf.reindex(px.index).ffill()
        bench = bdf / bdf.dropna().iloc[0] * 100.0

    # 回撤（策略净值）
    roll_max = eq.cummax()
    drawdown = (eq / roll_max - 1.0) * 100.0
    # 滚动夏普（策略日收益，20 交易日）
    rets = eq.pct_change().dropna()
    roll_sharpe = (rets.rolling(20).mean() * 252.0) / \
        (rets.rolling(20).std(ddof=0) * np.sqrt(252.0)).replace(0, np.nan)

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                        row_heights=[0.55, 0.25, 0.20],
                        subplot_titles=("净值曲线", "策略回撤", "滚动夏普(20日)"))
    fig.add_trace(go.Scatter(x=bh.index, y=bh, name="买入持有(标的)",
                             line=dict(width=1.2, color="#2563eb")), row=1, col=1)
    fig.add_trace(go.Scatter(x=eq.index, y=eq, name="策略净值",
                             line=dict(width=2, color="#e74c3c")), row=1, col=1)
    if bench is not None:
        fig.add_trace(go.Scatter(x=bench.index, y=bench, name="基准指数",
                                 line=dict(width=1.2, color="#f59e0b", dash="dot")), row=1, col=1)
    # 买卖点标注（落在策略净值曲线上）
    for t in tr:
        ed, xd = pd.to_datetime(t["entry_date"]), pd.to_datetime(t["exit_date"])
        yb = eq.asof(ed) if not eq.empty else None
        ys = eq.asof(xd) if not eq.empty else None
        if yb is not None:
            fig.add_trace(go.Scatter(x=[ed], y=[yb], mode="markers",
                                     marker=dict(symbol="triangle-up", size=11,
                                                 color="#16a34a"),
                                     name="买", showlegend=False), row=1, col=1)
        if ys is not None:
            fig.add_trace(go.Scatter(x=[xd], y=[ys], mode="markers",
                                     marker=dict(symbol="triangle-down", size=11,
                                                 color="#dc2626"),
                                     name="卖", showlegend=False), row=1, col=1)
    # 回撤
    fig.add_trace(go.Scatter(x=drawdown.index, y=drawdown, name="回撤",
                             fill="tozeroy", line=dict(width=0.8, color="#dc2626"),
                             fillcolor="rgba(220,38,38,0.15)"), row=2, col=1)
    # 滚动夏普
    fig.add_trace(go.Scatter(x=roll_sharpe.index, y=roll_sharpe, name="滚动夏普",
                             line=dict(width=1, color="#8b5cf6")), row=3, col=1)
    fig.add_hline(y=0, line_dash="dot", line_color="#999", row=3, col=1)

    fig.update_layout(height=720, showlegend=True,
                      margin=dict(l=0, r=0, t=46, b=0), hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0))
    return fig
