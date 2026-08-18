"""
Sina 行情数据源 —— A 股实时行情（低延迟）。

迁移自 Violet Z(app.py) 的 get_sina_quote / api_stock_spot 逻辑。
修复了原项目 get_hist_data 中硬编码日期范围(20260401-20260430)的问题，
改用 akshare 最近日线取最后一根（含换手率）。
"""
from __future__ import annotations

import urllib.request

from app.data.cache import get_cache, CACHE_TTL
from app.data.sources.akshare_source import (
    normalize_ashare_code,
    detect_ashare_exchange,
    AKShareSource,
)


class SinaSource:
    """基于 Sina HTTP API 的 A 股实时行情。"""

    def __init__(self):
        self.cache = get_cache()
        self._ak = AKShareSource()

    def get_sina_quote(self, code: str) -> dict | None:
        """直接调用 Sina HTTP API 获取实时行情。"""
        cache = self.cache
        key = "sina_quote"
        cached = cache.get_dict(key, code)
        if cached is not None:
            return cached

        prefix = detect_ashare_exchange(code)
        if not prefix:
            return None
        symbol = normalize_ashare_code(code)
        url = f"http://hq.sinajs.cn/list={prefix}{symbol}"
        headers = {
            "Referer": "http://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            text = resp.read().decode("gbk")
        except Exception:
            return None

        fields = text.split('"')[1].split(",")
        if not fields or len(fields) < 32:
            return None

        result = {
            "name": fields[0],
            "open": float(fields[1]) if fields[1] else 0,
            "pre_close": float(fields[2]) if fields[2] else 0,
            "price": float(fields[3]) if fields[3] else 0,
            "high": float(fields[4]) if fields[4] else 0,
            "low": float(fields[5]) if fields[5] else 0,
            "volume": int(fields[8]) if fields[8] else 0,    # 股
            "amount": float(fields[9]) if fields[9] else 0,  # 成交额
            "date": fields[30],
            "time": fields[31],
        }
        if result["price"] <= 0:
            # 最新价为空/0：视为非有效快照（停牌/瞬时空数据），不缓存假值，交上层兜底
            return None
        cache.set_dict(result, CACHE_TTL["sina_quote"], key, code)
        return result

    def get_us_quote(self, ticker: str) -> dict | None:
        """Sina 美股实时行情（gb_<ticker> 端点），yfinance 不可用时兜底。

        字段映射已对照 stock_us_daily 逐项核实：
          [1]最新价 [5]昨收 [6]最高 [7]最低 [8]52周高 [9]52周低 [10]成交量 [12]市值 [14]PE
        涨跌额/涨跌幅由价格与昨收计算，保证内部一致。
        """
        cache = self.cache
        key = "sina_us_quote"
        cached = cache.get_dict(key, ticker.upper())
        if cached is not None:
            return cached

        symbol = ticker.strip().lower()
        url = f"http://hq.sinajs.cn/list=gb_{symbol}"
        headers = {
            "Referer": "http://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            text = resp.read().decode("gbk")
        except Exception:
            return None

        fields = text.split('"')[1].split(",")
        if not fields or len(fields) < 15:
            return None

        def _num(i, default=None):
            try:
                return float(fields[i])
            except (ValueError, IndexError, TypeError):
                return default

        price = _num(1)
        prev_close = _num(5)
        if price is None or price <= 0:
            return None

        result = {
            "ticker": ticker.upper(),
            "company_name": fields[0] if fields[0] else ticker.upper(),
            "currency": "USD",
            "current_price": price,
            "previous_close": prev_close,
            "day_high": _num(6),
            "day_low": _num(7),
            "52_week_high": _num(8),
            "52_week_low": _num(9),
            "volume": int(_num(10, 0)),
            "market_cap": _num(12),
            "pe_ratio": _num(14),
            "update_time": fields[3] if len(fields) > 3 else None,
        }
        if prev_close:
            result["change"] = round(price - prev_close, 4)
            result["change_percent"] = round((price - prev_close) / prev_close * 100, 4)
        result = {k: v for k, v in result.items() if v is not None}
        cache.set_dict(result, CACHE_TTL["quote"], key, ticker.upper())
        return result

    def get_spot(self, code: str) -> dict | None:
        """综合行情快照：实时价 + 涨跌幅/振幅 + 换手率 + PE/PB/市值（Violet Z 逻辑）。"""
        quote = self.get_sina_quote(code)
        if not quote:
            return None

        price = quote["price"]
        pre_close = quote["pre_close"]
        high = quote["high"]
        low = quote["low"]
        amplitude = round((high - low) / pre_close * 100, 2) if pre_close > 0 else 0
        change_pct = (price - pre_close) / pre_close * 100 if pre_close > 0 else 0

        hist = self._ak.get_recent_daily(code)
        turnover = None
        if hist:
            try:
                turnover = float(hist.get("换手率"))
            except (TypeError, ValueError):
                turnover = None

        fin = self._ak.get_financial_indicators(code)
        eps = fin.get("eps")
        nav = fin.get("nav_per_share")

        pe = round(price / eps, 2) if eps and eps > 0 else None
        pb = round(price / nav, 2) if nav and nav > 0 else None

        market_cap = None
        if hist and turnover and turnover > 0:
            vol_lots = hist.get("成交量")
            if vol_lots:
                try:
                    vol_lots = float(vol_lots)
                    total_shares = round(vol_lots * 100 / (turnover / 100))
                    market_cap = round(total_shares * price, 2)
                except (TypeError, ValueError):
                    pass

        return {
            "code": normalize_ashare_code(code),
            "name": quote["name"],
            "price": price,
            "change_pct": round(change_pct, 2),
            "change_amount": round(price - pre_close, 2),
            "volume": quote["volume"],
            "amount": quote["amount"],
            "high": high,
            "low": low,
            "open": quote["open"],
            "pre_close": pre_close,
            "pe": pe,
            "pb": pb,
            "market_cap": market_cap,
            "turnover": turnover,
            "amplitude": amplitude,
        }
