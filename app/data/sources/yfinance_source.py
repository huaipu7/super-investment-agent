"""
Yahoo Finance 数据源 —— 全球市场。

合并迁移：
  - Research-Agent(backend/data_ingestion/yahoo_finance.py)：财务报表标准化、公司信息、竞品
  - Agent-for-Investment(workshops/executor.py)：实时报价、历史行情、财务概览、搜索、校验
缓存统一走 SQLite（替换原 Redis）。
"""
from __future__ import annotations

import datetime
import time
import urllib.request
from typing import Any, Callable

import pandas as pd
import yfinance as yf
from yfinance.config import YfConfig

from app.data.cache import get_cache, CACHE_TTL
from app.data.cleaning.normalizer import normalize_metric_name
from app.data.sources.base import DataSource

YFINANCE_RETRY_TIMES = 3
YFINANCE_RETRY_DELAY = 2.0


def _setup_yahoo_proxy() -> None:
    """把 Windows 系统代理传给 yfinance 的 curl_cffi 网络栈（需 dict 格式）。

    背景：本机启用 VPN/科学上网后，系统代理为 http://127.0.0.1:<port>。
    yfinance 1.3.0 基于 curl_cffi，它不读 WinINET 系统代理；若直接联网，
    出口 IP 可能被 Yahoo 403/429 拦截。此处读取系统代理并以 dict 形式注入
    YfConfig.network.proxy，使 curl_cffi 走同一代理通道。
    """
    if YfConfig.network.proxy:
        # 已显式配置（.env/代码）则保持不动
        if isinstance(YfConfig.network.proxy, str):
            YfConfig.network.proxy = {"http": YfConfig.network.proxy, "https": YfConfig.network.proxy}
        return
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}
    http = proxies.get("http") or proxies.get("https")
    if http:
        YfConfig.network.proxy = {"http": http, "https": http}
    else:
        YfConfig.network.proxy = {}


_setup_yahoo_proxy()


class YFinanceError(RuntimeError):
    """yfinance 数据源异常。"""


class TickerNotFoundError(YFinanceError):
    """股票代码未找到。"""


class NoDataError(YFinanceError):
    """数据为空。"""


def _retry(func: Callable, *args, **kwargs) -> Any:
    """带重试机制的调用包装（沿用 Violetz executor._retry）。"""
    last_error = None
    for attempt in range(YFINANCE_RETRY_TIMES):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt < YFINANCE_RETRY_TIMES - 1:
                time.sleep(YFINANCE_RETRY_DELAY)
    raise YFinanceError(f"数据获取失败（已重试{YFINANCE_RETRY_TIMES}次）: {last_error}")


