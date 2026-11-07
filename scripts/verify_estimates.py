"""离线验证：Phase 1 估算参数真实化 + 新指标 + 策略增强 + 持久化缓存。

覆盖：
- estimates.py：make_estimate 结构、compute_beta（合成数据确定性）、estimate_erp
  夹取、estimate_peer_pe 分级、get_risk_free_rate 返回结构（网络失败回退不抛异常）。
- valuation.analyze：estimates 字段 + peer_pe 覆盖 industry_avg。
- financial：dupont_breakdown / roic / valuation_context(pe_percentile) 结构。
- technical：obv / divergence。
- trade_plan：risk_reward_ratio / confidence_level / weekly_trend / weekly_resonance。
- supply_chain：estimates_used 列表；geopolitical：china_exposure_estimate。
- cache：set/get 往返（进程重启模拟）。

退出码: 0 = 全部通过；1 = 存在失败。无需后端即可运行。
"""
from __future__ import annotations

import sys
import traceback

if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("gb"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, ".")

import numpy as np
import pandas as pd

from app.analysis.estimates import (
    compute_beta, estimate_erp, estimate_peer_pe, get_risk_free_rate, make_estimate,
)
from app.analysis.financial import FinancialAnalyzer
from app.analysis.geopolitical import GeopoliticalAnalyzer
from app.analysis.supply_chain import SupplyChainAnalyzer
from app.analysis.technical import compute_indicators
from app.analysis.trade_plan import build_trade_plan
from app.analysis.valuation import DCFModel
from app.analysis.industry import IndustryClass

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


def _synthetic_ohlc(days: int = 480, downtrend: bool = False, seed: int = 1) -> pd.DataFrame:
    idx = pd.date_range("2023-01-01", periods=days, freq="B")
    base = np.linspace(100, 80 if downtrend else 130, days)
    noise = np.random.RandomState(seed).normal(0, 0.8, days)
    close = base + noise
    vol = np.random.RandomState(seed).rand(days) * 1e6 + 5e5
    return pd.DataFrame({
        "Open": close * 0.999, "High": close * 1.012, "Low": close * 0.988,
        "Close": close, "Volume": vol,
    }, index=idx)


def _test_estimates_core():
    print("== estimates 核心 ==")
    e = make_estimate(0.05, "test", "high", "note")
    _ok("make_estimate 结构", isinstance(e, dict) and e["value"] == 0.05
        and e["confidence"] in ("high", "medium", "low") and "source" in e)

    rf = get_risk_free_rate(ashare=False)
    _ok("Rf 返回结构", isinstance(rf, dict) and {"value", "source", "confidence"} <= set(rf)
        and (rf["value"] is None or 0.0 < rf["value"] < 0.15))
    rf_cn = get_risk_free_rate(ashare=True)
    _ok("Rf(中国) 返回结构", isinstance(rf_cn, dict) and "source" in rf_cn)

    # beta 合成数据确定性：股票日收益 = 0.8×基准 → beta ≈ 0.8
    idx = pd.date_range("2019-01-01", "2024-12-31", freq="B")
    mkt_ret = np.random.RandomState(42).normal(0.0005, 0.01, len(idx))
    mkt = 100 * np.exp(np.cumsum(mkt_ret))
    stock = 100 * np.exp(np.cumsum(0.8 * mkt_ret))
    b = compute_beta(pd.DataFrame({"Close": stock}, index=idx),
                     pd.DataFrame({"Close": mkt}, index=idx))
    _ok("beta 回归≈0.8", b.get("value") is not None and 0.7 < b["value"] < 0.9,
        f"beta={b.get('value')}")
    _ok("beta 高置信标注", b.get("confidence") == "high")
    b_none = compute_beta(None, None)
    _ok("beta 缺数据→None", b_none.get("value") is None and b_none.get("source"))

    erp = estimate_erp(pd.DataFrame({"Close": mkt}, index=idx), 0.03, fallback_erp=0.05)
    _ok("ERP 夹取到 [2%,9%]", erp.get("value") is not None and 0.02 <= erp["value"] <= 0.09,
        f"erp={erp.get('value')}")
    erp_short = estimate_erp(pd.DataFrame({"Close": mkt[:100]}, index=idx[:100]), 0.03, 0.05)
    _ok("ERP 样本不足→回退常量", erp_short.get("value") == 0.05 and "静态" in erp_short.get("source", ""))

    pe_hi = estimate_peer_pe([{"pe": 10}, {"pe": 20}, {"pe": 30}])
    _ok("peer PE n≥3 → 中位数20", pe_hi.get("value") == 20.0 and pe_hi.get("confidence") == "high")
    pe_lo = estimate_peer_pe([{"pe": 15.0}])
    _ok("peer PE n<3 → low 置信", pe_lo.get("value") == 15.0 and pe_lo.get("confidence") == "low")
    pe_no = estimate_peer_pe([{"pe": None}, {"name": "x"}])
    _ok("peer PE 无数据 → None", pe_no.get("value") is None)


