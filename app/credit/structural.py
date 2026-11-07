# -*- coding: utf-8 -*-
"""结构信用模型 —— Merton-KMV 违约距离（理论先行、无监督）。

从财务杠杆 + 股权市值波动率，解出资产价值 V 与资产波动率 sigma_V（Merton
双方程），再算违约距离 DD 与隐含违约概率 PD=N(-DD)。

诚实边界：
  - Merton 需**股权市值 E 与股权波动率 sigma_E**（市场数据，来自 yfinance）。
    yfinance 若被限流/不可用 → available=False（如实标注，不伪造）。
  - 债务面值 D 用账面总负债作代理；无风险利率 r、期限 T 为显式参数。
"""
from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm


# ── 纯理论核（无 I/O，可单测）───────────────────────────────

def solve_merton(E: float, sigma_E: float, D: float, r: float = 0.04,
                 T: float = 1.0, tol: float = 1e-8, max_iter: int = 300) -> dict:
    """同时解 Merton 两方程，返回 {V, sigma_V, dd, pd, converged}。

    方程：E = V*N(d1) - D e^{-rT} N(d2)；sigma_E = (V/E)*sigma_V*N(d1)。
    固定点/牛顿式迭代，N(d1)→~0 时收敛退化，给予显式 converged 标记。
    """
    if D <= 0 or E <= 0 or sigma_E <= 0:
        return {"ok": False, "reason": "输入需 E>0, D>0, sigma_E>0"}
    V = E + D
    sigma_V = sigma_E * (E / (E + D))      # 初始近似
    converged = False
    for _ in range(max_iter):
        if sigma_V <= 0:
            sigma_V = 1e-6
        d1 = (math.log(V / D) + (r + 0.5 * sigma_V ** 2) * T) / (sigma_V * math.sqrt(T))
        d2 = d1 - sigma_V * math.sqrt(T)
        Nd1 = norm.cdf(d1)
        Nd2 = norm.cdf(d2)
        if Nd1 < 1e-9:                     # N(d1)≈0 时 E≈0，无市场意义
            break
        disc = D * math.exp(-r * T) * Nd2
        V_new = (E + disc) / Nd1
        sigma_V_new = sigma_E * (E / V_new) / Nd1
        if abs(V_new - V) < tol and abs(sigma_V_new - sigma_V) < tol:
            V, sigma_V = V_new, sigma_V_new
            converged = True
            break
        V, sigma_V = V_new, sigma_V_new

    d1 = (math.log(V / D) + (r + 0.5 * sigma_V ** 2) * T) / (sigma_V * math.sqrt(T))
    dd = (math.log(V / D) + (r - 0.5 * sigma_V ** 2) * T) / (sigma_V * math.sqrt(T))
    pd_val = norm.cdf(-dd)
    return {
        "ok": True, "V": float(V), "sigma_V": float(sigma_V),
        "dd": float(dd), "pd": float(pd_val), "converged": bool(converged),
    }


def default_probability(dd: float) -> float:
    """由违约距离 DD 映射到违约概率（正态假设）。"""
    return float(norm.cdf(-dd))


# ── 市场数据接入（可选，失败诚实门控）──────────────────────

def equity_vol_from_history(closes: np.ndarray, annualization: int = 252) -> float | None:
    """由日收盘价序列估算年化股权波动率；样本<10 或退化 → None。"""
    p = np.asarray(closes, dtype=float)
    if len(p) < 10 or np.any(np.isnan(p)):
        return None
    lr = np.diff(np.log(np.maximum(p, 1e-9)))
    s = np.std(lr, ddof=1)
    if not np.isfinite(s) or s <= 0:
        return None
    return float(s * math.sqrt(annualization))


def merton_from_market(market_cap: float | None, debt: float | None,
                       equity_vol: float | None, r: float = 0.04,
                       T: float = 1.0) -> dict:
    """用市场 E/σ_E + 账面负债 D 计算 Merton。输入缺失 → available=False。"""
    missing = [k for k, v in
               {"市值 E": market_cap, "负债面值 D": debt,
                "股权波动率 σ_E": equity_vol}.items() if v is None]
    if missing:
        return {"ok": False, "available": False,
                "reason": f"市场数据缺失：{'、'.join(missing)}（yfinance 限流或未接入），不构造数值。"}
    res = solve_merton(float(market_cap), float(equity_vol), float(debt), r=r, T=T)
    if not res.get("ok"):
        return {"ok": False, "available": False, "reason": res.get("reason")}
    return {
        "ok": True, "available": True,
        "E": float(market_cap), "D": float(debt), "r": r, "T": T,
        "V": res["V"], "sigma_E": float(equity_vol), "sigma_V": res["sigma_V"],
        "dd": res["dd"], "pd": res["pd"], "converged": res["converged"],
    }
