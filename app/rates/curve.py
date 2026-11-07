# -*- coding: utf-8 -*-
"""利率曲线建模 —— Nelson-Siegel-Svensson (NSS) 拟合与因子提取。

JD 职责②：把「债券收益率走势」降维成可建模的连续因子。
NSS/Svensson 把整条收益率曲线用少量结构参数表示：
  r(t) = b0 + b1*L1(t,λ1) + b2*C1(t,λ1) + b3*C2(t,λ2)
其中：
  L1 = (1-e^{-t/λ})/(t/λ)                        —— 长期载荷
  C1 = L1 - e^{-t/λ}                             —— 第一曲率（中期驼峰）
  C2 = (1-e^{-t/λ2})/(t/λ2) - e^{-t/λ2}          —— 第二曲率（长端驼峰）

参数含义（对齐经典 Diebold-Li 解释）：
  b0 = 水平/长端利率（level）
  b1 = 期限结构斜率（slope），b1<0 表示向上倾斜
  b2 = 短中期曲率（curvature）
  b3 = 长端曲率（curvature2）
  λ1 / λ2 = 衰减速度（控制载荷峰值位置）

用 scipy.optimize.least_squares 在 maturities 上拟合，输出逐日因子序列、
拟合优度（R²）与拟合曲线。理论先行、无监督（不依赖任何商业评级/或第3方）。
"""
from __future__ import annotations

import datetime as dt
import re

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

# 美国政府官网收益率曲线列名 → 到期年限（年）。Mo=月度，Yr=年度。
_TENOR_YEARS = {
    "1 Mo": 1 / 12, "2 Mo": 2 / 12, "3 Mo": 3 / 12, "6 Mo": 6 / 12,
    "1 Yr": 1, "2 Yr": 2, "3 Yr": 3, "5 Yr": 5, "7 Yr": 7,
    "10 Yr": 10, "20 Yr": 20, "30 Yr": 30,
}


def tenor_to_years(tenor: str) -> float | None:
    """把 '10 Yr' / '6 Mo' 转成年限；无法解析返回 None。"""
    m = re.fullmatch(r"(\d+)\s*(Mo|Yr)", tenor.strip())
    if not m:
        return None
    val = float(m.group(1))
    return val / 12 if m.group(2) == "Mo" else val


# ── 载荷函数（Swensson 4 因子）──────────────────────────────
def _L1(t: np.ndarray, lam: float) -> np.ndarray:
    x = t / lam
    return np.where(x == 0, 1.0, (1 - np.exp(-x)) / np.where(x == 0, 1, x))


def _C1(t: np.ndarray, lam: float) -> np.ndarray:
    return _L1(t, lam) - np.exp(-t / lam)


def _C2(t: np.ndarray, lam2: float) -> np.ndarray:
    return (1 - np.exp(-t / lam2)) / np.where(t == 0, 1, t / lam2) - np.exp(-t / lam2)


def nss_curve(t: np.ndarray, b0, b1, b2, b3, lam1, lam2) -> np.ndarray:
    """NSS/Svensson 在到期年限 t 上求收益率（百分数）。"""
    t = np.asarray(t, dtype=float)
    return (
        b0
        + b1 * _L1(t, lam1)
        + b2 * _C1(t, lam1)
        + b3 * _C2(t, lam2)
    )


def _residuals(params: np.ndarray, mat: np.ndarray, yld: np.ndarray) -> np.ndarray:
    # 约束：λ∈[0.1, 30]，b0/b1/b2/b3 不设硬框（level 可能为负，如 2020 年初）
    b0, b1, b2, b3, lam1, lam2 = params
    return nss_curve(mat, b0, b1, b2, b3, lam1, lam2) - yld


