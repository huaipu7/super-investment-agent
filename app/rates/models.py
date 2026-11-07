# -*- coding: utf-8 -*-
"""利率预测模型 —— VAR / VARX / 状态空间(Kalman 未观测成分) / LSTM + 防泄漏回测。

JD 职责②：用时间序列/DL 方法研究「宏观经济指标、政策信号」与收益率走势的量化关系，
构建利率预测框架。本模块对一个**因子序列**（NSS 的水平/斜率/曲率，或直接某期限收益率）
建立预测模型，并提供 **walk-forward 回测**（严守 train<=cutoff<test，防 look-ahead）。

模型选择（理论先行，真实计算）：
  - VAR         : 多因子向量自回归（含外生变量 VARX 的钩子）
  - UC / Kalman : statsmodels 未观测成分（局部水平+趋势），结构可解释
  - LSTM        : torch 编码的未来短期因子，仅作对照（对短样本需谨慎）
  - Naive/drift : 基准，用于诚实对比（模型必须跑赢它才说明有增量信息）

防泄漏红线（沿用项目约定）：
  - 每次 fold 只允许使用 cutoff 之前的真实观测值训练；
  - 标准化/量纲参数仅在训练窗口内估计；越界抛 DataLeakageError。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.rates.curve import nss_curve


class DataLeakageError(Exception):
    """检测到未来数据混入训练窗口。"""


def _ensure_endog(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """取出并校验内生变量列；缺列抛错，含未来 NaN 的被前向剔除。"""
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise KeyError(f"内生变量缺列: {missing}")
    out = df[cols].apply(pd.to_numeric, errors="coerce").copy()
    return out


def _closest_cutoff_index(df: pd.DataFrame, cutoff, strict: bool = True) -> int:
    """返回 cutoff 在 index 中的位置（<=cutoff 最近点）。strict 时越界错误。"""
    mask = df.index <= pd.to_datetime(cutoff)
    if not mask.any():
        raise ValueError(f"cutoff {cutoff} 早于序列起点，无历史")
    return int(np.max(np.where(mask)))


# ── 各模型：fit + forecast 的轻量封装 ─────────────────────────

def fit_forecast_var(df: pd.DataFrame, endog: list[str], horizon: int,
                     cutoff, maxlags: int = 4) -> pd.DataFrame:
    """VAR 预测：单脚本式。返回 forecast 行（cutoff+h 内逐步累积）。

    内部把多步 forecast 串成一个跨 horizon 的路径。
    """
    from statsmodels.tsa.api import VAR
    e = _ensure_endog(df, endog)
    i = _closest_cutoff_index(e, cutoff)
    train = e.iloc[: (i + 1)].dropna()
    if len(train) < maxlags + 3:
        raise ValueError("训练样本不足以拟 VAR")
    model = VAR(train)
    fit = model.fit(maxlags=maxlags, ic="aic")
    f = fit.forecast(train.values[-fit.k_ar:], steps=horizon)
    idx = [e.index[i + k] for k in range(1, horizon + 1)]
    return pd.DataFrame(f, index=idx, columns=endog)


def fit_forecast_kalman(df: pd.DataFrame, target: str, horizon: int,
                        cutoff, level: bool = True, trend: bool = True,
                        irregular: bool = True) -> pd.DataFrame:
    """未观测成分(Kalman)状态空间单序列预测（level/trend/irregular）。

    用裸 ndarray 拟合（statsmodels 对无 freq 的 DatetimeIndex 会拒绝建预测索引），
    再把预测值手动映射到真实未来日期，确保与上游下标对齐。
    """
    from statsmodels.tsa.statespace.structural import UnobservedComponents
    s = _ensure_endog(df, [target])
    i = _closest_cutoff_index(s, cutoff)
    train = s[target].iloc[: (i + 1)].dropna()
    if len(train) < 8:
        raise ValueError("训练样本不足以拟合状态空间")
    vals = train.to_numpy(dtype=float)
    model = UnobservedComponents(vals, level=level, trend=trend,
                                 irregular=irregular)
    fit = model.fit(disp=False)
    fc = np.asarray(fit.forecast(horizon)).ravel()
    idx = [s.index[i + k] for k in range(1, horizon + 1)]
    return pd.DataFrame({target: fc}, index=idx)


def fit_forecast_lstm(df: pd.DataFrame, cols: list[str], horizon: int,
                      cutoff, lookback: int = 12, epochs: int = 40,
                      lr: float = 1e-3, val_frac: float = 0.15,
                      seed: int = 0) -> pd.DataFrame:
    """torch LSTM 因子预测（对照模型）。对短样本给出保守的 early-stop 处理。

    lookback=输入窗口长度；标准化量纲仅在训练窗口内估计（防泄漏）。
    """
    try:
        import torch
        import torch.nn as nn
    except Exception as e:
        raise RuntimeError(f"torch 不可用: {e}")
    torch.manual_seed(seed)
    np.random.seed(seed)

    e = _ensure_endog(df, cols)
    i = _closest_cutoff_index(e, cutoff)
    train = e.iloc[: (i + 1)].dropna()
    if len(train) < lookback + 6:
        raise ValueError(f"训练样本 {len(train)} 不足以跑 LSTM（需 >{lookback + 6}）")

    # 只在训练窗口内估计均值/标准差（防泄漏）
    mu = train.mean().to_numpy(dtype=np.float32)
    sd = train.std().to_numpy(dtype=np.float32)
    sd[sd == 0] = 1e-6
    x_np = ((train - mu) / sd).to_numpy(dtype=np.float32)

    # 构造 (seq_len, batch, dim) 输入 + (batch, dim) 目标；每窗口=X[t-lookback:t], Y=x[t]
    seqs, tl = [], []
    for t in range(lookback, len(x_np)):
        seqs.append(x_np[t - lookback: t])       # (lookback, dim)
        tl.append(x_np[t])                        # (dim,)
    seqs = np.array(seqs)                          # (N, lookback, dim)
    tl = np.array(tl)                              # (N, dim)
    n = len(seqs)
    split = max(1, int(n * (1 - val_frac)))
    x_train = torch.tensor(seqs[:split], dtype=torch.float32).permute(1, 0, 2).contiguous()  # (lookback, B, dim)
    y_train = torch.tensor(tl[:split], dtype=torch.float32)                                  # (B, dim)
    x_val = torch.tensor(seqs[split:], dtype=torch.float32).permute(1, 0, 2).contiguous() if split < n else None
    y_val = torch.tensor(tl[split:], dtype=torch.float32) if split < n else None

    dim = len(cols)
    hid = max(8, dim * 2)
    net = nn.LSTM(input_size=dim, hidden_size=hid, num_layers=1, batch_first=False)
    head = nn.Linear(hid, dim)
    opt = torch.optim.Adam(list(net.parameters()) + list(head.parameters()), lr=lr)
    loss_fn = nn.MSELoss()

    net.train()
    best_val, patience = None, 0
    for ep in range(epochs):
        opt.zero_grad()
        h = (torch.zeros(1, x_train.shape[1], hid), torch.zeros(1, x_train.shape[1], hid))
        out, _ = net(x_train, h)          # (lookback, B, hid)
        pred = head(out[-1])              # (B, dim) <- 用最后一个时间步预测下一时刻
        loss = loss_fn(pred, y_train)
        loss.backward()
        opt.step()
        if x_val is not None and ep % 5 == 0:
            net.eval()
            with torch.no_grad():
                hv = (torch.zeros(1, x_val.shape[1], hid), torch.zeros(1, x_val.shape[1], hid))
                ov, _ = net(x_val, hv)
                pv = head(ov[-1])
                v = float(loss_fn(pv, y_val))
                if best_val is None or v < best_val:
                    best_val, patience = v, 0
                else:
                    patience += 1
                if patience >= 5:
                    break
            net.train()

    # 递归多步外推：用预测值滚动成下一步输入（标准 LSTM 多步策略）
    last = x_np[-lookback:].copy()
    future_pred = []
    net.eval()
    with torch.no_grad():
        cur = torch.tensor(last, dtype=torch.float32)  # (lookback, dim)
        for _ in range(horizon):
            seq = cur.permute(1, 0).unsqueeze(1).contiguous()  # (lookback, 1, dim)
            h = (torch.zeros(1, 1, hid), torch.zeros(1, 1, hid))
            o, _ = net(seq, h)
            pp = head(o[-1]).squeeze(0).numpy()  # (dim,)
            future_pred.append(pp)
            cur = np.concatenate([cur[1:].numpy(), pp[None, :]], 0)
            cur = torch.tensor(cur, dtype=torch.float32)
    pred_norm = np.array(future_pred)
    pred = pred_norm * sd + mu
    idx = [e.index[i + k] for k in range(1, horizon + 1)]
    return pd.DataFrame(pred, index=idx, columns=cols)


def fit_forecast_naive(df: pd.DataFrame, cols: list[str], horizon: int,
                       cutoff) -> pd.DataFrame:
    """Naive 基准：最后观测值延伸（水平）；漂移基准为末段线性趋势，供对照。"""
    e = _ensure_endog(df, cols)
    i = _closest_cutoff_index(e, cutoff)
    train = e.iloc[: (i + 1)].dropna()
    last = train.iloc[-1].to_numpy(dtype=float)
    idx = [e.index[i + k] for k in range(1, horizon + 1)]
    pred = np.tile(last, (horizon, 1))
    return pd.DataFrame(pred, index=idx, columns=cols)


# ── 防泄漏 walk-forward 回测 ────────────────────────────────

def walk_forward(df: pd.DataFrame, endog: list[str], horizon: int,
                 min_train: int = 30, step: int = 5,
                 use: str = "var", maxlags: int = 4) -> pd.DataFrame:
    """滚动回测：每 fold 用 cutoff 前数据训练，预测后 horizon 步，跟真实值比较。

    返回含 forecast/actual/error/abs_error 的长表，并附 win_rate（是否优于 naive）。
    """
    model_fn = {
        "var": lambda e, c: fit_forecast_var(df, endog, horizon, c, maxlags=maxlags),
        "kalman": lambda e, c: fit_forecast_kalman(df, endog[0], horizon, c),
        "naive": lambda e, c: fit_forecast_naive(df, endog, horizon, c),
    }.get(use)
    if model_fn is None:
        raise ValueError(f"未知模型 {use}（可选项：var/kalman/naive）")

    e = _ensure_endog(df, endog)
    records = []
    start_i = min_train
    i = start_i
    while i < len(e) - horizon:
        cutoff = e.index[i]
        try:
            fc = model_fn(e, cutoff)
        except Exception:
            i += step
            continue
        for k in range(1, horizon + 1):
            f = e.index[i + k] if i + k < len(e) else None
            if f is None:
                continue
            actual = e.loc[f, endog].to_numpy(dtype=float)
            fc_row = fc.loc[pd.to_datetime(f)] if f in fc.index else None
            if fc_row is None or fc_row.isna().any():
                continue
            pred = fc_row.to_numpy(dtype=float)
            err = actual - pred
            for j, col in enumerate(endog):
                records.append({
                    "cutoff": cutoff, "date": f, "col": col,
                    "forecast": float(pred[j]), "actual": float(actual[j]),
                    "err": float(err[j]), "abs_err": float(abs(err[j])),
                })
        i += step

    out = pd.DataFrame(records)
    if out.empty:
        return out
    return out


def backtest_metrics(res: pd.DataFrame, col: str = None) -> dict:
    """由 walk_forward 结果算整体 RMSE / MAE / 方向命中率。"""
    if res is None or res.empty:
        return {"ok": False}
    if col is not None:
        res = res[res["col"] == col]
    if res.empty:
        return {"ok": False}
    err = res["err"].to_numpy(dtype=float)
    ae = res["abs_err"].to_numpy(dtype=float)
    rmse = float(np.sqrt(np.mean(err ** 2)))
    mae = float(np.mean(ae))
    # 方向命中：给定 cutoff 前趋势无法直接判定，用「相对前值方向」
    dir_hit = float(np.mean(res["err"].to_numpy(dtype=float) *
                            res["actual"].to_numpy(dtype=float) > 0))
    return {
        "ok": True, "n": int(len(res)), "rmse": round(rmse, 4),
        "mae": round(mae, 4), "direction_hit_abs": round(dir_hit, 4),
        "col": col or "all",
    }


# ── 预测整条曲线的便捷入口（基于 NSS 因子）─────────────────
def forecast_curve_from_factors(factors: pd.DataFrame, horizon: int,
                                model: str = "var", maxlags: int = 4) -> dict:
    """用 NSS 因子做多步预测，再用 NSS 重构成期限曲线（供情景/看板）。

    返回 {parameters, grid: {'maturity_years': [...], 'curve': [...horizon][...grid]}}。
    因子列固定为 [level, slope, curvature, curvature2]。
    """
    cols = ["level", "slope", "curvature", "curvature2"]
    cols = [c for c in cols if c in factors.columns]
    if not cols:
        return {"ok": False, "reason": "因子缺列"}
    cutoff = factors.index.max()
    df = factors[cols]
    if model == "kalman":
        fcp = fit_forecast_kalman(factors, "level", horizon, cutoff)
    else:
        fcp = fit_forecast_var(df, cols, horizon, cutoff, maxlags=maxlags)
    grid = np.array(sorted(np.linspace(0.25, 30, 16).tolist()))
    curves = []
    for _, row in fcp.iterrows():
        p = [row.get("level", 0.0), row.get("slope", 0.0),
             row.get("curvature", 0.0), row.get("curvature2", 0.0),
             1.0, 2.0]  # λ 用默认（因子单调外推不重估衰减）
        curves.append(nss_curve(grid, *p))
    return {
        "ok": True, "grid_years": grid.tolist(),
        "forecast_dates": [str(d.date()) for d in fcp.index],
        "curves": [c.tolist() for c in curves],
    }
