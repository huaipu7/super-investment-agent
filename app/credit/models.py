# -*- coding: utf-8 -*-
"""信用评级预测模型 —— LightGBM（树）与 MLP（神经网络）二分类。

统一 sklearn-compatible 估计器接口（.fit(X,y) / .predict_proba(X)），供
backtest.py 的 walk-forward 调用。模型函数只在训练窗内拟合（防泄漏红线：训练
分布必须截止于 cutoff 之前，见 backtest.py）。
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd


def _impute_medians(X: pd.DataFrame) -> pd.DataFrame:
    """返回训练窗内每列中位数（用于训练/测试统一填补，防跨窗泄漏）。"""
    med = X.astype(float).median()
    med[med.isna()] = 0.0
    return med


def _apply_impute(X: pd.DataFrame, med: pd.Series) -> pd.DataFrame:
    """用训练窗 medians 填补（训练窗外的测点不泄漏其自身中位数）。"""
    X = X.astype(float).copy()
    for c in X.columns:
        m = med.get(c, np.nan)
        if pd.isna(m):
            m = 0.0
        X[c] = X[c].fillna(m)
    return X


def make_model(name: str, params: dict | None = None) -> object:
    """按名字构造 sklearn 兼容分类器。name ∈ {lightgbm, mlp}。"""
    params = params or {}
    if name == "lightgbm":
        try:
            import lightgbm as lgb
        except Exception as e:  # pragma: no cover
            raise RuntimeError(f"lightgbm 不可用：{e}")
        defaults = dict(
            n_estimators=200, learning_rate=0.05, max_depth=4,
            min_child_samples=20, subsample=0.9, colsample_bytree=0.9,
            num_leaves=31, random_state=0, class_weight="balanced",
            n_jobs=-1, verbose=-1,
        )
        defaults.update(params)
        return lgb.LGBMClassifier(**defaults)
    if name == "mlp":
        from sklearn.neural_network import MLPClassifier
        defaults = dict(
            hidden_layer_sizes=(32, 16), activation="relu",
            max_iter=300, random_state=0, alpha=1e-3,
            early_stopping=True, n_iter_no_change=15,
        )
        defaults.update(params)
        return MLPClassifier(**defaults)
    raise ValueError(f"未知模型 {name}（可选 lightgbm/mlp）")


def make_regressor(name: str = "lightgbm", params: dict | None = None) -> object:
    """按名字构造回归器用于连续风险分（如 Altman Z''）预测。"""
    params = params or {}
    if name == "lightgbm":
        try:
            import lightgbm as lgb
        except Exception as e:  # pragma: no cover
            raise RuntimeError(f"lightgbm 不可用：{e}")
        defaults = dict(n_estimators=200, learning_rate=0.05, max_depth=4,
                        min_child_samples=20, subsample=0.9, colsample_bytree=0.9,
                        num_leaves=31, random_state=0, n_jobs=-1, verbose=-1)
        defaults.update(params)
        return lgb.LGBMRegressor(**defaults)
    raise ValueError(f"未知回归器 {name}（可选 lightgbm）")


def fit_predict(name: str, X_train: pd.DataFrame, y_train,
                X_test: pd.DataFrame, model_params: dict | None = None,
                return_model: bool = False) -> object | np.ndarray:
    """拟合并在 X_test 上输出正类概率（或返回模型）。中位数只在训练窗内估计。"""
    med = _impute_medians(X_train)
    X_tr = _apply_impute(X_train, med)
    X_te = _apply_impute(X_test, med)
    y_tr = np.asarray(y_train, dtype=float)
    # 神经网络对量纲敏感：MLP 用训练窗内 fitted 的 StandardScaler（point-in-time）；
    # 树模型对尺度不敏感，不做缩放以免改变其行为。
    if name == "mlp":
        from sklearn.preprocessing import StandardScaler
        sc = StandardScaler().fit(X_tr.values)
        Xtr_v, Xte_v = sc.transform(X_tr.values), sc.transform(X_te.values)
    else:
        Xtr_v, Xte_v = X_tr.values, X_te.values
    model = make_model(name, model_params)
    model.fit(Xtr_v, y_tr)
    p = model.predict_proba(Xte_v)[:, 1]
    if return_model:
        return {"proba": p, "model": model, "feat_cols": list(X_tr.columns)}
    return p
