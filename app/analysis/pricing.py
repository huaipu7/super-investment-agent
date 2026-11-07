"""行情时点声明：产物里那个现价，到底是哪一天的收盘价。

`financial_analysis.current_price` 只是一个数字。它是 2026-11-04 的收盘还是今天的
实时价，产物里原本无处可查——`AAPL/20261105T002222.json` 生成于 11-05 00:22，
现价却是 11-04 的收盘，读者只能去翻 `price_history.dates[-1]` 反推。

这里把「现价对应哪个交易日」落成产物顶层的一个块，供台账、看板与报告共用一份口径。

三条约定
--------
1. **零数值叶子**：只放字符串与布尔，`ledger._walk` 不会把它变成事实，
   `fact_total` 一个不变、不新增 UNKNOWN。
2. **口径与 `financial.py` 取 `current_price` 逐字对齐**：按收盘价 `dropna` 后取
   最后一行，而不是简单取 `index[-1]`——当日 K 线可能整行缺失（未成交）或只有日期。
3. **不猜源**：vintage 缺失时 `source=""`、`is_point_in_time=false`，
   note 里明写「行情源未标注」，绝不填一个看起来合理的源名。
"""
from __future__ import annotations

import pandas as pd

# 与 financial.py 取 current_price 的同一列选择规则
_DEFAULT_CLOSE_COL = "Close"

_NOTE = "现价取该交易日收盘；当日 K 线若整行缺失（未成交）已剔除"
_NO_PRICE_NOTE = "无有效收盘价（无可用 K 线）"


def _as_date(v) -> str:
    return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)


def _last_close_date(price_data) -> str | None:
    """最后一个**有效收盘**所在行的日期；无有效收盘返回 None。"""
    if price_data is None or getattr(price_data, "empty", True):
        return None
    close_col = (_DEFAULT_CLOSE_COL if _DEFAULT_CLOSE_COL in price_data.columns
                 else price_data.columns[0])
    closes = pd.to_numeric(price_data[close_col], errors="coerce").dropna()
    if closes.empty:
        return None
    return _as_date(closes.index[-1])


def build_pricing(price_data) -> dict:
    """DataFrame → 顶层 `pricing` 块。

    Returns:
        {"price_as_of": "2026-11-04" | None,
         "source": "yfinance" | "",
         "is_point_in_time": bool,
         "requested_as_of": str | None,
         "note": str}
    """
    price_as_of = _last_close_date(price_data)
    if price_as_of is None:
        return {
            "price_as_of": None,
            "source": "",
            "is_point_in_time": False,
            "requested_as_of": None,
            "note": _NO_PRICE_NOTE,
        }

    vintage = getattr(price_data, "attrs", None) or {}
    vintage = vintage.get("vintage") if isinstance(vintage, dict) else None
    if not isinstance(vintage, dict) or not vintage:
        return {
            "price_as_of": price_as_of,
            "source": "",
            "is_point_in_time": False,
            "requested_as_of": None,
            "note": f"{_NOTE}；行情源未标注",
        }

    return {
        "price_as_of": price_as_of,
        "source": vintage.get("source") or "",
        "is_point_in_time": bool(vintage.get("is_point_in_time", False)),
        "requested_as_of": vintage.get("requested_as_of"),
        "note": _NOTE,
    }