def _test_financial_new():
    print("== financial 新指标 ==")
    fa = FinancialAnalyzer()
    aligned = pd.DataFrame({
        "NET_INCOME": [80.0, 100.0, 120.0], "REVENUE": [800.0, 1000.0, 1200.0],
        "TOTAL_ASSETS": [1800.0, 2000.0, 2200.0], "TOTAL_EQUITY": [900.0, 1000.0, 1100.0],
        "OPERATING_INCOME": [120.0, 150.0, 170.0], "CASH_AND_EQUIVALENTS": [150.0, 200.0, 250.0],
        "CURRENT_LIABILITIES": [260.0, 300.0, 320.0],
    }, index=[2022, 2023, 2024])

    dup = fa._compute_dupont(aligned, [2022, 2023, 2024])
    _ok("杜邦分解结构", isinstance(dup, list) and len(dup) == 3
        and {"net_margin", "asset_turnover", "equity_multiplier", "roe"} <= set(dup[0]))
    _ok("杜邦 ROE 一致", dup[-1]["roe"] is not None
        and abs(dup[-1]["roe"] - 120.0 / 1100.0) < 1e-9)

    roic = fa._compute_roic(aligned, [2022, 2023, 2024])
    _ok("ROIC 结构", isinstance(roic, list) and len(roic) == 3 and "roic" in roic[0])
    _ok("ROIC 正值合理", roic[-1]["roic"] is not None and roic[-1]["roic"] > 0)

    idx = pd.date_range("2021-06-01", "2024-12-31", freq="B")
    prices = np.linspace(50, 150, len(idx)) + np.random.RandomState(7).normal(0, 2, len(idx))
    price_data = pd.DataFrame({"Close": prices}, index=idx)
    vc = fa._compute_pe_percentile(aligned, [2022, 2023, 2024], price_data, {"shares_outstanding": 10000})
    _ok("PE 分位结构", isinstance(vc, dict) and vc.get("pe_percentile") is not None
        and 0.0 <= vc["pe_percentile"] <= 1.0 and vc.get("current_pe") and vc.get("hist_pe"))
    vc_no = fa._compute_pe_percentile(aligned, [2022, 2023, 2024], pd.DataFrame(), {})
    _ok("PE 分位缺行情→None", vc_no.get("pe_percentile") is None)


def _test_technical_new():
    print("== technical OBV / 量价背离 ==")
    df = _synthetic_ohlc(downtrend=True)
    ind = compute_indicators(df)
    _ok("OBV 存在", ind.get("obv") is not None)
    _ok("divergence 合法值", ind.get("divergence") in ("bearish", "bullish", "none"))
    # 顶背离：近 20 日价格净涨，但放量大跌使 OBV 窗口净降
    n = 26
    idx2 = pd.date_range("2024-01-01", periods=n, freq="B")
    close = [100.0 + i * 0.5 for i in range(n)]
    vol = [1e5] * n
    close[20] = close[19] - 3.0   # 放量下杀
    vol[20] = 1e7
    close[21] = close[20] + 1.0
    dfd = pd.DataFrame({
        "Open": close, "High": [c + 1.0 for c in close], "Low": [c - 1.0 for c in close],
        "Close": close, "Volume": vol,
    }, index=idx2)
    ind2 = compute_indicators(dfd)
    _ok("顶背离检测", ind2.get("divergence") == "bearish", f"divergence={ind2.get('divergence')}")


