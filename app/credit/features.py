# -*- coding: utf-8 -*-
"""信用评级特征工程 —— 会计比率 + Altman Z'' / Ohlson O 结构分。

输入是 EdgarSource.fetch_credit_panel 的年度科目面板（point-in-time），
输出逐财政年度的特征行（含结构风险分与代理标签），全部真实计算，NaN 安全。

诚实边界：
  - Altman Z'' 采用**账面权益 X4**（无市场 feed 也能算；原版用市值 X4 需市场数据）。
  - Ohlson O-score 的 FUTL（资金流/负债）用营业利润作代理项，如实标注。
  - 二分类标签 label=1 表示「Altman Z'' < ALTMAN_LABEL_CUT（灰区/风险区）」，
    **是结构模型代理标签，而非评级机构/违约真实标签**（商业 feed 拿不到）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Altman Z''（私人/新兴市场账面 X4 版）分区：Z''>=2.60 安全，1.10~2.60 灰色，<1.10 风险。
# 代理标签取 Z''<2.60（灰区+风险区）二分类，常数与分区预置明文供复核。
ALTMAN_LABEL_CUT = 2.60

_FEATURES = [
    "debt_ratio", "debt_to_equity", "current_ratio", "net_margin",
    "roa", "roe", "asset_turnover", "ebit_margin", "size_log_assets",
    "wcta", "reta", "ebita", "eq_lia",
]


def _safe_div(n: pd.Series, d: pd.Series) -> pd.Series:
    """NaN 安全除法：分母 0 → NaN，inf → NaN。"""
    d = d.replace(0, np.nan)
    out = n / d
    return out.replace([np.inf, -np.inf], np.nan)


def financial_ratios(panel: pd.DataFrame) -> pd.DataFrame:
    """由科目面板计算一组会计比率（NaN 安全）。"""
    out = pd.DataFrame(index=panel.index)
    out["debt_ratio"] = _safe_div(panel["liabilities"], panel["assets"])
    out["debt_to_equity"] = _safe_div(panel["liabilities"], panel["equity"])
    out["current_ratio"] = _safe_div(panel["current_assets"], panel["current_liabilities"])
    out["net_margin"] = _safe_div(panel["net_income"], panel["revenue"])
    out["roa"] = _safe_div(panel["net_income"], panel["assets"])
    out["roe"] = _safe_div(panel["net_income"], panel["equity"])
    out["asset_turnover"] = _safe_div(panel["revenue"], panel["assets"])
    out["ebit_margin"] = _safe_div(panel["operating_income"], panel["revenue"])
    out["size_log_assets"] = np.log(panel["assets"].clip(lower=1.0))
    # 结构分用到的基础项（留作特征，不代表标签线性可还原）
    wc = panel["current_assets"] - panel["current_liabilities"]
    out["wcta"] = _safe_div(wc, panel["assets"])
    out["reta"] = _safe_div(panel["retained_earnings"], panel["assets"])
    out["ebita"] = _safe_div(panel["operating_income"], panel["assets"])
    out["eq_lia"] = _safe_div(panel["equity"], panel["liabilities"])
    return out


def altman_z(panel: pd.DataFrame) -> pd.Series:
    """Altman Z'' 连续分（账面 X4，私人/新兴市场版）。"""
    wc = panel["current_assets"] - panel["current_liabilities"]
    X1 = _safe_div(wc, panel["assets"])
    X2 = _safe_div(panel["retained_earnings"], panel["assets"])
    X3 = _safe_div(panel["operating_income"], panel["assets"])
    X4 = _safe_div(panel["equity"], panel["liabilities"])
    z = 6.56 * X1 + 3.26 * X2 + 6.72 * X3 + 1.05 * X4 + 3.25
    return z


def ohlson_o(panel: pd.DataFrame) -> pd.Series:
    """Ohlson O-score（财务困境）。FUTL 用营业利润/负债作代理项。

    O>0（经验上约 >0.5）提示困境；此处作为连续风险特征输出，不作标签。
    """
    wc = panel["current_assets"] - panel["current_liabilities"]
    tlta = _safe_div(panel["liabilities"], panel["assets"])
    wcta = _safe_div(wc, panel["assets"])
    clca = _safe_div(panel["current_liabilities"], panel["current_assets"])
    nita = _safe_div(panel["net_income"], panel["assets"])
    futl = _safe_div(panel["operating_income"], panel["liabilities"])  # 代理项
    intwo = (panel["net_income"] < 0).astype(float)
    oeneg = (panel["liabilities"] > panel["assets"]).astype(float)
    ni = panel["net_income"]
    ni_prev = ni.shift(1)
    denom = ni.abs() + ni_prev.abs()
    chin = (
        (ni - ni_prev) / denom.replace(0, np.nan)
    )
    o = (-1.32 - 0.407 * np.log(panel["assets"].clip(lower=1.0))
         + 6.03 * tlta - 1.43 * wcta + 0.0757 * clca
         - 1.72 * nita - 2.37 * futl - 1.83 * intwo
         + 0.285 * oeneg - 0.521 * chin)
    return o


def build_feature_frame(panel: pd.DataFrame) -> pd.DataFrame:
    """把单主体年度科目面板转成（带特征 + 结构分 + 代理标签）的行。

    返回 index=财政年终日，columns=[财年特征..., altman_z, ohlson_o, label, filed]。
    """
    if panel is None or panel.empty:
        return pd.DataFrame()
    # 统一类型：instant 字段偶尔以 None 存在（object dtype），强制数值化，None→NaN，
    # 避免除乘 ufunc 因 None 抛 TypeError。filed 是日期字符串，保留原值不强制数值化。
    numeric_cols = [c for c in panel.columns if c not in ("filed",)]
    panel[numeric_cols] = panel[numeric_cols].apply(pd.to_numeric, errors="coerce")
    feat = financial_ratios(panel)
    z = altman_z(panel)
    o = ohlson_o(panel)
    out = feat.copy()
    out["altman_z"] = z
    out["ohlson_o"] = o
    # 标签：Altman Z'' 风险/灰区（<2.60）。缺失（任一 X 为 NaN）→ NaN，不强制。
    out["label"] = (z < ALTMAN_LABEL_CUT).astype(float).where(z.notna(), np.nan)
    out["filed"] = panel["filed"] if "filed" in panel.columns else pd.NaT
    return out


def pool_feature_frames(by_ticker: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """把 {ticker: feature_frame} 堆叠成跨截面面板，附 ticker 列并统一缺失。"""
    frames = []
    for ticker, ff in by_ticker.items():
        if ff is None or ff.empty:
            continue
        ff = ff.copy()
        ff["ticker"] = ticker
        frames.append(ff.reset_index().rename(columns={"index": "end"}))
    if not frames:
        return pd.DataFrame()
    pooled = pd.concat(frames, ignore_index=True)
    for c in _FEATURES + ["altman_z", "ohlson_o", "label"]:
        if c in pooled.columns:
            pooled[c] = pooled[c].astype(float)
    return pooled
