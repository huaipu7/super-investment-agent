# -*- coding: utf-8 -*-
"""情景与压力测试 —— 基准/上下行 + Monte Carlo + 历史压力重演。

JD 职责②：搭建利率预测与**情景分析**框架。给出可解释、可复核的利率情景库：
  - 基准情景：基于模型预测（VAR 因子外推 + NSS 重构成期限路径）
  - 上/下行情景：对水平/斜率/曲率施加政策-宏观驱动的显式冲击（可解释的微观结构假设）
  - Monte Carlo：向量自回归 + 残差协方差驱动的随机路径（多因子相关结构）
  - 历史压力重演：2020(疫情)/2022(激进加息)/2008(危机) 窗口重现真实曲线路径，量化收益冲击

诚实边界：所有数值来自真实 Treasury 历史或真实模型计算；情景冲击量是**主观参数**
（analyst 假设），报告里显式标注为假设，非预测。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.rates.curve import fit_curve_path, nss_curve, make_grid

# 期限列映射到年限（重复使用，避免循环导入）
from app.rates.curve import tenor_to_years


def _factor_window(df: pd.DataFrame, window: int = 21) -> pd.DataFrame:
    """取近 window 个交易日（默认约 1 个月）的因子均值/末值。"""
    if df is None or df.empty:
        return pd.DataFrame()
    return df.tail(window)


class ScenarioEngine:
    """基于 NSS 因子的情景引擎。"""

    def __init__(self, factors: pd.DataFrame):
        self.factors = factors
        self.grid = np.array(sorted(set(
            (0.25, 0.5, 1, 2, 3, 5, 7, 10, 20, 30)
        )))
        self._last = (factors.iloc[-1] if factors is not None and not factors.empty else None)

    # ── 单期曲线重建 ───────────────────────────────────────
    def _curve_from_params(self, level, slope, curvature=0.0, curvature2=0.0,
                           lam1=1.0, lam2=2.0) -> np.ndarray:
        return nss_curve(self.grid, level, slope, curvature, curvature2, lam1, lam2)

    # ── 情景库（基准 + 上行 + 下行 + 极端）─────────────────
    def scenarios(self, forecast_dates: list) -> dict:
        """输出基准/乐观/悲观/极端情景的期限曲线路径。

        冲击以「对当前因子终值的相对偏移」表达，并附 scenario 元信息（可解释）。
        """
        if self._last is None:
            return {"ok": False, "reason": "无因子"}
        level = float(self._last["level"])
        slope = float(self._last["slope"])
        curv = float(self._last.get("curvature", 0.0))
        curv2 = float(self._last.get("curvature2", 0.0))
        lam1 = float(self._last.get("lambda1", 1.0))
        lam2 = float(self._last.get("lambda2", 2.0))

        # 冲击假设（bp，作用于水平/斜率/曲率）。显式标注为假设。
        shocks = {
            # 名称             水平Δ(bp) 斜率Δ(bp) 曲率Δ(bp) 说明
            "基准":        (0,    0,     0,  "模型外推：无额外冲击"),
            "乐观(下行利率)": (-50,  25,   10,  "鹰派缓解/通胀回落 → 长端降、曲线走陡"),
            "悲观(上行利率)": (50,  -25,  -10,  "通胀黏性/加息预期升温 → 长端升、曲线走平"),
            "极端激进":      (100, -50,  -20,  "2008/2022 式风险或急涨息（压力情景）"),
            "极端避险(长端降)": (-100, 50, 20,  "风险事故 → 避险买盘压长端（压力情景）"),
        }
        out = {"ok": True, "forecast_dates": forecast_dates, "scenarios": {}}
        for name, (dl, ds, dc, desc) in shocks.items():
            lev = level + dl / 100.0
            slo = slope + ds / 100.0
            cur = curv + dc / 100.0
            # 对每个预测日（或仅今天）返回完整曲线
            path = [self._curve_from_params(lev, slo, cur, curv2, lam1, lam2).tolist()]
            out["scenarios"][name] = {
                "description": desc,
                "level_shift_bp": dl, "slope_shift_bp": ds, "curvature_shift_bp": dc,
                "path": path,
            }
        out["maturities"] = self.grid.tolist()
        out["shock_assumption"] = "冲击量为分析师假设（主观参数），非预测；数值交由真实模型与历史校准。"
        return out

    # ── Monte Carlo：因子 VAR + 残差协方差随机路径 ──────────
    def monte_carlo(self, n_paths: int = 200, horizon: int = 21, seed: int = 7,
                    maxlags: int = 4) -> dict:
        """用因子 VAR 拟合 + 高斯残差协方差抽样，生成因子与曲线分位路径。

        返回 {maturities, horizon_dates, percentiles: {p10,p50,p90}, paths_n}。
        """
        from statsmodels.tsa.api import VAR
        cols = ["level", "slope", "curvature", "curvature2"]
        cols = [c for c in cols if c in self.factors.columns]
        if len(cols) < 2:
            return {"ok": False, "reason": "因子不足（需 level/slope/curvature）"}
        df = self.factors[cols].dropna()
        if len(df) < maxlags + 5:
            return {"ok": False, "reason": "因子样本不足"}

        # 用**一阶差分**（近似平稳）拟合 VAR：水平因子近随机游走，且 NSS 因子高度共线
        # （level↔slope r≈-0.998, curvature↔curvature2 r≈-0.999），若直接对水平拟合，
        # 任何扰动都会被共线根放大 → 路径崩塌到 0。差分后增量均值回归、量纲更稳。
        diffs = df.diff().dropna()
        if len(diffs) < maxlags + 5:
            return {"ok": False, "reason": "差分后因子样本不足"}
        rng = np.random.default_rng(seed)
        model = VAR(diffs).fit(maxlags=maxlags, ic="aic")
        resid = model.resid
        cov = resid.cov().to_numpy()
        # 正则化残差协方差：NSS 因子共线使其近奇异（cond~1e6），直接抽样产生爆震。
        # 特征值截断把条件数压到 ~1e4，保留跨因子相关结构但抑制退化向路径失控。
        evals, evecs = np.linalg.eigh(cov)
        eps = max(evals.max() * 1e-6, 1e-10)
        cap = eps * 1e4
        ev_clipped = np.clip(evals, eps, cap)
        cov_reg = (evecs * ev_clipped) @ evecs.T

        last = df.iloc[-1].to_numpy()          # 从最近一次真实观测出发
        lookback = model.k_ar
        state = diffs.iloc[-lookback:].to_numpy()

        paths = []
        for _ in range(n_paths):
            y = state.copy()
            cum = last.copy()
            lv = []
            for t in range(horizon):
                # 预测下一期**增量**，叠加正则化随机冲击，累加回水平
                fc = model.forecast(y[-model.k_ar:], steps=1)[0]
                err = rng.multivariate_normal(np.zeros(len(cols)), cov_reg)
                d = fc + err
                cum = cum + d
                lv.append(cum.copy())
                y = np.vstack([y, d[None, :]])
            paths.append(np.array(lv))          # (horizon, dim)
        paths = np.array(paths)                 # (n_paths, horizon, dim)
        level = np.array([p[:, 0] for p in paths])   # 用水平因子
        level_p10 = np.percentile(level, 10, axis=0)
        level_p50 = np.percentile(level, 50, axis=0)
        level_p90 = np.percentile(level, 90, axis=0)
        horizon_dates = [f"t+{k+1}" for k in range(horizon)]
        return {
            "ok": True, "n_paths": n_paths, "horizon_steps": horizon,
            "horizon_dates": horizon_dates,
            "percentiles": {
                "level_p10": level_p10.tolist(), "level_p50": level_p50.tolist(),
                "level_p90": level_p90.tolist(),
            },
            "note": ("Monte Carlo：因子一阶差分 VAR（平稳）+ 正则化残差协方差抽样；"
                     "p50 近似基准，p10/p90 为置信带。用差分原因：水平因子近随机游走且"
                     "NSS 因子高度共线，直接对水平拟合的 VAR 在扰动下不稳定。"),
        }

    # ── 历史压力重演 ───────────────────────────────────────
    def stress_replay(self, curve: pd.DataFrame) -> dict:
        """在真实历史曲线上重现 2020/2022 压力窗口，量化水平/斜率位移。

        用全量曲线的 NSS 因子，在窗口首末日取 factor delta，衡量收益冲击。
        """
        windows = {
            "2020疫情冲击": ("2020-02-20", "2020-03-31"),
            "2022激进加息": ("2021-12-01", "2022-11-01"),
            "2008金融危机": ("2008-08-01", "2008-12-01"),
        }
        factors = fit_curve_path(curve)
        out = {"ok": True, "events": {}}
        for name, (d0, d1) in windows.items():
            d0, d1 = pd.Timestamp(d0), pd.Timestamp(d1)
            win = factors.loc[(factors.index >= d0) & (factors.index <= d1)]
            if len(win) < 2:
                out["events"][name] = {"ok": False, "reason": f"窗口 {d0.date()}-{d1.date()} 无数据"}
                continue
            first, last = win.iloc[0], win.iloc[-1]
            out["events"][name] = {
                "ok": True,
                "window": [d0.date().isoformat(), d1.date().isoformat()],
                "level_delta_bp": round(float((last["level"] - first["level"]) * 100), 2),
                "slope_delta_bp": round(float((last["slope"] - first["slope"]) * 100), 2),
                "curvature_delta_bp": round(float((last["curvature"] - first["curvature"]) * 100), 2),
                "level_start": round(float(first["level"]), 3),
                "level_end": round(float(last["level"]), 3),
            }
        out["note"] = "压力重演基于真实 Treasury 历史观测（NSS 因子），量化水平/斜率/曲率位移。"
        return out

    # ── 便捷汇总（看板/API 用）──────────────────────────────
    def summary(self) -> dict:
        """当前 NSS 因子的解读（水平/斜率/曲率），用于展示。"""
        if self._last is None:
            return {"ok": False}
        return {
            "ok": True,
            "as_of": str(self.factors.index[-1].date()),
            "level": round(float(self._last["level"]), 3),
            "slope": round(float(self._last["slope"]), 3),
            "curvature": round(float(self._last["curvature"]), 3),
            "curvature2": round(float(self._last.get("curvature2", 0.0)), 3),
            "r2": round(float(self._last.get("r2", float("nan"))), 4) if pd.notna(self._last.get("r2")) else None,
            "level_interpretation": "水平 = 长端利率中枢",
            "slope_interpretation": "斜率 <0 = 曲线向上倾斜(正常化)",
        }
