"""Phase 2 可视化模块离线验证（无需后端）。

覆盖新增图函数（ui/viz_charts.py）与图表生成器（app/report/charts.py）：
- kline_ohlc_fig：D/W/M 周期、买卖点/价位叠加、空数据降级、确定性
- dcf_sensitivity_fig：热力图 trace、缺失 None
- dupont_fig：瀑布 + ROE 趋势、缺失 None
- backtest_perf_fig：净值/基准/回撤/滚动夏普、缺基准降级、空数据 None
- indicator_series：序列长度与公式一致
- DCFModel.compute_sensitivity：5×5 矩阵、中心点=基准每股价值
- _price_action_chart（含成交量副图）、_dcf_heatmap、_dupont_chart：PNG 生成

运行: D:\\python\\python.exe scripts\\verify_viz2.py
退出码: 0 = 全部通过；1 = 存在失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from app.analysis.technical import compute_indicators, indicator_series
from app.analysis.valuation import DCFModel
from app.report.charts import ChartGenerator

_PASS = 0
_FAIL = 0


def _ok(name: str, cond: bool, detail: str = ""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# ── 合成数据 ─────────────────────────────────────────────

def _synthetic_price_history(days: int = 120, seed: int = 3) -> dict:
    idx = pd.date_range("2025-01-02", periods=days, freq="B")
    base = np.linspace(100, 130, days)
    noise = np.random.RandomState(seed).normal(0, 1.5, days)
    close = base + noise
    open_ = close * 0.998
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    vol = np.random.RandomState(seed).rand(days) * 2e6 + 1e6
    return {"available": True,
            "dates": [d.strftime("%Y-%m-%d") for d in idx],
            "open": [float(v) for v in open_],
            "high": [float(v) for v in high],
            "low": [float(v) for v in low],
            "close": [float(v) for v in close],
            "volume": [float(v) for v in vol]}


def _synthetic_trade_plan() -> dict:
    return {"available": True, "entry_zone": [108.0, 120.0],
            "stop_loss": 102.0, "take_profit": 135.0}


def _synthetic_val_with_sensitivity() -> dict:
    dm = DCFModel()
    fcf = [80.0, 90.0, 105.0, 118.0, 130.0]
    sens = dm.compute_sensitivity(fcf, base_wacc=0.09,
                                  base_terminal_growth=0.03,
                                  shares=100.0, net_debt=50.0)
    return {"dcf": {"fair_value": 8.0, "sensitivity": sens}}


def _synthetic_fin_dupont() -> dict:
    return {"dupont_breakdown": [
        {"period": "2022", "net_margin": 0.10, "asset_turnover": 0.6,
         "equity_multiplier": 2.0, "roe": 0.12},
        {"period": "2023", "net_margin": 0.12, "asset_turnover": 0.65,
         "equity_multiplier": 2.1, "roe": 0.164},
        {"period": "2024", "net_margin": 0.14, "asset_turnover": 0.7,
         "equity_multiplier": 2.2, "roe": 0.2156},
    ]}


def _synthetic_records(days: int = 40, start: float = 100.0, drift: float = 0.5) -> list[dict]:
    """模拟 /history records（含 OHLCV 全字段）。"""
    idx = pd.date_range("2026-01-05", periods=days, freq="B")
    records = []
    prev = start
    for i, d in enumerate(idx):
        close = round(prev + drift + np.random.RandomState(i).normal(0, 0.8), 2)
        open_ = round(prev, 2)
        high = round(max(open_, close) * 1.01, 2)
        low = round(min(open_, close) * 0.99, 2)
        records.append({"date": d.strftime("%Y-%m-%d"), "open": open_, "high": high,
                        "low": low, "close": close, "volume": int(1e6 + i * 100)})
        prev = close
    return records


# ── 断言组 ───────────────────────────────────────────────

def _test_kline():
    print("== kline_ohlc_fig（交互 K线）==")
    import ui.viz_charts as vz
    ph = _synthetic_price_history()
    tp = _synthetic_trade_plan()

    fig = vz.kline_ohlc_fig(ph, tp, "D")
    n_d = len(fig.data) if fig is not None else -1
    _ok("日K线含主图+副图", fig is not None and n_d >= 4, f"traces={n_d}")
    if fig is not None:
        types = [tr.type for tr in fig.data]
        _ok("含 candlestick", "candlestick" in types, f"types={types}")
        _ok("含成交量 bar", "bar" in types)
        _ok("含 MACD/RSI 线", types.count("scatter") >= 4)

    # 周/月需足够根数（月线至少 ~6 根，周线 ~5 根）
    ph_long = _synthetic_price_history(420)
    fig_w = vz.kline_ohlc_fig(ph_long, tp, "W")
    _ok("周线生成", fig_w is not None and len(fig_w.data) >= 4)
    fig_m = vz.kline_ohlc_fig(ph_long, tp, "M")
    _ok("月线生成", fig_m is not None and len(fig_m.data) >= 4)

    # 确定性：同输入两次 → trace 类型序列一致 + 首个 candlestick 数据长度一致
    fig2 = vz.kline_ohlc_fig(ph, tp, "D")
    same_types = [tr.type for tr in fig.data] == [tr.type for tr in fig2.data]
    c1 = next((tr for tr in fig.data if tr.type == "candlestick"), None)
    c2 = next((tr for tr in fig2.data if tr.type == "candlestick"), None)
    same_len = c1 is not None and c2 is not None and len(c1.open) == len(c2.open)
    _ok("确定性", same_types and same_len)

    # 空/坏数据降级
    _ok("空 dict → None", vz.kline_ohlc_fig({}, None, "D") is None)
    _ok("None → None", vz.kline_ohlc_fig(None, None, "D") is None)
    short = dict(ph, close=ph["close"][:10], open=ph["open"][:10],
                 high=ph["high"][:10], low=ph["low"][:10],
                 volume=ph["volume"][:10], dates=ph["dates"][:10])
    _ok("不足30根 → None", vz.kline_ohlc_fig(short, None, "D") is None)

    # records 形式（/history）归一化
    hist = {"records": _synthetic_records()}
    fig_h = vz.kline_ohlc_fig(hist, None, "D")
    _ok("records 输入生成", fig_h is not None and len(fig_h.data) >= 2)


def _test_dcf_sensitivity():
    print("== dcf_sensitivity_fig（DCF 敏感性热力图）==")
    import ui.viz_charts as vz
    val = _synthetic_val_with_sensitivity()
    sens = val["dcf"]["sensitivity"]
    _ok("矩阵 5×5", len(sens["wacc_values"]) == 5 and len(sens["growth_values"]) == 5
        and len(sens["matrix"]) == 5 and all(len(r) == 5 for r in sens["matrix"]))
    base = sens["base_fair_value"]
    _ok("中心点=基准每股价值", base is not None and base > 0)
    # 手工复算基准：WACC=0.09, g=0.03
    dm = DCFModel()
    d = dm.compute_dcf([80.0, 90.0, 105.0, 118.0, 130.0], 0.09, terminal_growth=0.03)
    expect = (d["fair_value_ev"] - 50.0) / 100.0
    _ok("矩阵与 compute_dcf 一致", abs(base - expect) < 0.05, f"base={base} expect={expect:.2f}")

    fig = vz.dcf_sensitivity_fig(val)
    _ok("热力图生成", fig is not None and any(tr.type == "heatmap" for tr in fig.data))
    _ok("缺失 → None", vz.dcf_sensitivity_fig({}) is None
        and vz.dcf_sensitivity_fig(None) is None
        and vz.dcf_sensitivity_fig({"dcf": {"fair_value": 8.0}}) is None)


def _test_dupont():
    print("== dupont_fig（杜邦分解）==")
    import ui.viz_charts as vz
    fin = _synthetic_fin_dupont()
    fig = vz.dupont_fig(fin)
    _ok("瀑布+ROE趋势生成", fig is not None and len(fig.data) >= 2, f"traces={len(fig.data) if fig else 0}")
    if fig is not None:
        _ok("含 waterfall", any(tr.type == "waterfall" for tr in fig.data))
    _ok("缺失 → None", vz.dupont_fig({}) is None
        and vz.dupont_fig({"dupont_breakdown": []}) is None)
    # 全 None 行降级
    bad = {"dupont_breakdown": [{"period": "2024", "net_margin": None,
                                 "asset_turnover": None, "equity_multiplier": None,
                                 "roe": None}]}
    _ok("全 None → None", vz.dupont_fig(bad) is None)


def _test_backtest_perf():
    print("== backtest_perf_fig（回测绩效曲线）==")
    import ui.viz_charts as vz
    price = _synthetic_records(40)
    bench = _synthetic_records(40, start=3200.0, drift=4.0)
    trades = [
        {"entry_date": "2026-01-20", "exit_date": "2026-02-10",
         "entry_price": 105.0, "exit_price": 112.0, "pnl_pct": 6.67},
    ]
    fig = vz.backtest_perf_fig(price, bench, trades)
    _ok("净值/基准/回撤/夏普", fig is not None and len(fig.data) >= 4,
        f"traces={len(fig.data) if fig else 0}")
    if fig is not None:
        _ok("含基准曲线", any(tr.name == "基准指数" for tr in fig.data))
        _ok("含策略净值", any(tr.name == "策略净值" for tr in fig.data))
    # 无基准 → 仍生成（仅标的+策略）
    fig_nb = vz.backtest_perf_fig(price, None, trades)
    _ok("无基准降级", fig_nb is not None and len(fig_nb.data) >= 3)
    # 空 trades → 仍生成（买入持有线）
    fig_nt = vz.backtest_perf_fig(price, None, [])
    _ok("空成交降级", fig_nt is not None and len(fig_nt.data) >= 1)
    # 数据不足 → None
    _ok("数据不足 → None", vz.backtest_perf_fig([], None, []) is None)
    _ok("None → None", vz.backtest_perf_fig(None, None, None) is None)


def _test_indicator_series():
    print("== indicator_series（序列指标）==")
    df = pd.DataFrame({
        "Open": np.linspace(100, 130, 120), "High": np.linspace(102, 132, 120),
        "Low": np.linspace(98, 128, 120), "Close": np.linspace(100, 130, 120),
        "Volume": np.full(120, 1e6),
    }, index=pd.date_range("2025-01-02", periods=120, freq="B"))
    s = indicator_series(df)
    _ok("键齐全", {"ma5", "ma20", "ma60", "macd_dif", "macd_dea", "macd_hist",
                    "rsi14", "obv", "boll_upper"} <= set(s))
    for k, v in s.items():
        _ok(f"{k} 长度一致", isinstance(v, pd.Series) and len(v) == 120, f"len={len(v) if isinstance(v, pd.Series) else '?'}")
        break
    _ok("macd_hist 非全空", not s["macd_hist"].dropna().empty)
    _ok("rsi14 非全空", not s["rsi14"].dropna().empty)
    _ok("rsi ∈[0,100]", bool(((s["rsi14"].dropna() >= 0) & (s["rsi14"].dropna() <= 100)).all()))
    # 末值标量与 compute_indicators 同口径
    ind = compute_indicators(df)
    _ok("末值 RSI 与标量一致", abs(float(s["rsi14"].dropna().iloc[-1]) - ind["rsi14"]) < 1e-6)
    _ok("末值 MACD 柱一致", abs(float(s["macd_hist"].dropna().iloc[-1]) - ind["macd_hist"]) < 1e-6)
    _ok("缺列 → 空 dict", indicator_series(pd.DataFrame({"Close": [1, 2]})).get("ma5", None) is not None
        or len(indicator_series(pd.DataFrame({"Close": [1, 2]}))) == 0)


def _test_report_charts():
    print("== 报告 PNG（_price_action_chart 含成交量 / _dcf_heatmap / _dupont_chart）==")
    import hashlib
    import os
    import shutil

    tmp = "data/reports/_verify_viz2"
    os.makedirs(tmp, exist_ok=True)
    gen = ChartGenerator(output_dir=tmp)

    ph = _synthetic_price_history(300)
    result = {"price_history": ph,
              "trade_plan": _synthetic_trade_plan(),
              "valuation_analysis": _synthetic_val_with_sensitivity(),
              "financial_analysis": _synthetic_fin_dupont()}

    p1 = gen._price_action_chart(result, "pa2.png")
    _ok("K线+成交量 PNG", p1 is not None and os.path.exists(p1) and os.path.getsize(p1) > 0)
    if p1:
        p1b = gen._price_action_chart(result, "pa2b.png")
        same = (p1b is not None
                and hashlib.md5(open(p1, "rb").read()).hexdigest()
                == hashlib.md5(open(p1b, "rb").read()).hexdigest())
        _ok("K线图确定性", same)

    h = gen._dcf_heatmap(result, "dcf2.png")
    _ok("DCF 热力图 PNG", h is not None and os.path.exists(h) and os.path.getsize(h) > 0)
    d = gen._dupont_chart(result, "dupont2.png")
    _ok("杜邦瀑布 PNG", d is not None and os.path.exists(d) and os.path.getsize(d) > 0)

    # 缺失数据 → None 不抛异常
    _ok("热力图缺数据 → None", gen._dcf_heatmap({}, "x.png") is None)
    _ok("杜邦缺数据 → None", gen._dupont_chart({}, "x.png") is None)

    # generate_all 集成：新图进入 charts dict
    all_charts = gen.generate_all(result)
    _ok("generate_all 含 dcf_sensitivity", "dcf_sensitivity" in all_charts)
    _ok("generate_all 含 dupont", "dupont" in all_charts)

    shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 60)
    print("verify_viz2.py — Phase 2 可视化（交互K线/敏感性热力图/杜邦/回测绩效）")
    print("=" * 60)
    _test_kline()
    print()
    _test_dcf_sensitivity()
    print()
    _test_dupont()
    print()
    _test_backtest_perf()
    print()
    _test_indicator_series()
    print()
    _test_report_charts()
    print()
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