def _test_trade_plan_enhanced():
    print("== trade_plan 增强字段 ==")
    df = _synthetic_ohlc()
    val = {"dcf": {"fair_value": 150.0, "upside": 0.3},
           "relative_valuation": {"pe": {"company": 20.0, "industry_avg": 18.0, "premium": 0.11}}}
    fin = {"current_price": 100.0}
    tp = build_trade_plan(price_data=df, valuation_result=val,
                          swot_rating="看好", company_info={}, financial_result=fin)
    _ok("风险收益比字段", tp.get("risk_reward_ratio") is not None and tp.get("risk_reward_ratio") > 0,
        f"rr={tp.get('risk_reward_ratio')}")
    _ok("置信度枚举", tp.get("confidence_level") in ("高", "中", "低"))
    _ok("周线趋势合法", tp.get("weekly_trend") in ("多头", "空头", "粘合", None))
    _ok("周线共振字段", tp.get("weekly_resonance") in ("共振", "背离", "周线参考", None))
    _ok("估值上行时置信度中或高", tp.get("confidence_level") in ("高", "中"),
        f"confidence={tp.get('confidence_level')}")

    tp2 = build_trade_plan(price_data=df, valuation_result=val,
                           swot_rating="看好", company_info={}, financial_result=fin)
    _ok("策略确定性", tp.get("rating") == tp2.get("rating")
        and tp.get("risk_reward_ratio") == tp2.get("risk_reward_ratio"))

    tp_none = build_trade_plan(price_data=None, valuation_result=None,
                               swot_rating=None, company_info=None, financial_result=None)
    _ok("全空输入不崩", isinstance(tp_none, dict) and tp_none.get("available") is True)


def _test_valuation_estimates():
    print("== valuation estimates + peer_pe ==")
    ratios = {
        "FREE_CASH_FLOW": [{"period": "2023", "value": 100.0},
                           {"period": "2024", "value": 120.0}],
        "PE": [{"period": "2024", "value": 20.0}],
    }
    dm = DCFModel()
    res = dm.analyze(
        ratios=ratios, growth_rates={}, raw_metrics={},
        company_info={"shares_outstanding": 1000, "currency": "USD"},
        industry_cls=IndustryClass.TECH_HARDWARE,
        current_price=100.0,
        estimates={"risk_free_rate": {"value": 0.04, "source": "test", "confidence": "high"},
                   "beta": {"value": 1.2, "source": "回归", "confidence": "high"},
                   "erp": {"value": 0.05, "source": "指数", "confidence": "low"}},
    )
    est = res.get("estimates") or {}
    _ok("estimates 含 Rf/Beta/ERP", {"risk_free_rate", "beta", "erp"} <= set(est))
    _ok("estimates 含来源/置信度", all(isinstance(est[k], dict) and est[k].get("source")
        for k in ("risk_free_rate", "beta", "erp")))

    res2 = dm.analyze(
        ratios=ratios, growth_rates={}, raw_metrics={},
        company_info={"shares_outstanding": 1000, "currency": "USD"},
        industry_cls=IndustryClass.TECH_HARDWARE, current_price=100.0,
        peer_pe={"value": 18.0, "source": "竞品中位数", "confidence": "high"},
    )
    rel = res2.get("relative_valuation") or {}
    pe = rel.get("pe") or {}
    _ok("peer_pe 覆盖 industry_avg", pe.get("industry_avg") == 18.0
        and pe.get("industry_avg_source") == "竞品中位数")


def _test_compliance_estimates():
    print("== 供应链/关税 显式标注 ==")
    sc = SupplyChainAnalyzer().analyze(
        company_info={}, financial_analysis={}, geopolitical_analysis={},
        market_analysis={}, industry_cls=IndustryClass.TECH_HARDWARE)
    eu = sc.get("estimates_used") or []
    _ok("supply_chain.estimates_used 存在", isinstance(eu, list) and len(eu) >= 3)
    _ok("estimates_used 每项含 param/source", all(isinstance(x, dict) and "param" in x and "source" in x for x in eu))

    geo = GeopoliticalAnalyzer().analyze_tariff_sensitivity(
        financial_analysis={"ratios": {"GROSS_MARGIN": [{"period": "2024", "value": 0.4}]}},
        company_info={"country": "US", "industry": "semiconductor",
                      "name": "TestCo", "summary": "china supply chain chips"},
        market_analysis={},
    )
    ce = geo.get("china_exposure_estimate") if isinstance(geo, dict) else None
    _ok("tariff.china_exposure_estimate 存在", isinstance(ce, dict)
        and ce.get("source") and ce.get("confidence") in ("high", "medium", "low"))


