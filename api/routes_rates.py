# -*- coding: utf-8 -*-
"""固收利率 API —— /api/v1/rates/*（曲线、预测、情景、政策、压力重演、Monte Carlo）。

复用 app/agent/fixed_income 的工具实现（NSS/VAR/情景/政策 → 真实数值），
另加一个直连 ScenarioEngine.monte_carlo 的端点（无现成工具，需因子序列）。
可观测：打点 fi_rates_* / fi_mc_* 指标。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Query

from app.observability.metrics import metrics
from app.agent import fixed_income as _fi

router = APIRouter(prefix="/api/v1/rates", tags=["fixed-income-rates"])


def _run(name: str, args: dict, metric: str) -> dict:
    import time
    t0 = time.perf_counter()
    try:
        out = json.loads(_fi.tool_executor(name, args))
        if not out.get("ok", True):
            metrics.inc(f"fi_{metric}_errors", 1)
    except Exception as e:
        out = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        metrics.inc(f"fi_{metric}_errors", 1)
    metrics.inc(f"fi_{metric}_requests", 1)
    metrics.record(f"fi_{metric}_latency", time.perf_counter() - t0)
    return out


@router.get("/curve")
def curve(months: int = Query(13, ge=1, le=120)):
    """当前美债曲线 NSS 因子读数 + 关键期限（2Y/10Y/30Y）即期。"""
    return _run("rate_curve", {"months": months}, "rates")


@router.get("/forecast")
def forecast(horizon: int = Query(5, ge=1, le=30)):
    """NSS 因子 VAR 多步外推（水平中枢）+ 诚实对照 walk-forward RMSE（var vs naive）。"""
    return _run("rate_forecast", {"horizon": horizon}, "rates")


@router.get("/scenario")
def scenario():
    """利率情景库：基准/乐观/悲观/极端激进/极端避险的期限路径。"""
    return _run("rate_scenario", {}, "rates")


@router.get("/policy")
def policy():
    """最新 FOMC 声明鸽鹰分（词典法启发式，非官方口径）。"""
    return _run("policy_signal", {}, "rates")


@router.get("/stress")
def stress():
    """历史压力重演：2020 疫情 / 2022 激进加息 / 2008 危机的真实水平/斜率位移(bp)。"""
    return _run("stress_replay", {}, "rates")


@router.get("/montecarlo")
def montecarlo(n_paths: int = Query(100, ge=20, le=500),
               horizon: int = Query(21, ge=3, le=63),
               seed: int = Query(7)):
    """Monte Carlo：因子一阶差分 VAR + 正则化残差协方差 → level 因子分位路径。"""
    import time
    from app.data import fixed_income
    from app.rates import curve as C
    from app.rates import scenario as SC

    t0 = time.perf_counter()
    try:
        fi = fixed_income.get_fixed_income_data()
        curve_df = fi.get_yield_curve(months=13)
        factors = C.fit_curve_path(curve_df) if curve_df is not None else None
        if factors is None or factors.empty:
            out = {"ok": False, "reason": "Treasury 曲线为空，无法生成 Monte Carlo"}
        else:
            mc = SC.ScenarioEngine(factors).monte_carlo(n_paths=n_paths,
                                                       horizon=horizon, seed=seed)
            out = mc
            if not mc.get("ok", True):
                metrics.inc("fi_mc_errors", 1)
    except Exception as e:
        out = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        metrics.inc("fi_mc_errors", 1)
    metrics.inc("fi_mc_requests", 1)
    metrics.record("fi_mc_latency", time.perf_counter() - t0)
    return out
