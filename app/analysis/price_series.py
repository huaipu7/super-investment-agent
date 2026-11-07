"""行情序列 → 产物里的列式 `price_history`。

全量管线与增量刷新都要产出同一份结构，抽出来共用，避免两处各写一遍慢慢跑偏。

当日 K 线可能整行 NaN（未成交 / 源只给了日期），必须剔除，否则 NaN 会进产物和台账；
取最近 300 个交易日（报告 K 线图与均线叠加图的需要）。
"""
from __future__ import annotations

import pandas as pd

from app.utils.logger import logger

_OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def _as_date(v) -> str:
    return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)


def build_price_history(price_data) -> dict:
    """DataFrame → {"available": True, dates/open/high/low/close/volume}。

    数据不足或解析失败时返回 `available=False` 并写明原因，不抛异常、不产出空壳。
    """
    if price_data is None or getattr(price_data, "empty", True):
        return {"available": False, "reason": "无历史行情数据"}
    try:
        tail = (price_data[_OHLCV]
                .apply(pd.to_numeric, errors="coerce")
                .dropna()
                .tail(300))
        if tail.empty:
            return {"available": False, "reason": "行情全为缺失值"}
        return {
            "available": True,
            "dates": [_as_date(d) for d in tail.index],
            "open": [float(x) for x in tail["Open"]],
            "high": [float(x) for x in tail["High"]],
            "low": [float(x) for x in tail["Low"]],
            "close": [float(x) for x in tail["Close"]],
            "volume": [float(x) for x in tail["Volume"]],
        }
    except Exception as e:
        logger.warning("行情序列序列化失败: %s", e)
        return {"available": False, "reason": f"行情序列序列化失败: {e}"}
