"""技术指标计算模块（纯 pandas/numpy，确定性、无 LLM）。

输入为 MarketData.get_history() 返回的 OHLCV DataFrame
（index=DatetimeIndex，列 Open/High/Low/Close/Volume）。所有指标基于最后一根
bar 计算（标量），数据不足的字段显式置 None，不编造。空 DataFrame / 缺列时
返回 {"available": False, "reason": ...} 且不抛异常。

风格对齐 app/agent/scorer.py：纯规则、确定性、可复现。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _latest(series: pd.Series) -> float | None:
    """取序列最后一个非 NaN 值；全空返回 None。"""
    s = series.dropna()
    if s.empty:
        return None
    return float(s.iloc[-1])


def indicator_series(df: pd.DataFrame) -> dict[str, pd.Series]:
    """整段序列的技术指标（供交互 K 线副图叠加，非标量）。

    与 compute_indicators 同口径：MA5/20/60、布林带(20,2σ)、MACD(12,26,9)、
    RSI(14, Wilder)、OBV。空/缺列 DataFrame 返回空 dict。前向索引与 df 一致。
    """
    if df is None or len(df) == 0:
        return {}
    req = {"Open", "High", "Low", "Close"}
    if req - set(df.columns):
        return {}
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)

    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    boll_mid = close.rolling(20).mean()
    boll_std = close.rolling(20).std(ddof=0)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = dif - dea

    rsi = _rsi_series(close, 14)

    out = {
        "ma5": ma5, "ma20": ma20, "ma60": ma60,
        "boll_upper": boll_mid + 2 * boll_std,
        "boll_mid": boll_mid,
        "boll_lower": boll_mid - 2 * boll_std,
        "macd_dif": dif, "macd_dea": dea, "macd_hist": hist,
        "rsi14": rsi,
    }
    if "Volume" in df.columns:
        vol = df["Volume"].astype(float)
        out["obv"] = (np.sign(close.diff()).fillna(0.0) * vol).cumsum()
    return out


def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI(period) 全序列（Wilder 平滑）。不足 period+1 根时全 NaN。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.where(avg_loss != 0.0, np.where(avg_gain > 0.0, 100.0, 50.0))
    return rsi


def compute_indicators(df: pd.DataFrame) -> dict:
    """计算最新交易日的技术指标摘要。

    Args:
        df: OHLCV DataFrame（含列 Open/High/Low/Close，Volume 可选）

    Returns:
        dict: {
            "available": bool,
            "reason": str|None,          # available=False 时的原因
            "close": float|None,         # 最新收盘价
            "ma5"/"ma20"/"ma60": float|None,
            "trend": "多头"|"空头"|"粘合"|None,   # MA20 vs MA60
            "rsi14": float|None,
            "macd_dif"/"macd_dea"/"macd_hist": float|None,
            "boll_upper"/"boll_mid"/"boll_lower": float|None,
            "atr14": float|None,
            "volatility_20d": float|None,      # 20 日年化波动率
            "pos_52w": float|None,             # 52周高低位置 [0,1]，超出夹到 [0,1.2]
            "support_20": float|None,          # 近 20 日最低价
            "resistance_20": float|None,       # 近 20 日最高价
            "high_52w"/"low_52w": float|None,
            "n_rows": int,
        }
    """
    if df is None or len(df) == 0:
        return {"available": False, "reason": "无历史行情数据", "n_rows": 0}

    req = {"Open", "High", "Low", "Close"}
    missing = req - set(df.columns)
    if missing:
        return {"available": False,
                "reason": f"历史行情缺列: {sorted(missing)}", "n_rows": len(df)}

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    out: dict = {"available": True, "reason": None, "n_rows": int(len(df))}

    # ── 最新收盘价 ──
    out["close"] = _latest(close)

    # ── 趋势：MA5 / MA20 / MA60 ──
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    out["ma5"] = _latest(ma5)
    out["ma20"] = _latest(ma20)
    out["ma60"] = _latest(ma60)

    m20, m60 = out["ma20"], out["ma60"]
    if m20 is not None and m60 is not None:
        tol = max(m60 * 0.005, 1e-9)  # 0.5% 以内视为粘合
        if m20 > m60 + tol:
            out["trend"] = "多头"
        elif m20 < m60 - tol:
            out["trend"] = "空头"
        else:
            out["trend"] = "粘合"
    else:
        out["trend"] = None

    # ── 动量：RSI(14)（Wilder 平滑）──
    out["rsi14"] = _rsi(close, 14)

    # ── MACD(12,26,9) ──
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    out["macd_dif"] = _latest(dif)
    out["macd_dea"] = _latest(dea)
    hist = dif - dea
    out["macd_hist"] = _latest(hist)

    # ── 布林带 (20, 2σ) ──
    boll_mid = close.rolling(20).mean()
    boll_std = close.rolling(20).std(ddof=0)
    out["boll_mid"] = _latest(boll_mid)
    out["boll_upper"] = _latest(boll_mid + 2 * boll_std)
    out["boll_lower"] = _latest(boll_mid - 2 * boll_std)

    # ── ATR(14) ──
    out["atr14"] = _atr(high, low, close, 14)

    # ── 20 日年化波动率（对数收益）──
    logret = np.log(close / close.shift(1)).dropna()
    if len(logret) >= 20:
        out["volatility_20d"] = float(logret.tail(20).std(ddof=1) * np.sqrt(252))
    else:
        out["volatility_20d"] = None

    # ── 量价：OBV 能量潮 + 近 20 日量价背离 ──
    if "Volume" in df.columns:
        vol = df["Volume"].astype(float)
        d = np.sign(close.diff()).fillna(0.0)
        obv_series = (d * vol).cumsum()
        out["obv"] = _latest(obv_series)
        p20 = close.tail(20)
        o20 = obv_series.tail(20)
        if len(p20) >= 20 and len(o20) >= 20 and float(p20.iloc[0]) != 0:
            price_up = float(p20.iloc[-1]) > float(p20.iloc[0])
            obv_up = float(o20.iloc[-1]) > float(o20.iloc[0])
            if price_up and not obv_up:
                out["divergence"] = "bearish"   # 价升量不跟（顶背离，警惕）
            elif not price_up and obv_up:
                out["divergence"] = "bullish"   # 价跌量能背离（底背离，关注）
            else:
                out["divergence"] = "none"
        else:
            out["divergence"] = None
    else:
        out["obv"] = None
        out["divergence"] = None

    # ── 52 周高低位置（滚动 252 交易日）──
    win = close.tail(252)
    hi = high.tail(252)
    lo = low.tail(252)
    if len(win) > 0 and hi.max() > 0:
        c = out["close"]
        out["high_52w"] = float(hi.max())
        out["low_52w"] = float(lo.min())
        span = hi.max() - lo.min()
        if span > 0 and c is not None:
            pos = (c - lo.min()) / span
            out["pos_52w"] = float(min(pos, 1.2))  # 创新高允许轻微超 1
        else:
            out["pos_52w"] = None
    else:
        out["high_52w"] = out["low_52w"] = out["pos_52w"] = None

    # ── 支撑 / 压力位 ──
    out["support_20"] = _latest(low.rolling(20).min())
    out["resistance_20"] = _latest(high.rolling(20).max())

    return out


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    """RSI（Wilder 平滑）。数据不足返回 None。"""
    delta = close.diff()
    if len(delta) <= period:
        return None
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    latest_gain, latest_loss = avg_gain.iloc[-1], avg_loss.iloc[-1]
    if latest_loss == 0:
        return 100.0 if latest_gain > 0 else 50.0
    rs = latest_gain / latest_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float | None:
    """ATR（Wilder 平滑）。数据不足返回 None。"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    if len(tr) <= period:
        return None
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    v = atr.iloc[-1]
    return None if (v is None or np.isnan(v)) else float(v)
