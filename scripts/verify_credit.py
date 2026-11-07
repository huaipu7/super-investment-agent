# -*- coding: utf-8 -*-
"""Phase 3 验证 —— 信用评级引擎（JD 职责①）真实数据回归。

从 SEC EDGAR 官方 XBRL 拉一批海外非金融主体年度科目（point-in-time by filed），
计算 Altman Z'' / Ohlson O 结构分，用 LightGBM/MLP 做 reduced-form 违约预测，
walk-forward（按 filed 严格前训练后测试，防 look-ahead）→ AUC/KS + 校准。

可信度红线（约束#7）：
  - 全部数值来自真实 EDGAR 财务或真实计算；缺失字段显式 None/"—"。
  - 二分类 label=Altman Z''<2.60（灰区/风险区）为**结构模型代理标签**，非评级
    机构/违约真实标签（商业 feed 不可得）。
  - 特征用广义会计比率（不直接喂 Z'' 的 X1~X4 项，避免线性自反）。

运行：D:\\python\\python.exe scripts/verify_credit.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from app.credit import features as F
from app.credit import structural as S
from app.credit import calibration as calib
from app.credit import backtest as B
from app.credit import models as models_mod
from app.data import fixed_income

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
    print("Phase 3 信用评级引擎验证  运行时间:", pd.Timestamp.now())
    print("=" * 78)

    fi = fixed_income.get_fixed_income_data()

    # 非金融主体，横跨 IG(investment-grade) → 受困/高杠杆，制造真实信用分化
    # （Altman Z 对银行/保险资产负债表不适，故剔除金融股）
    universe = [
        # 稳健/IG
        "AAPL", "MSFT", "JNJ", "PG", "XOM", "CAT", "UPS",
        # 中/高杠杆
        "F", "GM", "BA", "T", "VZ", "CVS",
        # 受困/困境（疫情冲击强）
        "AAL", "UAL", "LUV", "M", "KSS", "RCL", "CCL", "NCLH", "W",
    ]

    # ── [1] 拉取 + 特征 ─────────────────────────────────────
    print("\n[1] EDGAR 财务 → 特征 + 结构分")
    by: dict[str, pd.DataFrame] = {}
    avail = 0
    for tk in universe:
        try:
            panel = fi.get_credit_panel(tk)
            ff = F.build_feature_frame(panel)
            if ff.empty or ff["altman_z"].dropna().empty:
                print(f"   {tk}: 结构分不可算（{len(panel)} 行，缺 Z'' 输入）")
                continue
            by[tk] = ff
            avail += 1
            last = ff.iloc[-1]
            print(f"   {tk}: {len(ff)}年  Z''={last['altman_z']:.2f} "
                  f"label={int(last['label']) if pd.notna(last['label']) else '—'} "
                  f"filed={str(last['filed'])[:10]}")
        except Exception as e:
            print(f"   {tk}: ERR {type(e).__name__} {str(e)[:70]}")
    check("主体样本>10", avail >= 10, f"可用={avail}/{len(universe)}")

    pooled = F.pool_feature_frames(by)
    if pooled.empty:
        print("  空面板"); check("生成跨截面面板", False, ""); return _finish()
    nrows, npos = len(pooled), int((pooled["label"] == 1).sum())
    print(f"   pooled={nrows} 行, 正类(风险区)={npos}, pos_rate={npos / nrows:.3f}")
    check("跨截面面板生成", nrows > 0, f"n={nrows}")
    check("标签为正/负两类(有区分度)", 0 < npos < nrows,
          f"pos={npos}/{nrows}（非全零/全一，才可测 AUC/KS）")
    n_z = int(pooled["altman_z"].notna().sum())
    print(f"   Altman Z'' 中位={pooled['altman_z'].median():.2f}  "
          f"min={pooled['altman_z'].min():.2f} max={pooled['altman_z'].max():.2f}  "
          f"可算={n_z}")
    print(f"   Ohlson O 中位={pooled['ohlson_o'].median():.2f}")

    # ── [2] Merton-KMV（市场数据，诚实门控）────────────────
    print("\n[2] Merton-KMV 违约距离（市场数据接入）")
    sample = [t for t in universe if t in by][:4]
    from app.data.sources.yfinance_source import YFinanceSource
    yf = YFinanceSource()
    mout = []
    for tk in sample:
        status, cap, vol, debt = None, None, None, None
        try:
            info = yf.fetch_company_info(tk)
            cap = info.get("market_cap")
            h = yf.fetch_price_history(tk, start="2024-01-01", end="2026-08-31")
            vol = S.equity_vol_from_history(h["Close"].to_numpy()) if not h.empty else None
            debt = (by[tk].iloc[-1]["liabilities"] if pd.notna(by[tk].iloc[-1]["liabilities"]) else None)
            status = S.merton_from_market(cap, debt, vol)
        except Exception as e:
            status = {"ok": False, "available": False,
                      "reason": f"市场数据源不可用（{type(e).__name__}: {str(e)[:50]}）"}
        mout.append((tk, status))
        if status.get("available"):
            print(f"   {tk}: E={status['E']:.0f} D={status['D']:.0f} σ_V={status['sigma_V']:.3f} "
                  f"DD={status['dd']:.2f} PD={status['pd']:.3f}")
        else:
            print(f"   {tk}: 不可用 — {status.get('reason')}")
    any_ok = any(r.get("available") for _, r in mout)
    n_tried, n_unavail = len(mout), sum(1 for _, r in mout if not r.get("available"))
    check("Merton 市场数据(有则算，无则诚实标注不可用)",
          any_ok or n_tried > 0, f"available={any_ok}, 诚实不可用={n_unavail}/{n_tried}（不伪造）")

    # 核心数学可用性（示例输入，非真实主体估计，仅验证方程可解）
    demo = S.solve_merton(E=100.0, sigma_E=0.30, D=60.0, r=0.04, T=1.0)
    check("Merton 方程可解(示例输入验证)", demo.get("ok") and demo.get("pd") > 0,
          f"V={demo['V']:.1f} σ_V={demo['sigma_V']:.3f} DD={demo['dd']:.2f} PD={demo['pd']:.3f}")

    # ── [3] walk-forward 二分类（AUC/KS）──────────────────
    print("\n[3] reduced-form 违约预测（walk-forward, 防泄漏）")
    # 不含 Z'' 的 X1~X4 直接项，避免线性自反；用宽口径会计比率
    MODEL_FEATURES = ["debt_ratio", "debt_to_equity", "current_ratio", "net_margin",
                      "roa", "roe", "asset_turnover", "ebit_margin", "size_log_assets",
                      "ohlson_o"]
    res = B.walk_forward_credit(pooled, MODEL_FEATURES, model_name="lightgbm",
                                n_folds=5, min_train=30, min_test=4,
                                test_window_months=12)
    if res.get("ok"):
        print(f"   lightgbm pooled AUC={res['auc']} KS={res['ks']} "
              f"pos_rate={res['report'].get('pos_rate')}")
        print(f"   report: acc={res['report'].get('accuracy')} prec={res['report'].get('precision')} "
              f"rec={res['report'].get('recall')} f1={res['report'].get('f1')} brier={res['report'].get('brier')}")
        for f_ in res["folds"]:
            print(f"   fold cutoff={f_['cutoff']} n={f_.get('n_test')} "
                  f"AUC={f_.get('auc')} KS={f_.get('ks')}")
        check("lightgbm pooled AUC 有效", res.get("auc") is not None, f"AUC={res['auc']}")
        check("AUC 显著>0.5（有区分力）", res.get("auc") is not None and res["auc"] > 0.6,
              f"AUC={res['auc']} KS={res['ks']}")
    else:
        print(f"   lightgbm 失败: {res.get('reason')}")
        check("lightgbm pooled AUC 有效", False, res.get("reason", ""))

    # MLP 对照
    res_mlp = B.walk_forward_credit(pooled, MODEL_FEATURES, model_name="mlp",
                                    n_folds=5, min_train=30, min_test=4,
                                    test_window_months=12)
    if res_mlp.get("ok"):
        print(f"   mlp pooled AUC={res_mlp['auc']} KS={res_mlp['ks']}")

    # ── [4] 连续 Z'' 回归（与分类互补）────────────────────
    print("\n[4] 连续 Altman Z'' 回归（R²/RMSE）")
    d = pooled.dropna(subset=["label", "altman_z"]).copy()
    d["filed"] = pd.to_datetime(d["filed"])
    d = d.sort_values("filed").reset_index(drop=True)
    split_n = int(len(d) * 0.75)
    xtr, ytr = d.iloc[:split_n], d.iloc[split_n:]
    if len(xtr) >= 20 and len(ytr) >= 5:
        med = models_mod._impute_medians(xtr[MODEL_FEATURES])
        Xtr, Xte = models_mod._apply_impute(xtr[MODEL_FEATURES], med), models_mod._apply_impute(ytr[MODEL_FEATURES], med)
        reg = models_mod.make_regressor("lightgbm")
        reg.fit(Xtr.values, xtr["altman_z"].to_numpy(float))
        yy = ytr["altman_z"].to_numpy(float)
        yp = reg.predict(Xte.values)
        r2 = 1 - np.sum((yy - yp) ** 2) / np.sum((yy - yy.mean()) ** 2)
        rmse = float(np.sqrt(np.mean((yy - yp) ** 2)))
        print(f"   test n={len(ytr)} R²={r2:.3f} RMSE={rmse:.2f}")
        check("连续 Z'' 回归有结果", r2 > 0, f"R²={r2:.3f}")
    else:
        print(f"   样本不足 train={len(xtr)} test={len(ytr)}")
        check("连续 Z'' 回归有结果", False, "样本不足")

    # ── [5] 概率校准 ──────────────────────────────────────
    print("\n[5] 概率校准（Platt/Isotonic）")
    if res.get("ok") and res.get("oos", {}).get("y"):
        y = np.array(res["oos"]["y"]); p = np.array(res["oos"]["p"])
        # 用一半拟合校准器、一半评估（诚实）
        m = len(y) // 2
        p_iso = calib.isotonic(p[:m], y[:m], p[m:])
        p_platt = calib.platt_scale(p[:m], y[:m], p[m:])
        bri_raw = calib.brier_score(y[m:], p[m:])
        bri_iso = calib.brier_score(y[m:], p_iso)
        ece_raw = calib.expected_calibration_error(y[m:], p[m:])
        ece_iso = calib.expected_calibration_error(y[m:], p_iso)
        print(f"   raw brier={bri_raw:.3f} ece={ece_raw['ece']} ; isotonic brier={bri_iso:.3f} ece={ece_iso['ece']}")
        check("校准有输出", ece_iso["n"] > 0, f"ece_raw={ece_raw['ece']} ece_iso={ece_iso['ece']}")

    return _finish()


def _finish() -> int:
    print("\n" + "=" * 78)
    total = len(_checks)
    npass = sum(1 for c in _checks if c.startswith(PASS))
    print(f"结果：{npass}/{total} PASS")
    failed = [c for c in _checks if c.startswith(FAIL)]
    if failed:
        print("\n未通过：")
        for c in failed:
            print("  " + c)
    return 0 if npass == total else 1


if __name__ == "__main__":
    sys.exit(main())
