# -*- coding: utf-8 -*-
"""信用评级回测 —— point-in-time walk-forward（防 look-ahead）+ AUC/KS。

防泄漏红线（沿用项目约定）：
  - 行序按**真实文件提交日 filed** 排序（不是财政年度）；训练只允许 filed<cutoff
    的观测，测试必须是 filed>=cutoff 的后续窗口 → 杜绝「用未来财报评过去」。
  - 缺失填补的本数据只在训练窗内估计（见 models._impute_medians）。

输出：每 fold 的 AUC/KS/Brier/ECE + 汇总的 pooled OOS 指标；另附回归任务（预测连续
Altman Z'' 分）R²/RMSE，供与分类互补。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.credit import calibration as calib
from app.credit import models as models_mod


def auc_ks(y_true, proba) -> dict:
    """AUC + KS 统计（KS=max(TPR-FPR)）。"""
    from sklearn.metrics import roc_auc_score, roc_curve
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(proba, dtype=float)
    if len(set(y)) < 2 or len(y) < 2:
        return {"auc": None, "ks": None, "n": int(len(y)), "reason": "单类样本"}
    auc = float(roc_auc_score(y, p))
    fpr, tpr, _ = roc_curve(y, p)
    ks = float(np.max(tpr - fpr))
    return {"auc": round(auc, 4), "ks": round(ks, 4), "n": int(len(y))}


def classification_report(y_true, proba, th: float = 0.5) -> dict:
    """阈值评分：准确率/精度/召回/F1 + Brier。"""
    from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(proba, dtype=float)
    pred = (p >= th).astype(int)
    if len(set(y)) < 2:
        return {"n": int(len(y))}
    brier = calib.brier_score(y, p)
    return {
        "n": int(len(y)), "pos_rate": round(float(y.mean()), 4),
        "accuracy": round(float(accuracy_score(y, pred)), 4),
        "precision": round(float(precision_score(y, pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y, pred, zero_division=0)), 4),
        "f1": round(float(f1_score(y, pred, zero_division=0)), 4),
        "brier": round(brier, 4),
    }


def walk_forward_credit(pooled: pd.DataFrame, features: list[str],
                        model_name: str = "lightgbm", n_folds: int = 5,
                        min_train: int = 40, min_test: int = 5,
                        test_window_months: int = 12,
                        model_params: dict | None = None) -> dict:
    """point-in-time walk-forward 二分类回测。

    返回 {ok, folds:[{cutoff,auc,ks,n,pos_rate}], pooled:{auc,ks,...},
          oos:{y,p}, note}。
    """
    df = pooled.dropna(subset=["label"]).copy()
    df["filed"] = pd.to_datetime(df["filed"])
    df = df.sort_values("filed").reset_index(drop=True)
    if len(df) < min_train + min_test * 2:
        return {"ok": False, "reason": f"样本不足（n={len(df)}）",
                "oos": {"y": [], "p": []}, "folds": []}

    try:
        import lightgbm  # noqa: F401  # 触发导入错误提示
    except Exception:
        pass

    cutoffs = [df["filed"].quantile(q) for q in np.linspace(0.15, 0.85, n_folds)]
    oos_y, oos_p, folds = [], [], []
    prev_end = None
    for c in cutoffs:
        train = df[df["filed"] < c]
        if prev_end is not None:
            lo, hi = prev_end, c
        else:
            lo, hi = c, c
        # 测试窗：cutoff 之后 test_window_months 内
        lo = c
        test = df[(df["filed"] >= lo) & (df["filed"] < lo + pd.DateOffset(months=test_window_months))]
        prev_end = c
        if len(train) < min_train or len(test) < min_test:
            continue
        Xtr, ytr = train[features], train["label"].astype(float)
        Xte = test[features]
        try:
            p = models_mod.fit_predict(model_name, Xtr, ytr, Xte,
                                       model_params=model_params)
        except Exception as e:
            folds.append({"cutoff": str(c.date()), "error": str(e)})
            continue
        yte = test["label"].astype(float).to_numpy()
        acc = auc_ks(yte, p)
        oos_y.extend(yte.tolist())
        oos_p.extend(p.tolist())
        folds.append({
            "cutoff": str(c.date()),
            "n_test": int(len(yte)),
            "pos_rate": round(float(np.mean(yte)), 4),
            "auc": acc.get("auc"), "ks": acc.get("ks"),
        })

    if not oos_y or len(set(oos_y)) < 2:
        return {"ok": False, "reason": "无有效折", "oos": {"y": oos_y, "p": oos_p},
                "folds": folds}
    oos_auc = auc_ks(np.array(oos_y), np.array(oos_p))
    oos_rep = classification_report(np.array(oos_y), np.array(oos_p))
    return {
        "ok": True,
        "model": model_name, "features": features,
        "folds": folds,
        "oos": {"y": oos_y, "p": oos_p},
        "auc": oos_auc.get("auc"), "ks": oos_auc.get("ks"),
        "report": oos_rep,
        "note": ("point-in-time walk-forward（按 filed 严格前训练后测试）。"
                 "标签为 Altman Z'' 结构代理标签（非评级机构/违约真实标签）。"),
    }