def fit_nss(mat: np.ndarray, yld: np.ndarray) -> dict:
    """拟合单日曲线，返回因子与拟合曲线。yld 为百分数（如 4.75）。

    mat/yld 均需为实值数组；含 NaN 的期限被剔除。拟合失败返回部分结果。
    """
    mat = np.asarray(mat, dtype=float)
    yld = np.asarray(yld, dtype=float)
    mask = np.isfinite(mat) & np.isfinite(yld)
    mat, yld = mat[mask], yld[mask]
    if len(mat) < 4:
        return {"ok": False, "reason": "可用期限不足4个", "params": None, "r2": None}

    # 初值：b0=最长端，b1=短期-长期差，b2/b3=0，λ=典型值
    y0 = yld[np.argmax(mat)] if len(mat) > 1 else yld[0]
    y1 = yld[np.argmin(mat)] if len(mat) > 1 else yld[0]
    x0 = np.array([y0, y1 - y0, 0.0, 0.0, 1.0, 2.0])

    try:
        res = least_squares(_residuals, x0, args=(mat, yld),
                            bounds=([-200, -200, -200, -200, 0.1, 0.1],
                                    [200, 200, 200, 200, 30, 30]),
                            max_nfev=4000)
        params = res.x
    except Exception:
        return {"ok": False, "reason": "拟合失败", "params": None, "r2": None}

    fitted = nss_curve(mat, *params)
    try:
        ss_res = float(np.sum((yld - fitted) ** 2))
        ss_tot = float(np.sum((yld - yld.mean()) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot > 1e-12 else 1.0
    except Exception:
        r2 = None

    return {
        "ok": True,
        "params": {
            "level": float(params[0]), "slope": float(params[1]),
            "curvature": float(params[2]), "curvature2": float(params[3]),
            "lambda1": float(params[4]), "lambda2": float(params[5]),
        },
        "maturities": mat.tolist(), "yields": yld.tolist(),
        "fitted": fitted.tolist(), "r2": r2,
        "n_points": int(len(mat)),
    }


def fit_curve_path(curve: pd.DataFrame) -> pd.DataFrame:
    """对整张曲线面板逐日拟合，输出因子序列。

    输入 curve：index=交易日，columns=期限（如 '10 Yr'），值为收益率(%)。
    输出 DataFrame：index=交易日，columns=[level, slope, curvature, curvature2,
    lambda1, lambda2, r2]，对齐非空日期。
    """
    if curve is None or curve.empty:
        return pd.DataFrame()
    rows = []
    for ts, row in curve.iterrows():
        mat, yld = [], []
        for col in curve.columns:
            v = row.get(col)
            if v is None or pd.isna(v):
                continue
            yrs = tenor_to_years(str(col))
            if yrs is None:
                continue
            mat.append(yrs)
            yld.append(float(v))
        if len(mat) < 4:
            continue
        r = fit_nss(np.array(mat), np.array(yld))
        if not r.get("ok"):
            continue
        p = r["params"]
        rows.append({"date": ts, **p, "r2": r["r2"]})
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame()
    out = out.sort_values("date").set_index("date")
    out.attrs["source"] = "Nelson-Siegel-Svensson 拟合（Treasury 观测曲线）"
    out.attrs["is_point_in_time"] = True
    return out


def day_fit(curve: pd.DataFrame, date=None) -> dict:
    """拟合单日曲线（给定日期或最后一个非空日），供 API/看板展示。"""
    if curve is None or curve.empty:
        return {"ok": False, "reason": "无曲线数据"}
    if date is None:
        date = curve.index.max()
    ts = pd.to_datetime(date)
    if ts not in curve.index:
        ts = curve.index[curve.index <= ts][-1:]  # 取 <=date 最近日
        if len(ts) == 0:
            return {"ok": False, "reason": f"无 {date} 前数据"}
        ts = ts[0]
    row = curve.loc[ts]
    mat, yld = [], []
    for col in curve.columns:
        v = row.get(col)
        if v is None or pd.isna(v):
            continue
        yrs = tenor_to_years(str(col))
        if yrs is None:
            continue
        mat.append(yrs)
        yld.append(float(v))
    if len(mat) < 4:
        return {"ok": False, "reason": "该日可用期限不足", "date": str(ts.date())}
    r = fit_nss(np.array(mat), np.array(yld))
    r["date"] = str(ts.date())
    return r


def make_grid(curve_columns=None, t_max: float = 30.0, n: int = 60) -> np.ndarray:
    """构造等间距到期年限网格（默认 0.25 到 30 年），供拟合曲线插值。"""
    if curve_columns is not None:
        yrs = [tenor_to_years(c) for c in curve_columns]
        yrs = [y for y in yrs if y is not None]
        if yrs:
            return np.array(sorted(set(yrs)))
    return np.linspace(0.25, t_max, n)