def _test_cache():
    print("== 分析缓存持久化 ==")
    from app.analysis.cache import get_cached_result, set_cached_result
    ticker = "__VERIFY_CACHE__"
    result = {"ticker": ticker, "pipeline_time": 123.0, "trade_plan": {"rating": "看多"}}
    ok = set_cached_result(ticker, result)
    got = get_cached_result(ticker)
    _ok("缓存写入+读取往返", ok and got is not None and got.get("pipeline_time") == 123.0)
    _ok("缓存命中标记", isinstance(got, dict) and (got.get("cache") or {}).get("hit") is True)
    _ok("缓存字段完整", got and got.get("trade_plan", {}).get("rating") == "看多")


def _test_risk_free_chain():
    """离线验证无风险利率的三级链路（不打网络，全部用假实现）。"""
    print("== 无风险利率链路 ==")
    from app.analysis import estimates as E

    real_t, real_a = E._us_10y_treasury, E._us_10y_akshare
    try:
        E._us_10y_treasury = lambda: (0.0484, "US Treasury 每日收益率曲线 10 Yr（官方）", "2026-11-05")
        E._us_10y_akshare = lambda: (0.0521, "akshare bond_zh_us_rate 美国国债收益率10年", "2026-11-05")
        us = E.get_risk_free_rate(market="us")
        _ok("美股 Rf 走财政部官方曲线",
            us["value"] == 0.0484 and "Treasury" in us["source"] and us["confidence"] == "high",
            str(us.get("value")))
        _ok("美股 Rf 带观测日期", "2026-11-05" in (us.get("note") or ""), str(us.get("note")))

        def _boom():
            raise RuntimeError("treasury down")

        E._us_10y_treasury = _boom
        hk = E.get_risk_free_rate(market="hk")
        _ok("财政部不可达时回落 akshare 美国10Y",
            hk["value"] == 0.0521 and "akshare" in hk["source"], str(hk.get("source")))
        _ok("港股用美元利率写明是近似",
            "联系汇率" in (hk.get("note") or "") and hk["confidence"] == "medium",
            str(hk.get("note")))

        E._us_10y_akshare = _boom
        dead = E.get_risk_free_rate(market="us")
        _ok("两条通路都挂时回退常量并标 low",
            dead["value"] == 0.025 and dead["confidence"] == "low", str(dead.get("value")))
        _ok("回退时 source 写明失败原因",
            "免费源不可用" in dead["source"] and "treasury down" in dead["source"],
            dead["source"][:60])
    finally:
        E._us_10y_treasury, E._us_10y_akshare = real_t, real_a

    # 范围校验在取数函数内部，假掉上层的 fetch 就测不到它——这里假的是数据源本身
    import app.data.sources.treasury_source as TS

    class _BadTS:
        def fetch_yield_curve(self, months=3, as_of=None):
            return pd.DataFrame({"10 Yr": [50.0]}, index=pd.to_datetime(["2026-11-05"]))

    orig_ts = TS.TreasurySource
    TS.TreasurySource = _BadTS
    try:
        try:
            E._us_10y_treasury()
            rejected = False
        except ValueError as exc:
            rejected = "超出合理范围" in str(exc)
        _ok("越界利率(50%)被范围校验拒绝", rejected)
    finally:
        TS.TreasurySource = orig_ts

    # A 股曾把 RangeIndex 行号当日期，写出「数据日期 7117」
    import akshare as ak

    orig_bond = ak.bond_zh_us_rate
    ak.bond_zh_us_rate = lambda **kw: pd.DataFrame({
        "日期": ["2026-11-04", "2026-11-05"],
        "中国国债收益率10年": [1.6077, 1.6038],
        "美国国债收益率10年": [4.90, 4.84],
    })
    try:
        rate, _, obs = E._cn_10y_akshare()
        _ok("A股 Rf 观测日期取日期列而非行号",
            obs == "2026-11-05" and abs(rate - 0.016038) < 1e-9, f"{obs} / {rate}")
    finally:
        ak.bond_zh_us_rate = orig_bond


def main():
    print("=" * 60)
    print("verify_estimates.py — Phase 1 估算真实化 + 新指标 + 策略增强 + 缓存")
    print("=" * 60)
    _test_estimates_core()
    print()
    _test_risk_free_chain()
    print()
    _test_financial_new()
    print()
    _test_technical_new()
    print()
    _test_trade_plan_enhanced()
    print()
    _test_valuation_estimates()
    print()
    _test_compliance_estimates()
    print()
    _test_cache()
    print()
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