class YFinanceSource(DataSource):
    """基于 yfinance 的全球市场数据接入。"""

    def __init__(self):
        self._ticker_cache: dict[str, yf.Ticker] = {}
        self.cache = get_cache()

    def _get_ticker(self, ticker: str) -> yf.Ticker:
        if ticker not in self._ticker_cache:
            self._ticker_cache[ticker] = yf.Ticker(ticker)
        return self._ticker_cache[ticker]

    # ── 财务报表（Research-Agent 迁移）────────────────────────

    def _fetch_and_normalize(self, ticker: str, period: str, attr: str) -> pd.DataFrame:
        t = self._get_ticker(ticker)
        if period == "quarterly":
            df = getattr(t, f"quarterly_{attr}", None)
        else:
            df = getattr(t, attr, None)

        if df is None or df.empty:
            return pd.DataFrame()

        raw = df.copy()
        mapped_index = {}
        for name in raw.index:
            code = normalize_metric_name(str(name), source="yfinance")
            if code is not None:
                mapped_index[name] = code
        raw = raw.rename(index=mapped_index)
        raw = raw[~raw.index.duplicated(keep="first")]
        out = raw.T
        out.index = pd.to_datetime(out.index)
        out = out.sort_index()
        return out.apply(pd.to_numeric, errors="coerce")

    def fetch_income_statements(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        return self._fetch_and_normalize(ticker, period, "financials")

    def fetch_balance_sheets(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        return self._fetch_and_normalize(ticker, period, "balance_sheet")

    def fetch_cash_flow_statements(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        return self._fetch_and_normalize(ticker, period, "cashflow")

    def fetch_price_history(
        self, ticker: str, start: str = "", end: str = ""
    ) -> pd.DataFrame:
        t = self._get_ticker(ticker)
        kwargs: dict = {}
        if start:
            kwargs["start"] = start
        if end:
            kwargs["end"] = end
        hist = t.history(period="max" if not start else None, **kwargs)
        if hist.empty:
            return pd.DataFrame()
        keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in hist.columns]
        return hist[keep]

    def fetch_company_info(self, ticker: str) -> dict:
        t = self._get_ticker(ticker)
        info = t.info or {}
        return {
            "name": info.get("longName") or info.get("shortName", ""),
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "market_cap": info.get("marketCap"),
            "currency": info.get("currency", "USD"),
            "country": info.get("country", ""),
            "summary": info.get("longBusinessSummary", ""),
            "employees": info.get("fullTimeEmployees"),
            "website": info.get("website", ""),
            "shares_outstanding": info.get("sharesOutstanding") or info.get("impliedSharesOutstanding"),
            "fiscal_year_end": info.get("fiscalYearEnd"),
        }

    def search_competitors(self, ticker: str) -> list[str]:
        """通过 yfinance 行业信息占位搜索（原逻辑：返回空，由 LLM/行业分类确定）。"""
        info = self.fetch_company_info(ticker)
        if not info.get("industry"):
            return []
        return []

    # ── 行情 / 财务 / 搜索（Violetz 迁移 + SQLite 缓存）────────

    def get_quote(self, ticker: str) -> dict:
        """获取股票实时报价（带 5 分钟缓存）。"""
        cache = self.cache
        cached = cache.get_dict("quote", ticker.upper())
        if cached is not None:
            return cached

        stock = _retry(yf.Ticker, ticker)
        try:
            info = _retry(lambda: stock.info)
        except Exception:
            info = {}

        if not info or info.get("regularMarketPrice") is None:
            try:
                fast_info = _retry(lambda: stock.fast_info)
                price = fast_info.get("lastPrice")
            except Exception:
                price = None
            if price is None:
                suggestions = self.search_tickers(ticker)
                hint = ""
                if suggestions:
                    hint = f"，您是不是在找: {', '.join(s['ticker'] for s in suggestions[:3])}"
                raise TickerNotFoundError(f"无法获取 {ticker} 的数据{hint}")

        try:
            fast_info = _retry(lambda: stock.fast_info)
        except Exception:
            fast_info = {}

        result = {
            "ticker": ticker.upper(),
            "company_name": info.get("longName") or info.get("shortName") or ticker.upper(),
            "currency": info.get("currency") or "USD",
            "current_price": fast_info.get("lastPrice") or info.get("currentPrice")
            or info.get("regularMarketPrice"),
            "previous_close": fast_info.get("previousClose") or info.get("previousClose")
            or info.get("regularMarketPreviousClose"),
            "open": info.get("open") or info.get("regularMarketOpen"),
            "day_high": info.get("dayHigh") or info.get("regularMarketDayHigh"),
            "day_low": info.get("dayLow") or info.get("regularMarketDayLow"),
            "change": info.get("regularMarketChange"),
            "change_percent": info.get("regularMarketChangePercent"),
            "volume": info.get("volume") or info.get("regularMarketVolume"),
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE") or info.get("forwardPE"),
            "dividend_yield": info.get("dividendYield"),
            "52_week_high": info.get("fiftyTwoWeekHigh"),
            "52_week_low": info.get("fiftyTwoWeekLow"),
        }
        result = {k: v for k, v in result.items() if v is not None}
        cache.set_dict(result, CACHE_TTL["quote"], "quote", ticker.upper())
        return result

    def get_history(self, ticker: str, period: str = "1mo") -> pd.DataFrame:
        """获取历史行情数据（period: 1d/5d/1mo/3mo/6mo/1y/2y/5y/10y/ytd/max）。"""
        cache = self.cache
        cached = cache.get_dataframe("history", ticker.upper(), period)
        if cached is not None:
            return cached

        stock = _retry(yf.Ticker, ticker)
        df = _retry(lambda: stock.history(period=period))

        if df.empty:
            raise NoDataError(f"未获取到 {ticker} 在 period={period} 范围内的数据")

        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        ttl = CACHE_TTL["history_short"] if period in ("1d", "5d") else CACHE_TTL["history_long"]
        cache.set_dataframe(df, ttl, "history", ticker.upper(), period)
        return df

    def get_financials(self, ticker: str) -> dict:
        """获取关键财务指标概览（带 1 小时缓存）。"""
        cache = self.cache
        cached = cache.get_dict("financials", ticker.upper())
        if cached is not None:
            return cached

        stock = _retry(yf.Ticker, ticker)
        info = _retry(lambda: stock.info)
        if not info:
            raise YFinanceError(f"无法获取 {ticker} 财务数据（Yahoo 数据源返回空，可能被限流）")

        result = {k: v for k, v in {
            "ticker": ticker.upper(),
            "company_name": info.get("longName") or ticker.upper(),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "market_cap": info.get("marketCap"),
            "enterprise_value": info.get("enterpriseValue"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "peg_ratio": info.get("pegRatio"),
            "price_to_book": info.get("priceToBook"),
            "dividend_yield": info.get("dividendYield"),
            "revenue": info.get("totalRevenue"),
            "revenue_growth": info.get("revenueGrowth"),
            "profit_margin": info.get("profitMargins"),
            "eps": info.get("trailingEps"),
            "debt_to_equity": info.get("debtToEquity"),
            "return_on_equity": info.get("returnOnEquity"),
            "free_cash_flow": info.get("freeCashflow"),
        }.items() if v is not None}
        cache.set_dict(result, CACHE_TTL["financials"], "financials", ticker.upper())
        return result

    def get_ticker_info(self, ticker: str) -> dict:
        """获取股票综合信息（带 1 小时缓存）。"""
        cache = self.cache
        cached = cache.get_dict("info", ticker.upper())
        if cached is not None:
            return cached

        stock = _retry(yf.Ticker, ticker)
        info = _retry(lambda: stock.info)
        if not info:
            raise YFinanceError(f"无法获取 {ticker} 综合信息（Yahoo 数据源返回空，可能被限流）")

        result = {k: v for k, v in {
            "ticker": ticker.upper(),
            "company_name": info.get("longName") or ticker.upper(),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
            "website": info.get("website"),
            "employees": info.get("fullTimeEmployees"),
            "description": info.get("longBusinessSummary"),
        }.items() if v is not None}
        cache.set_dict(result, CACHE_TTL["info"], "info", ticker.upper())
        return result

    def fetch_news(self, ticker: str, limit: int = 20) -> list[dict]:
        """美股最新新闻（yfinance Ticker.news）。

        item 结构形如 {"content": {"title","summary","provider":{...},"pubDate"},
        "providerPublishTime": unix_ts, ...}；字段解析保持与 sentiment 模块一致的容错写法。
        统一 item schema: {title, summary, published, source, url}
        """
        stock = self._get_ticker(ticker)
        try:
            raw = stock.news
        except Exception:
            return []
        if not raw:
            return []

        items: list[dict] = []
        for item in raw[:limit]:
            content = item.get("content") or item
            if not isinstance(content, dict):
                continue
            title = content.get("title") or ""
            if not title:
                continue
            published = content.get("pubDate") or item.get("pubDate") or ""
            ts = item.get("providerPublishTime")
            if not published and ts:
                try:
                    published = datetime.datetime.fromtimestamp(int(ts)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                except (ValueError, TypeError, OSError):
                    published = ""
            provider = content.get("provider") or {}
            canonical = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
            items.append(
                {
                    "title": title,
                    "summary": (content.get("summary") or content.get("description") or "")[
                        :300
                    ],
                    "published": str(published),
                    "source": provider.get("displayName") or provider.get("name") or "",
                    "url": canonical.get("url") or "",
                }
            )
        return items

    @staticmethod
    def _fmt_date(v) -> str:
        """安全格式化日期（NaT/None → 空串）。"""
        if v is None or (hasattr(pd, "isna") and pd.isna(v)):
            return ""
        try:
            return pd.to_datetime(v).strftime("%Y-%m-%d")
        except Exception:
            return str(v)

    def fetch_meetings(self, ticker: str, limit: int = 5) -> list[dict]:
        """美股财报/分红日历（yfinance 免费字段）→ 与 A 股股东大会同构记录。

        诚实边界：yfinance 仅披露财报披露日 / 除息日 / 分红日等日期，不含股东
        大会提案，因此 proposals 留空并在 name/source_note 如实说明「仅披露日期」。
        """
        stock = self._get_ticker(ticker)
        cal: dict = {}
        try:
            raw = stock.calendar
            if isinstance(raw, dict):
                cal = raw
            elif hasattr(raw, "to_dict"):
                d = raw.to_dict()
                flat: dict = {}
                for k, v in d.items():
                    if isinstance(v, dict):
                        for kk, vv in v.items():
                            flat.setdefault(kk, []).append(vv)
                    else:
                        flat[k] = v
                cal = flat
        except Exception:
            pass

        earnings_dates: list[str] = []
        ed = cal.get("Earnings Date") or cal.get("Earnings Date Start") or []
        if isinstance(ed, (str, int, float)):
            ed = [ed]
        for d in ed:
            ds = self._fmt_date(d)
            if ds:
                earnings_dates.append(ds)
        if not earnings_dates:
            try:
                edf = stock.get_earnings_dates(limit=max(limit, 8))
                if edf is not None and not edf.empty:
                    earnings_dates = [self._fmt_date(d) for d in edf.index]
            except Exception:
                pass

        ex_div = self._fmt_date(cal.get("Ex-Dividend Date"))
        div = self._fmt_date(cal.get("Dividend Date"))
        note = "yfinance 财报/分红日历（仅披露日期，无股东大会提案）"
        out = []
        for ds in earnings_dates[:limit]:
            out.append({
                "name": "财报/分红日历（yfinance）",
                "start_date": ds,
                "record_date": ex_div,      # 除息日
                "resolution_date": div,     # 分红日
                "announcement_date": "",
                "proposals": "",
                "source_note": note,
            })
        return out

    def search_tickers(self, query: str, top_k: int = 5) -> list[dict]:
        """按关键词搜索股票代码。"""
        try:
            results = yf.Search(query)
            quotes = getattr(results, "quotes", []) or []
            return [
                {
                    "ticker": q.get("symbol"),
                    "name": q.get("longname") or q.get("shortname"),
                    "exchange": q.get("exchange"),
                }
                for q in quotes[:top_k]
                if q.get("symbol")
            ]
        except Exception:
            return []

    def validate_ticker(self, ticker: str) -> bool:
        """验证股票代码是否有效。"""
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            return bool(info and info.get("regularMarketPrice") is not None)
        except Exception:
            return False
