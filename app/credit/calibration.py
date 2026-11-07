# -*- coding: utf-8 -*-
"""信用概率校准 —— Platt / Isotonic + Brier / 期望校准误差(ECE)。

模型输出的风险概率需校准到经验违约率口径。这里提供两种校准器与两种准确度/置信度
指标。校准器只在训练窗内拟合，测试窗应用（防泄漏）。
"""
from __future__ import annotations

import numpy as np


def platt_scale(raw: np.ndarray, y: np.ndarray,
                raw_test: np.ndarray) -> np.ndarray:
    """Platt 缩放：把 log-odds 用逻辑回归校准。"""
    from sklearn.linear_model import LogisticRegression
    raw = np.asarray(raw, dtype=float).reshape(-1, 1)
    y = np.asarray(y, dtype=float)
    clf = LogisticRegression(C=1e6, max_iter=1000)
    clf.fit(raw, y)
    return clf.predict_proba(np.asarray(raw_test, dtype=float).reshape(-1, 1))[:, 1]


def isotonic(raw: np.ndarray, y: np.ndarray, raw_test: np.ndarray) -> np.ndarray:
    """Isotonic 回归（保序）校准。"""
    from sklearn.isotonic import IsotonicRegression
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(np.asarray(raw, dtype=float), np.asarray(y, dtype=float))
    return iso.predict(np.asarray(raw_test, dtype=float))


def brier_score(y_true: np.ndarray, proba: np.ndarray) -> float:
    """Brier 分数：mean((p-y)^2)，越小越准。"""
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(proba, dtype=float)
    return float(np.mean((p - y) ** 2))


def expected_calibration_error(y_true: np.ndarray, proba: np.ndarray,
                               bins: int = 10) -> dict:
    """分桶期望校准误差：|桶内均值概率 - 桶内实际正例率| 加权平均。"""
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(proba, dtype=float)
    if bins < 1:
        bins = 1
    edges = np.linspace(0, 1, bins + 1)
    ece, n = 0.0, len(p)
    details = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (p >= lo) & (p < hi) if i < bins - 1 else (p >= lo) & (p <= hi)
        cnt = int(mask.sum())
        if cnt == 0:
            continue
        conf = float(p[mask].mean())
        acc = float(y[mask].mean())
        ece += (cnt / n) * abs(conf - acc)
        details.append({"bin": [round(lo, 2), round(hi, 2)], "n": cnt,
                        "conf": round(conf, 3), "acc": round(acc, 3)})
    return {"ece": round(ece, 4), "n": int(n), "details": details}
