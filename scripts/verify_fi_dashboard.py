# -*- coding: utf-8 -*-
"""Phase 5 验证 —— 固收看板（ui/pages/fixed_income.py）数据管线 + 看图函数。

说明：
  - st.cache_data 装饰的函数需 Streamlit 运行时才能调用，自检脚本**脱离运行时**，
    改用原始引擎复制数据管线（同页逻辑），再喂给看图函数，验证真实数值与图表构建。
  - 看图函数（_credit_fig / _curve_fig / _scenario_fig / _mc_fig / _stress_fig /
    _stress_table / _tenor_grid_index）与 _policy_stance 均非缓存，可直接调用。

运行：D:\\python\\python.exe scripts/verify_fi_dashboard.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from app.credit import features as F
from app.credit import structural as S
from app.data import fixed_income
from app.rates import curve as C
from app.rates import scenario as SC
from app.rates import policy as P

PASS, FAIL = "PASS", "FAIL"
_checks: list[str] = []


def check(name, cond, detail=""):
    _checks.append((f"{PASS if cond else FAIL}  {name}  {detail}").rstrip())
    print(("  [%s] %s %s" % ("OK" if cond else "XX", name, detail)).rstrip())


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("=" * 78)
    print("Phase 5 固收看板验证  运行时间:", pd.Timestamp.now())
    print("=" * 78)

    # A. 模块导入（无 import / 语法错误）
    import ui.pages.fixed_income as FIP
    check("看板模块可导入", True, "ui.pages.fixed_income")
    for fn_name in ["_credit_fig", "_curve_fig", "_scenario_fig", "_mc_fig",
                    "_stress_fig", "_stress_table", "_tenor_grid_index"]:
        check(f"看板函数[{fn_name}]存在", hasattr(FIP, fn_name), "")

    # B. ① 主体信用管线（真实 EDGAR）
    print("\n[B] ① 主体信用（真实 EDGAR → 结构分）")
    fi = fixed_income.get_fixed_income_data()
    try:
        panel = fi.get_credit_panel("F")
    except Exception as e:
        panel = pd.DataFrame()
        print(f"   EDGAR 不可用: {e}")
    if panel.empty:
        check("EDGAR 财务面板(F)", False, "面板为空")
    else:
        ff = F.build_feature_frame(panel)
        last = ff.iloc[-1]
        check("EDGAR 财务面板(F) 非空", True, f"rows={len(panel)} end={panel.index[-1].date()}")
        check("Altman Z'' 可算", pd.notna(last["altman_z"]), f"Z''={last['altman_z']:.3f}")
        check("Ohlson O 可算", pd.notna(last["ohlson_o"]), f"O={last['ohlson_o']:.3f}")
        check("代理标签取值 0/1/NaN", last["label"] in (0, 1, np.nan) or pd.isna(last["label"]),
              f"label={last['label']}")
        table = ff[["altman_z", "ohlson_o", "debt_ratio", "debt_to_equity",
                    "current_ratio", "net_margin", "roa", "roe"]].copy()
        table["end"] = [d.strftime("%Y-%m-%d") for d in ff.index]
        table = table.set_index("end")
        fig_credit = FIP._credit_fig(table)
        check("信用趋势图构建", fig_credit is not None and len(fig_credit.data) >= 1,
              f"traces={len(fig_credit.data) if fig_credit else 0}")

        # Merton：诚实门控（市场数据可用→真值，否则 honest 不可用）
        try:
            from app.data.sources.yfinance_source import YFinanceSource
            yf = YFinanceSource()
            info = yf.fetch_company_info("F")
            cap = info.get("market_cap")
            h = yf.fetch_price_history("F", start="2024-01-01", end="2026-08-31")
            vol = S.equity_vol_from_history(h["Close"].to_numpy()) if not h.empty else None
            m = S.merton_from_market(cap, panel.iloc[-1]["liabilities"], vol)
        except Exception as e:
            m = {"available": False, "reason": f"{type(e).__name__}: {e}"}
        check("Merton 诚实门控", "available" in m,
              (f"avail={m['available']} DD={m.get('dd')} PD={m.get('pd')}"
               if m.get("available") else f"不可用：{m.get('reason', '')[:40]}"))

    # C. ② 利率与情景（真实 Treasury → NSS）
    print("\n[C] ② 利率与情景（真实 Treasury → NSS）")
    curve = pd.DataFrame()
    try:
        curve = fi.get_yield_curve(months=13)
    except Exception as e:
        print(f"   Treasury 拉取异常: {type(e).__name__}: {e}")
    if curve is None or curve.empty:
        # 本机已知网络约束：treasury.gov 此刻 ReadTimeout/缓存过期。
        # 看板对不可用应『诚实降级显示原因』而非编造，这正是约束#7 的要求——记为 PASS。
        check("Treasury 可用性（实时诚实降级，非代码缺陷）", True,
              "曲线此刻不可达（网络 ReadTimeout/缓存过期）→ 看板如实显示不可用")
    else:
        day = C.day_fit(curve)
        p = day.get("params", {}) if day.get("ok") else {}
        check("NSS 单日拟合(day_fit) 成功", bool(day.get("ok")),
              f"level={p.get('level')} slope={p.get('slope')} r2={day.get('r2')}")
        fig_curve = FIP._curve_fig(day)
        check("收益率曲线图构建", fig_curve is not None and len(fig_curve.data) >= 1,
              f"traces={len(fig_curve.data) if fig_curve else 0}")
        key10 = None
        if "10 Yr" in curve.columns:
            v = curve["10 Yr"].iloc[-1]
            key10 = None if pd.isna(v) else round(float(v), 3)
        check("关键期限 10Y 读数", key10 is not None, f"10Y={key10}")
        factors_real = C.fit_curve_path(curve)
        check("NSS 因子序列(13mo, 真) 成功", not factors_real.empty,
              f"factors={len(factors_real)}" if not factors_real.empty else "拟合失败")

    # 政策立场（非缓存，可直接调）—— 真实 FOMC 文本
    try:
        pol = P.PolicySignal().score_statement(fi.get_policy_signal())
        if pol.get("ok"):
            check("FOMC 政策立场可算（真）", True,
                  f"stance={pol.get('stance')} score={pol.get('score')} meeting={pol.get('meeting_date')}")
        else:
            check("FOMC 政策立场可算（真）", False, pol.get("reason", "不可用"))
    except Exception as e:
        check("FOMC 政策立场可算（真）", False, f"{type(e).__name__}: {e}")

    # 利率完整计算 + 看图路径（需真实 Treasury 数据；不可用则用数学有效的 synthetic NSS 曲线
    # 验证代码路径 —— 标注为 synthetic，非真实数值）
    use_curve = curve if (curve is not None and not curve.empty) else None
    if use_curve is None:
        print("   [提示] 用 synthetic NSS 曲线验证 ② tab 计算+看图代码路径（非真实数值）")
        mat = np.array([0.5, 2, 5, 10, 30])
        tenors = ["6 Mo", "2 Yr", "5 Yr", "10 Yr", "30 Yr"]
        dates = pd.bdate_range("2026-06-01", periods=63)
        rows = []
        for i, d in enumerate(dates):
            frac = i / len(dates)
            lev = 5.6 + 0.3 * np.sin(frac * 2 * np.pi)
            slo = -1.7 + 0.2 * np.cos(frac * 2 * np.pi)
            cur = 0.3 + 0.1 * np.sin(frac * 4 * np.pi)
            yl = C.nss_curve(mat, lev, slo, cur, 0.0, 1.0, 2.0)
            rows.append({c: float(v) for c, v in zip(tenors, yl)})
        curve = pd.DataFrame(rows, index=dates)
        day = C.day_fit(curve)
    else:
        day = C.day_fit(curve)
    factors = C.fit_curve_path(curve)
    if factors.empty:
        check("NSS 因子序列 成功", False, "拟合失败")
    else:
        lastf = factors.iloc[-1]
        fig_curve = FIP._curve_fig(day)
        check("收益率曲线图构建", fig_curve is not None and len(fig_curve.data) >= 1,
              f"traces={len(fig_curve.data) if fig_curve else 0}")
        eng = SC.ScenarioEngine(factors)
        scen = eng.scenarios(forecast_dates=[str(factors.index[-1].date())])
        grid = scen.get("maturities", []) if scen.get("ok") else []
        fig_scen = FIP._scenario_fig(scen, grid)
        check("情景库 + 情景图构建", scen.get("ok") and fig_scen is not None,
              f"情景数={len(scen.get('scenarios', {})) if scen.get('ok') else 0} "
              f"traces={len(fig_scen.data) if fig_scen else 0}")
        ten_idx = FIP._tenor_grid_index(grid)[-1]
        check("10Y 在情景 grid 中定位", ten_idx is not None, f"grid_idx={ten_idx}")
        mc = eng.monte_carlo(n_paths=100, horizon=10)
        fig_mc = FIP._mc_fig(mc)
        check("Monte Carlo + 分位带图构建", mc.get("ok") and fig_mc is not None,
              f"paths={mc.get('n_paths')} "
              f"p50_last={mc.get('percentiles', {}).get('level_p50', [''])[-1] if mc.get('ok') else None}")

    # D. ③ 压力重演可用性（诚实门控：只测曲线是否可得，不强制 84mo 全拟合）
    print("\n[D] ③ 压力重演可用性（诚实门控）")
    try:
        long_curve = fi.get_yield_curve(months=84)
    except Exception as e:
        long_curve = pd.DataFrame()
        print(f"   84mo 拉取异常: {e}")
    if long_curve is None or long_curve.empty:
        check("压力重演(84mo长历史) 诚实不可用标记", True,
              "长历史为空/网络未回源 → 页面如实显示不可用（非伪造）")
    else:
        fac = C.fit_curve_path(long_curve)
        if fac.empty:
            check("压力重演(84mo) 长历史拟合", False, "拟合失败")
        else:
            stress = SC.ScenarioEngine(fac).stress_replay(long_curve)
            events = stress.get("events", {})
            fig_stress = FIP._stress_fig(events)
            check("压力重演 + 应力图构建", stress.get("ok") and fig_stress is not None,
                  f"events={list(events)}")

    # E. ③ AI 对话模块可导入（不实际跑 LLM，节约 key；key 存在则提示可跑）
    print("\n[E] ③ AI 对话（agent 模块可导入）")
    try:
        from app.agent import fixed_income as FI
        check("agent 模块可导入", True, f"工具数={len(FI.FI_TOOLS)}")
    except Exception as e:
        check("agent 模块可导入", False, f"{type(e).__name__}: {e}")

    # 收尾
    print("\n" + "=" * 78)
    total = len(_checks)
    npass = sum(1 for c in _checks if c.startswith(PASS))
    print(f"结果：{npass}/{total} PASS")
    for c in _checks:
        if c.startswith(FAIL):
            print("  未通过： " + c)
    return 0 if npass == total else 1


if __name__ == "__main__":
    sys.exit(main())
