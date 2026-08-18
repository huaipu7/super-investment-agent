"""🔬 回测验证 —— 以库方式调用 backtest_workflow，把无前瞻回测接入 SIA 看板。

三种模式：单次 / 批量（config.symbols）/ 滚动 walk-forward。
结果默认浏览 reports/ 已有 JSON；点「开始回测」才实时跑。
防泄漏红线与 CLI 完全一致（Dataset model_validator 强制 train ≤ cutoff < test）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import streamlit as st

BTW_ROOT = Path(os.environ.get("BACKTEST_WF_PATH",
                               r"D:\Desktop\ClaudeProjects\backtest_workflow"))
SIA_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SIA_ROOT))

MARKETS = ["us_stock", "cn_stock", "fund"]
DATA_SOURCES = ["synthetic", "csv", "yfinance", "akshare", "super_investment"]
PROJ_TYPES = ["mock", "super_investment", "super_investment_api"]


def _default_cfg() -> dict:
    """读取 backtest_workflow/config/default.yaml 作为表单默认值。"""
    try:
        import yaml
        with open(BTW_ROOT / "config" / "default.yaml", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _ensure_backtest() -> bool:
    if not BTW_ROOT.exists():
        st.error(f"未找到 backtest_workflow: {BTW_ROOT}\n"
                 "可用环境变量 BACKTEST_WF_PATH 覆盖。")
        return False
    if str(BTW_ROOT) not in sys.path:
        sys.path.insert(0, str(BTW_ROOT))
    return True


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _num(v: float | None) -> str:
    return "—" if v is None else f"{v:.4f}"


def _parse_symbol_row(row: str, fallback_market: str) -> dict:
    parts = [p.strip() for p in row.split(",")]
    mkt = parts[1] if len(parts) > 1 and parts[1] in MARKETS else fallback_market
    return {"symbol": parts[0], "market": mkt}


# ── 绩效曲线（Phase 2）──────────────────────────────────

def _render_perf_curve(r) -> None:
    """策略净值 vs 买入持有 vs 基准指数 + 回撤 + 滚动夏普。

    纯可视化增强：后端不可用 / 无行情时降级为提示，不影响回测结果本身。
    """
    try:
        import ui.viz_charts as vz
        from ui import api_client as api
    except Exception:
        st.caption("绩效曲线需 SIA 后端行情接口（当前不可用，跳过）。")
        return

    try:
        price_records = api.history(r.symbol, period="1y").get("records", [])
    except Exception:
        price_records = None
    try:
        bench_records = api.benchmark(r.symbol).get("records", [])
    except Exception:
        bench_records = None

    trades = []
    for t in r.trades:
        if not getattr(t, "entry_date", None) or not getattr(t, "exit_date", None):
            continue
        trades.append({
            "entry_date": str(t.entry_date), "exit_date": str(t.exit_date),
            "entry_price": t.entry_price, "exit_price": t.exit_price,
            "pnl_pct": t.pnl_pct,
        })

    fig = vz.backtest_perf_fig(price_records, bench_records, trades)
    if fig is not None:
        st.subheader("回测绩效曲线")
        st.plotly_chart(fig, use_container_width=True)
        st.caption("策略净值=持仓期内按入场价标记到市 + 平仓计入已实现收益；"
                   "基准指数按窗口起点归一为 100；回撤与滚动夏普基于策略净值。")
    elif price_records is None:
        st.caption("后端不可用，跳过绩效曲线（不影响回测结果）。")
    else:
        st.caption("测试窗口行情不足 10 根，跳过绩效曲线。")


# ── 结果渲染 ────────────────────────────────────────────────

def _render_single(r) -> None:
    c = st.columns([1, 1, 2])
    c[0].metric("信号", r.signal or "—")
    c[1].metric("置信度", f"{r.confidence:.2f}" if r.confidence is not None else "—")
    if r.reasoning:
        c[2].info(r.reasoning)

    m = r.metrics
    cols = st.columns(6)
    cols[0].metric("总收益", _pct(m.total_return_pct))
    cols[1].metric("年化收益", _pct(m.annualized_return_pct))
    cols[2].metric("最大回撤", _pct(m.max_drawdown_pct))
    cols[3].metric("夏普", _num(m.sharpe_ratio))
    cols[4].metric("胜率", _pct(m.win_rate))
    cols[5].metric("交易数", m.num_trades)

    # Phase 2：回测绩效曲线（策略净值 vs 买入持有 vs 基准 + 回撤 + 滚动夏普）
    _render_perf_curve(r)

    st.subheader("交易明细")
    rows = [{
        "入场日": t.entry_date,
        "入场价": t.entry_price if t.entry_price is not None else "—",
        "出场日": t.exit_date,
        "出场价": t.exit_price if t.exit_price is not None else "—",
        "持仓(交易日)": t.holding_days,
        "收益(%)": _pct(t.pnl_pct),
        "出场原因": t.exit_reason.value,
    } for t in r.trades]
    st.dataframe(rows, use_container_width=True)

    # Phase 3：Excel 导出（openpyxl 本地生成，数值原样）
    try:
        from ui import export as ui_export
        xlsx = ui_export.backtest_single_xlsx(r)
    except Exception:
        xlsx = None
    if xlsx:
        st.download_button(
            "导出 Excel", data=xlsx,
            file_name=f"backtest_{getattr(r, 'symbol', 'result')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"bt_xlsx_{getattr(r, 'symbol', 'result')}")

    st.subheader("Agent 流水")
    traces = [{
        "Agent": tr.agent, "耗时(s)": f"{tr.elapsed_s:.2f}", "ok": tr.ok,
        "输入摘要": tr.input_summary, "输出摘要": tr.output_summary,
    } for tr in r.traces]
    st.dataframe(traces, use_container_width=True)

    st.subheader("防泄漏检查")
    st.markdown(
        "- train 全部日期 ≤ cutoff < test：**通过**（Dataset model_validator 强制，越界抛 DataLeakageError）\n"
        "- ProjectionAgent 只接收 train：**接口保证**\n"
        "- StrategyExecutor 无行情输入：**接口保证**")
    for w in r.warnings:
        st.warning(w)
    if r.lookahead_risk:
        st.warning("⚠️ L3 对照模式：结果含 look-ahead 风险（当前实时数据），仅供对照")
    with st.expander("完整结果 JSON"):
        st.json(r.model_dump(mode="json"))


def _render_batch(br, wf: bool) -> None:
    rows = []
    for r in br.runs:
        row = {"期": f"p{r.period}", "标的": r.symbol} if not wf else {"期": f"p{r.period}"}
        if wf:
            row |= {"训练窗口": f"{r.train_start}~{r.train_end}", "cutoff": r.cutoff_date,
                    "测试窗口": f"{r.test_start}~{r.test_end}"}
        row |= {"市场": r.market.value, "信号": r.signal, "收益(%)": _pct(r.total_return_pct),
                "年化(%)": _pct(r.annualized_return_pct), "回撤(%)": _pct(r.max_drawdown_pct),
                "交易": r.num_trades, "出场": r.exit_reason}
        rows.append(row)
    st.dataframe(rows, use_container_width=True)

    if br.stats is not None:
        s = br.stats
        st.subheader("稳定性统计（walk-forward，有成交口径）")
        st.dataframe([
            {"指标": "训练窗口", "值": f"{s.window_days} 交易日"},
            {"指标": "滚动步长", "值": f"{s.step_days} 交易日"},
            {"指标": "期数", "值": s.n_periods},
            {"指标": "有成交期数", "值": s.n_trades},
            {"指标": "盈利/亏损期", "值": f"{s.wins} / {s.losses}"},
            {"指标": "胜率", "值": _pct(s.positive_rate)},
            {"指标": "平均收益", "值": _pct(s.avg_return_pct)},
            {"指标": "收益标准差", "值": _pct(s.std_return_pct)},
            {"指标": "最大/最小收益", "值": f"{_pct(s.max_return_pct)} / {_pct(s.min_return_pct)}"},
            {"指标": "信号分布", "值": json.dumps(s.signals, ensure_ascii=False)},
        ], use_container_width=True)

    for w in br.warnings:
        st.warning(w)

    with st.expander("逐期详情"):
        for r in br.runs:
            with st.expander(f"期 {r.period}: {r.symbol} @{r.cutoff_date}"):
                st.markdown(f"**信号**: {r.signal}（conf={r.confidence}）")
                if r.reasoning:
                    st.markdown(f"推理: {r.reasoning}")
                st.markdown(
                    f"收益 {_pct(r.total_return_pct)} · 年化 {_pct(r.annualized_return_pct)} · "
                    f"回撤 {_pct(r.max_drawdown_pct)} · 夏普 {_num(r.sharpe_ratio)} · 交易 {r.num_trades}")
                for w in r.warnings:
                    st.caption(f"⚠️ {w}")

    with st.expander("完整结果 JSON"):
        st.json(br.model_dump(mode="json"))

    # Phase 3：批量结果 Excel 导出
    try:
        from ui import export as ui_export
        bx = ui_export.backtest_batch_xlsx(br)
    except Exception:
        bx = None
    if bx:
        st.download_button(
            "导出汇总 Excel", data=bx,
            file_name=f"backtest_{'wf' if wf else 'batch'}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="bt_batch_xlsx")


# ── 执行 ────────────────────────────────────────────────────

def _run(cfg, mode, symbol, market, data_source, proj, start, cutoff, end,
         symbols_rows, window, step, project_path, api_base_url):
    if not _ensure_backtest():
        return None, mode
    cfg["symbol"] = str(symbol)
    cfg["market"] = market
    cfg["start_date"] = start.isoformat()
    cfg["cutoff_date"] = cutoff.isoformat()
    cfg["end_date"] = end.isoformat()
    cfg["data_source"] = data_source
    cfg.setdefault("projection_agent", {})["type"] = proj
    cfg.setdefault("super_investment", {})["project_path"] = project_path or str(SIA_ROOT)
    cfg.setdefault("super_investment", {})["api_base_url"] = api_base_url.strip() or None
    cfg.setdefault("report", {})["out_dir"] = "reports"

    try:
        with st.spinner("回测运行中…"):
            if mode == "批量":
                if not symbols_rows.strip():
                    st.error("批量模式请填写标的列表（每行 symbol,market）")
                    return None, "batch"
                from agents.batch_runner import run_batch
                cfg["symbols"] = [_parse_symbol_row(r, market)
                                  for r in symbols_rows.strip().splitlines() if r.strip()]
                return run_batch(cfg), "batch"
            if mode == "滚动 walk-forward":
                from agents.batch_runner import run_walk_forward
                if step > window:
                    st.error(f"滚动步长({step}) > 训练窗口({window})，请调整")
                    return None, "wf"
                return run_walk_forward(cfg, window=int(window), step=int(step)), "wf"
            from agents.orchestrator import Orchestrator
            return Orchestrator(cfg).run(), "single"
    except Exception as e:
        st.error(f"回测失败: {type(e).__name__}: {e}")
        return None, mode


# ── 历史报告浏览 ────────────────────────────────────────────

def _history_browser() -> None:
    reports = BTW_ROOT / "reports"
    with st.expander("📂 浏览历史报告（CLI / 本页产出）"):
        if not reports.exists():
            st.caption("reports/ 不存在，暂无历史。")
            return
        files = sorted(reports.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            st.caption("reports/ 为空。")
            return
        chosen = st.selectbox("选择报告", [p.name for p in files], key="bt_history")
        p = next(f for f in files if f.name == chosen)
        st.caption(f"{p} · {dt.datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M:%S}")
        fmt = st.radio("格式", ["📄 文字版 (Markdown)", "🗂 JSON"], horizontal=True,
                       key="bt_hist_fmt")
        if fmt.startswith("📄"):
            md = p.with_suffix(".md")
            if not md.exists():
                st.error(f"缺少文字版报告 {md.name}（每份回测同时产出 .json 与 .md）")
                return
            st.markdown(md.read_text(encoding="utf-8"))
        else:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                st.error(f"读取失败: {e}")
                return
            st.json(data)


# ── 页面入口 ────────────────────────────────────────────────

def page() -> None:
    st.header("🔬 回测验证")
    st.caption("投影能力无前瞻回测（DataFetch → Projection → Strategy → Validation），联动 backtest_workflow。"
               "默认 synthetic + mock，离线可复现。")
    if not _ensure_backtest():
        return

    cfg = _default_cfg()
    mode = st.radio("回测模式", ["单次", "批量", "滚动 walk-forward"],
                    horizontal=True, key="bt_mode_radio")

    with st.form("bt_form"):
        c1, c2 = st.columns(2)
        with c1:
            symbol = st.text_input("标的代码", value=str(cfg.get("symbol", "AAPL")))
            market = st.selectbox("市场", MARKETS,
                                  index=MARKETS.index(cfg.get("market", "us_stock")),
                                  key="bt_market")
            data_source = st.selectbox("数据源", DATA_SOURCES,
                                       index=DATA_SOURCES.index(cfg.get("data_source", "synthetic")),
                                       key="bt_ds")
            proj = st.selectbox("投影 Agent", PROJ_TYPES,
                                index=PROJ_TYPES.index(
                                    ((cfg.get("projection_agent") or {}).get("type", "mock"))),
                                key="bt_proj")
        with c2:
            start = st.date_input("起始日", dt.date.fromisoformat(str(cfg.get("start_date", "2020-01-01"))))
            cutoff = st.date_input("决策点 (cutoff)", dt.date.fromisoformat(str(cfg.get("cutoff_date", "2025-12-31"))))
            end = st.date_input("结束日", dt.date.fromisoformat(str(cfg.get("end_date", "2026-08-12"))))
            with st.expander("高级: SIA 联动"):
                si = cfg.get("super_investment") or {}
                project_path = st.text_input("SIA project_path",
                                             value=str(si.get("project_path") or SIA_ROOT))
                api_base_url = st.text_input("API base_url (L3 对照, 可选)",
                                             value=si.get("api_base_url") or "")

        symbols_rows = ""
        window, step = 252, 63
        if mode == "批量":
            existing = cfg.get("symbols") or []
            default_rows = "\n".join(
                f"{s['symbol']},{s['market']}" if isinstance(s, dict) else s
                for s in existing)
            symbols_rows = st.text_area("批量标的（每行 symbol,market）", value=default_rows,
                                        height=90,
                                        help="示例: AAPL,us_stock\n600519,cn_stock\n110022,fund")
        elif mode == "滚动 walk-forward":
            wf = cfg.get("walk_forward") or {}
            cw, cs = st.columns(2)
            window = cw.number_input("训练窗口(交易日)", min_value=20,
                                     value=int(wf.get("window") or 252), step=10)
            step = cs.number_input("滚动步长(交易日)", min_value=5,
                                   value=int(wf.get("step") or 63), step=5)

        submitted = st.form_submit_button("▶ 开始回测", type="primary")

    if submitted:
        result, rmode = _run(cfg, mode, symbol, market, data_source, proj,
                             start, cutoff, end, symbols_rows, window, step,
                             project_path, api_base_url)
        if result is not None:
            st.session_state["bt_result"] = result
            st.session_state["bt_mode"] = rmode

    if "bt_result" in st.session_state:
        res = st.session_state["bt_result"]
        st.caption(f"上次结果: {st.session_state['bt_mode']} · "
                   f"{res.generated_at:%Y-%m-%d %H:%M:%S}")
        if st.session_state["bt_mode"] == "single":
            _render_single(res)
        else:
            _render_batch(res, st.session_state["bt_mode"] == "wf")
    else:
        st.info("点击「开始回测」运行；或在下方浏览 reports/ 已有历史报告。")

    _history_browser()
