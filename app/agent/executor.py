"""执行车间 - 工具注册与执行（迁移自 workshops/executor.py）。

与原版的差异：不再直接抓取 yfinance/AKShare，而是统一接入
app.data.router.MarketData（A股→Sina/东财，美股/港股→yfinance）。
A 股数据经形状适配器转换为 Violetz 原契约（get_quote / get_financials /
get_ticker_info 的字段），使上层表达车间与评分引擎无需改动即可工作。
"""
from __future__ import annotations

import pandas as pd

from app.data.cache import get_cache, CACHE_TTL
from app.data.router import get_market_data
from app.data.sources.akshare_source import normalize_ashare_code


class ExecutorError(Exception):
    """执行车间异常基类"""
    pass


class TickerNotFoundError(ExecutorError):
    """股票代码未找到"""
    pass


class NoDataError(ExecutorError):
    """数据为空"""
    pass


# ── A 股公司信息（名称/总股本）缓存兜底 ──────────────────────────
# 东财 push2 公司信息接口在本网络不可用，总股本改由东财报表推导；
# 结果 SQLite 缓存 1 小时，避免重复抓资产负债表。
def _ashare_company_info(ticker: str) -> dict:
    cache = get_cache()
    key = normalize_ashare_code(ticker)
    cached = cache.get_dict("ashare_cinfo", key)
    if cached:
        return cached
    result = {"name": key, "shares": None}
    try:
        info = get_market_data().fetch_company_info(key)
        result["name"] = info.get("name") or key
        result["shares"] = info.get("shares_outstanding")
    except Exception:
        pass
    cache.set_dict(result, CACHE_TTL["info"], "ashare_cinfo", key)
    return result


def _ashare_market_cap(ticker: str, price) -> float | None:
    """A 股市值兜底：总股本 × 现价（股本由东财报表真实推导）。"""
    if not price:
        return None
    info = _ashare_company_info(ticker)
    shares = info.get("shares")
    if shares and price:
        try:
            return round(float(shares) * float(price), 2)
        except (TypeError, ValueError):
            return None
    return None


# ── 形状适配器（A 股数据 → Violetz 原契约） ─────────────────────

def _normalize_quote(quote: dict, ticker: str) -> dict:
    """将 A 股 Sina spot 转换为 yfinance 形状（美股/港股直接返回）。"""
    if "current_price" in quote:
        return quote
    price = quote.get("price")
    result = {
        "ticker": ticker.upper(),
        "company_name": quote.get("name") or ticker.upper(),
        "currency": "CNY",
        "current_price": price,
        "previous_close": quote.get("pre_close"),
        "open": quote.get("open"),
        "day_high": quote.get("high"),
        "day_low": quote.get("low"),
        "change": quote.get("change_amount"),
        "change_percent": quote.get("change_pct"),
        "volume": quote.get("volume"),
        "market_cap": quote.get("market_cap"),
        "pe_ratio": quote.get("pe"),
        "turnover": quote.get("turnover"),
        "amplitude": quote.get("amplitude"),
    }
    if result.get("market_cap") is None and price:
        result["market_cap"] = _ashare_market_cap(ticker, price)
    vintage = quote.get("vintage")
    if vintage:
        result["vintage"] = vintage
    return {k: v for k, v in result.items() if v is not None}


def _normalize_financials(fin: dict, ticker: str) -> dict:
    """将 A 股 akshare 指标转换为 yfinance 形状（供 VioletzScorer / 财务报告）。

    换算均为真实计算：ROE/净利率/营收增速 除以 100 转小数；
    负债权益比 = 资产负债率/(1-资产负债率)；PE/PB = 现价/EPS、现价/每股净资产。
    """
    if not any(k in fin for k in ("roe", "debt_ratio", "net_margin", "nav_per_share", "eps_cash_flow")):
        return fin  # 已是 yfinance 形状（美股/港股）

    info = _ashare_company_info(ticker)
    result = {
        "ticker": ticker.upper(),
        "company_name": info["name"],
        "currency": "CNY",
    }

    roe = fin.get("roe")
    if roe is not None:
        result["return_on_equity"] = round(roe / 100.0, 4)
    nm = fin.get("net_margin")
    if nm is not None:
        result["profit_margin"] = round(nm / 100.0, 4)
    rg = fin.get("revenue_growth")
    if rg is not None:
        result["revenue_growth"] = round(rg / 100.0, 4)
    eps = fin.get("eps")
    if eps is not None:
        result["eps"] = eps
    debt = fin.get("debt_ratio")
    if debt is not None and 0 <= debt < 100:
        result["debt_to_equity"] = round(debt / (100 - debt), 4)

    # PE/PB/市值（需要现价，Sina 单字段接口，轻量且缓存）
    price = None
    try:
        sq = get_market_data().sina.get_sina_quote(ticker)
        if sq and sq.get("price"):
            price = float(sq["price"])
    except Exception:
        pass
    if price:
        result["current_price"] = price
        if eps and eps > 0:
            result["pe_ratio"] = round(price / eps, 2)
        nav = fin.get("nav_per_share")
        if nav and nav > 0:
            result["price_to_book"] = round(price / nav, 2)
        mcap = _ashare_market_cap(ticker, price)
        if mcap:
            result["market_cap"] = mcap

    vintage = fin.get("vintage")
    if vintage:
        result["vintage"] = vintage
    return {k: v for k, v in result.items() if v is not None}


def _normalize_info(info: dict, ticker: str) -> dict:
    """将 A 股东财 info 转换为 yfinance 形状（美股/港股直接返回）。"""
    if "summary" in info or "longBusinessSummary" in info or "description" in info:
        result = dict(info)
        result.setdefault("ticker", ticker.upper())
        result.setdefault("company_name", info.get("name") or ticker.upper())
        return {k: v for k, v in result.items() if v is not None}
    result = {
        "ticker": ticker.upper(),
        "company_name": info.get("name") or ticker.upper(),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "currency": info.get("currency"),
        "country": info.get("country"),
        "website": info.get("website"),
        "employees": info.get("employees"),
        "description": info.get("summary"),
    }
    return {k: v for k, v in result.items() if v is not None}


# ── 工具实现 ────────────────────────────────────────────────────

def search_tickers(query: str, top_k: int = 5) -> list[dict]:
    """搜索股票代码

    Returns:
        [{"ticker": str, "name": str, "exchange": str}, ...]
    """
    try:
        return get_market_data().search_tickers(query, top_k)
    except Exception:
        return []


def validate_ticker(ticker: str) -> bool:
    """验证股票代码是否有效"""
    try:
        return get_market_data().validate_ticker(ticker)
    except Exception:
        return False


def get_quote(ticker: str, as_of=None) -> dict:
    """获取股票行情（经统一数据层，含数据源兜底）。as_of 时取 ≤as_of 最后一根真实历史 bar。"""
    try:
        quote = get_market_data().get_quote(ticker, as_of=as_of)
    except Exception as e:
        raise ExecutorError(f"无法获取 {ticker} 的数据: {e}")
    if not quote:
        raise TickerNotFoundError(f"无法获取 {ticker} 的数据")
    price = quote.get("current_price") or quote.get("price")
    if price is None:
        raise TickerNotFoundError(f"无法获取 {ticker} 的数据")
    return _normalize_quote(quote, ticker)


def get_history(ticker: str, period: str = "1mo", as_of=None) -> pd.DataFrame:
    """获取历史行情数据（period: 1d/5d/1mo/3mo/6mo/ytd/1y/5y/max）。

    as_of 提供时锚定序列终点（≤as_of），返回的 DataFrame 附带 vintage 于 attrs。
    """
    try:
        df = get_market_data().get_history(ticker, period, as_of=as_of)
    except Exception as e:
        raise NoDataError(f"未获取到 {ticker} 在 period={period} as_of={as_of} 范围内的数据: {e}")
    if df is None or df.empty:
        raise NoDataError(f"未获取到 {ticker} 在 period={period} as_of={as_of} 范围内的数据")
    # 时区统一处理
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    if as_of:
        df = df.loc[df.index <= pd.to_datetime(as_of)]
    return df


def get_financials(ticker: str, as_of=None) -> dict:
    """获取关键财务指标概览（A 股经 akshare 指标适配，其他走 yfinance）。

    as_of 仅作查询锚点：免费源无法回退历史财务快照，返回最新一期并显式标注
    is_point_in_time=False（vintage.note 说明）。
    """
    try:
        fin = get_market_data().get_financials(ticker, as_of=as_of)
    except Exception as e:
        raise ExecutorError(f"无法获取 {ticker} 财务数据: {e}")
    if not fin:
        raise ExecutorError(f"无法获取 {ticker} 财务数据（数据源返回空）")
    return _normalize_financials(fin, ticker)


def get_ticker_info(ticker: str) -> dict:
    """获取股票综合信息"""
    try:
        info = get_market_data().fetch_company_info(ticker)
    except Exception as e:
        raise ExecutorError(f"无法获取 {ticker} 综合信息: {e}")
    return _normalize_info(info, ticker)


def get_news_sentiment(ticker: str, max_articles: int = 5) -> dict:
    """获取股票新闻情绪分析

    委托 app.agent.sentiment 完成新闻获取和评分，在此做缓存包装。
    """
    from app.agent.sentiment import get_news_sentiment as _sentiment_analysis

    cache = get_cache()
    cache_key = f"{ticker.upper()}_{max_articles}"
    cached = cache.get_dict("sentiment", cache_key)
    if cached is not None:
        return cached

    result = _sentiment_analysis(ticker, max_articles)
    cache.set_dict(result, CACHE_TTL["quote"], "sentiment", cache_key)
    return result
