"""
统一数据路由层 —— 上层（分析管线/对话 Agent/看板）只依赖本模块。

按市场路由：
  - A 股（6 位代码或 .SS/.SZ/.BJ 后缀）→ Sina 行情 + AKShare 财务/财报
  - 美股（纯字母代码）→ 东方财富美股 F10 财报，失败回退 yfinance
  - 港股（.HK 后缀）→ yfinance
  - 其他 → yfinance
"""
from __future__ import annotations

import datetime as dt
import re
import time

import pandas as pd

from app.data.cache import get_cache, CACHE_TTL
from app.data.vintage import build_vintage
from app.data.sources.akshare_source import AKShareSource, normalize_ashare_code
from app.data.sources.akshare_us_source import AKShareUSSource
from app.data.sources.fund_source import (
    fund_search,
    fund_search_by_code,
    fund_realtime_nav,
    fund_historical_nav,
    fund_holdings,
    fund_basic_info,
    fund_ratings,
    fund_cumulative_nav_trend,
)
from app.data.sources.sina_source import SinaSource
from app.data.sources.yfinance_source import YFinanceSource
from app.observability.metrics import metrics as _metrics
from app.utils.logger import logger


def _ds_instrument(name: str):
    """数据源健康监控装饰器：记录 ds_<source>_<name> 请求/错误 + 聚合时延。

    source 按 ticker 路由（akshare/yfinance/sina），供 /metrics 数据源健康徽标。
    """
    def deco(fn):
        def wrapper(self, ticker, *a, **k):
            src = _source_label(ticker)
            _metrics.inc(f"ds_{src}_{name}")
            t0 = time.perf_counter()
            try:
                r = fn(self, ticker, *a, **k)
                _metrics.record(f"ds_{src}_latency", time.perf_counter() - t0)
                return r
            except Exception:
                _metrics.inc(f"ds_{src}_{name}_errors")
                _metrics.record(f"ds_{src}_latency", time.perf_counter() - t0)
                raise
        return wrapper
    return deco


# 复用 fund_source 的函数级接口
__all__ = [
    "is_ashare", "is_hk", "MarketData",
    "get_market_data",
    # 基金
    "fund_search", "fund_search_by_code", "fund_realtime_nav",
    "fund_historical_nav", "fund_holdings", "fund_basic_info",
    "fund_ratings", "fund_cumulative_nav_trend",
]

_ASHARE_SUFFIX = re.compile(r"\.(SS|SH|SZ|BJ)$", re.IGNORECASE)


def is_ashare(ticker: str) -> bool:
    """判断是否为 A 股代码：.SS/.SZ/.BJ 后缀，或 6 位纯数字代码。"""
    t = ticker.strip().upper()
    if _ASHARE_SUFFIX.search(t):
        return True
    if re.fullmatch(r"(SH|SZ|BJ)?\d{6}", t):
        return True
    return False


def is_hk(ticker: str) -> bool:
    return ticker.strip().upper().endswith(".HK")


def _parse_asof(v) -> pd.Timestamp | None:
    """解析 as_of 参数 → pd.Timestamp | None（空串/None → None）。"""
    if v is None or str(v).strip() == "":
        return None
    return pd.to_datetime(str(v))


def _source_label(ticker: str) -> str:
    return "akshare" if is_ashare(ticker) else "yfinance"


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_datetime_index(df: pd.DataFrame) -> pd.DataFrame:
    """SQLite 缓存 JSON 往返会把 DatetimeIndex 还原成字符串索引，这里补回来。"""
    if df is not None and not df.empty and not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index)
        except Exception:
            pass
    return df


class MarketData:
    """统一市场数据入口，按 ticker 市场路由到对应数据源。"""

    def __init__(self):
        self._yf = YFinanceSource()
        self._ak = AKShareSource()
        self._sina = SinaSource()
        self._us = AKShareUSSource()

    @property
    def yfinance(self) -> YFinanceSource:
        return self._yf

    @property
    def akshare(self) -> AKShareSource:
        return self._ak

    @property
    def sina(self) -> SinaSource:
        return self._sina

    @property
    def akshare_us(self) -> AKShareUSSource:
        return self._us

    # ── 行情 ──────────────────────────────────────────────────

    @_ds_instrument("quote")
    def get_quote(self, ticker: str, as_of=None) -> dict:
        """实时行情，带数据源兜底；as_of 提供时走历史序列取 ≤as_of 最后一根。

        返回 dict 附带 vintage（数据时点标记）。A 股 → Sina(spot) 失败回退
        yfinance；其他 → yfinance 失败回退 Sina 美股(gb_)。
        """
        if as_of:
            return self._quote_from_history(ticker, _parse_asof(as_of))

        if is_ashare(ticker):
            spot = self._sina.get_spot(ticker)
            if spot is not None:
                quote, source = dict(spot), "sina"
            else:
                quote, source = self._yf.get_quote(ticker), "yfinance"
        else:
            try:
                quote, source = self._yf.get_quote(ticker), "yfinance"
            except Exception:
                us = self._sina.get_us_quote(ticker)
                if us is None:
                    raise
                quote, source = us, "sina"

        quote["vintage"] = build_vintage(
            requested_as_of=None,
            data_observation_date=quote.get("date") or dt.date.today().isoformat(),
            source=source,
            is_point_in_time=True,
            note="实时行情快照（抓取时点）",
        )
        return quote

    def _quote_from_history(self, ticker: str, as_of_dt) -> dict:
        """用历史日线构建 ≤as_of 的行情快照（as_of 前的最后一根真实 bar）。"""
        df = self._history_df(ticker, "max", as_of_dt)
        if df is None or df.empty:
            raise ValueError(f"无法从历史序列构建 {ticker} as_of={as_of_dt.date()} 行情（无数据）")
        src = (df.attrs or {}).get("source") or _source_label(ticker)
        df = _to_datetime_index(df)
        hist = df.loc[df.index <= as_of_dt]
        if hist.empty:
            raise ValueError(f"as_of={as_of_dt.date()} 之前无 {ticker} 历史 bar，无法构建 as_of 行情")
        bar = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) >= 2 else None
        quote = {
            "ticker": ticker.upper(),
            "current_price": _num(bar["Close"]),
            "previous_close": _num(prev["Close"]) if prev is not None else None,
            "open": _num(bar["Open"]),
            "day_high": _num(bar["High"]),
            "day_low": _num(bar["Low"]),
            "volume": int(bar["Volume"]) if "Volume" in hist.columns and pd.notna(bar["Volume"]) else None,
            "date": str(hist.index[-1].date()),
        }
        quote["vintage"] = build_vintage(
            requested_as_of=str(as_of_dt.date()),
            data_observation_date=str(hist.index[-1].date()),
            source=src,
            is_point_in_time=True,
            note="由历史日线取 ≤as_of 最后一根构建（真实点对点，非最新快照改写）",
        )
        return {k: v for k, v in quote.items() if v is not None}

    @_ds_instrument("history")
    def get_history(self, ticker: str, period: str = "1mo", as_of=None) -> pd.DataFrame:
        """历史行情，带数据源兜底。as_of 锚定序列终点（≤as_of），缓存 key 含 as_of。"""
        as_of_dt = _parse_asof(as_of) if as_of else None
        if as_of_dt is not None:
            cache = get_cache()
            ck = ["history", ticker.upper(), period, str(as_of_dt.date())]
            df = cache.get_dataframe(*ck)
            if df is None:
                df = self._history_df(ticker, period, as_of_dt)
                if df is not None and not df.empty:
                    cache.set_dataframe(df, CACHE_TTL["history_long"], *ck)
        else:
            df = self._history_df(ticker, period, None)
        # 兜底源会改写 attrs["source"]，溯源标签要跟实际取数通路一致
        src = ((getattr(df, "attrs", None) or {}).get("source")
               if df is not None else None) or _source_label(ticker)
        df = _to_datetime_index(df)
        if as_of_dt is not None and df is not None and not df.empty:
            df = df.loc[df.index <= as_of_dt]
        if df is not None and not df.empty:
            df.attrs["vintage"] = build_vintage(
                requested_as_of=str(as_of_dt.date()) if as_of_dt is not None else None,
                data_observation_date=str(df.index[-1].date()),
                source=src,
                is_point_in_time=True,
                note=("历史行情序列锚定 ≤as_of（真实点对点）" if as_of_dt is not None
                      else "实时历史序列（无 as_of 锚定）"),
            )
        return df if df is not None else pd.DataFrame()

    @_ds_instrument("benchmark")
    def get_benchmark_history(self, ticker: str, period: str = "5y") -> pd.DataFrame:
        """基准指数历史行情（供 beta/ERP 估计）。A股→沪深300(sh000300)，
        港股→恒指(^HSI)，其他→标普500(^GSPC)。失败返回空 DataFrame。
        """
        try:
            if is_ashare(ticker):
                df = self._ak.fetch_index_history("sh000300", period)
                return df if not df.empty else pd.DataFrame()
            if is_hk(ticker):
                return self._yf.get_history("^HSI", period)
            return self._yf.get_history("^GSPC", period)
        except Exception as e:
            import pandas as pd
            return pd.DataFrame()

    def _history_df(self, ticker: str, period: str, as_of_dt) -> pd.DataFrame:
        """获取历史行情 DataFrame；as_of_dt 非空时序列已截断到 ≤as_of。

        A 股 → akshare(东财，start/end 截断)，失败回退 Sina 日线；
        港股 → yfinance，失败回退东财港股日线（Yahoo 对本机是 IP 级限流，
                Sina 美股日线对 .HK 无效，没有专门分支时整段行情会缺失）；
        其他 → yfinance(start/end 截断)，失败回退 Sina 美股日线。
        """
        if is_ashare(ticker):
            code = normalize_ashare_code(ticker)
            if as_of_dt is not None:
                start = (as_of_dt - dt.timedelta(days=3650)).strftime("%Y-%m-%d")
                df = self._ak.fetch_price_history(code, start=start,
                                                  end=as_of_dt.strftime("%Y-%m-%d"))
                if df.empty:
                    df = self._ak.get_ashare_daily(code, "max")
                    if not df.empty:
                        df = df.loc[df.index <= as_of_dt]
                return df
            df = self._ak.fetch_price_history(code)
            if df.empty:
                df = self._ak.get_ashare_daily(code, period)
            return df if df.empty else _slice_period(df, period)

        if is_hk(ticker):
            try:
                if as_of_dt is not None:
                    start = (as_of_dt - dt.timedelta(days=3650)).strftime("%Y-%m-%d")
                    df = self._yf.fetch_price_history(
                        ticker, start=start, end=as_of_dt.strftime("%Y-%m-%d"))
                else:
                    df = self._yf.get_history(ticker, period)
                if df is not None and not df.empty:
                    return df
            except Exception:
                pass
            df = self._ak.get_hk_history(ticker, period)
            if not df.empty:
                if as_of_dt is not None:
                    df = df.loc[df.index <= as_of_dt]
                # 港股默认标 yfinance，走到这里说明实际是东财数据，溯源得说实话
                df.attrs["source"] = "akshare"
                return df
            raise ValueError(f"无法获取 {ticker} 历史行情"
                             f"（as_of={as_of_dt.date() if as_of_dt else None}）")

        try:
            if as_of_dt is not None:
                start = (as_of_dt - dt.timedelta(days=3650)).strftime("%Y-%m-%d")
                df = self._yf.fetch_price_history(ticker, start=start,
                                                  end=as_of_dt.strftime("%Y-%m-%d"))
                if not df.empty:
                    return df
            else:
                return self._yf.get_history(ticker, period)
        except Exception:
            pass
        df = self._ak.get_us_history(ticker, period)
        if not df.empty:
            if as_of_dt is not None:
                df = df.loc[df.index <= as_of_dt]
            return df
        raise ValueError(f"无法获取 {ticker} 历史行情（as_of={as_of_dt.date() if as_of_dt else None}）")

    # ── 财务 ──────────────────────────────────────────────────

    @_ds_instrument("financials")
    def get_financials(self, ticker: str, as_of=None) -> dict:
        """关键财务指标概览。A 股走财务分析指标，其他走 yfinance。

        诚实边界：免费数据源无法回退历史财务快照。as_of 仅作查询锚点
        （akshare start_year 近似），返回的最新一期统一标注
        is_point_in_time=False + 明确 note，绝不伪造「as_of 当期历史值」。
        """
        as_of_dt = _parse_asof(as_of) if as_of else None
        if is_ashare(ticker):
            # start_year 缺省按当前年往前推 5 年（Phase 4：消除硬编码 2024）
            start_year = (str(as_of_dt.year) if as_of_dt is not None
                          else str(dt.date.today().year - 5))
            fin = self._ak.get_financial_indicators(
                normalize_ashare_code(ticker), start_year=start_year)
        else:
            # 美股优先东财 F10；非美股代码返回空字典，自然回退 yfinance
            fin = self._us.get_financials(ticker)
            if not fin:
                try:
                    fin = self._yf.get_financials(ticker)
                except Exception as e:
                    logger.warning("东财与 yfinance 均未取到 %s 财务概览，如实返回空: %s", ticker, e)
                    fin = {}
        fin = dict(fin)
        fin["vintage"] = build_vintage(
            requested_as_of=str(as_of_dt.date()) if as_of_dt is not None else None,
            data_observation_date=dt.date.today().isoformat(),
            source=_source_label(ticker),
            is_point_in_time=as_of_dt is None,
            note=(f"免费数据源无法回退历史财务快照：返回的是当前最新一期（可能含 restatement），"
                  f"不可视为 {as_of_dt.date()} 当期的历史值" if as_of_dt is not None
                  else "最新一期财务快照（无 as_of）"),
        )
        return fin

    @_ds_instrument("statements")
    def fetch_financial_statements(
        self, ticker: str, period: str = "annual"
    ) -> dict[str, pd.DataFrame]:
        """三张财务报表（标准化指标代码）。A 股走 akshare，其他走 yfinance。

        Phase 4：按年报/季报分级 TTL 缓存（财务区分年报季报）：
        年报 24h、季报 6h（季报累计值随新披露期变动较快）。
        """
        cache = get_cache()
        ck = ["fin_stmt", period, ticker.upper()]
        if cache.get_dict(*ck) == {"__cached__": True}:
            stmts = {}
            for k in ("income", "balance_sheet", "cash_flow"):
                df = cache.get_dataframe(*ck, k)
                stmts[k] = df if df is not None else pd.DataFrame()
            return stmts

        if is_ashare(ticker):
            stmts = self._ak.fetch_all_financials(ticker, period)
        else:
            # 美股优先走东财 F10（国内直连，不受 Yahoo 反爬影响）；
            # 非美股代码（港股等）在此返回全空，自然回退 yfinance。
            stmts = self._us.fetch_all_financials(ticker, period)
            if not any(not df.empty for df in stmts.values()):
                try:
                    stmts = self._yf.fetch_all_financials(ticker, period)
                except Exception as e:
                    logger.warning("东财与 yfinance 均未取到 %s 财报，如实返回空: %s", ticker, e)
                    stmts = {k: pd.DataFrame() for k in ("income", "balance_sheet", "cash_flow")}

        ttl = (CACHE_TTL.get("financials_quarterly")
               if period == "quarterly" else CACHE_TTL.get("financials_annual", 3600))
        if any(not df.empty for df in stmts.values()):
            cache.set_dict({"__cached__": True}, ttl, *ck)
            for k, df in stmts.items():
                if not df.empty:
                    cache.set_dataframe(df, ttl, *ck, k)
        return stmts

    @_ds_instrument("quarterly_income")
    def fetch_quarterly_income(self, ticker: str) -> pd.DataFrame:
        """季度利润表（仅利润表，供近4季度趋势），带 6 小时缓存。

        只拉利润表而非三表，避免为单条趋势线重复请求资产负债表/现金流量表。
        """
        cache = get_cache()
        ck = ["fin_stmt", "quarterly", ticker.upper(), "income"]
        cached = cache.get_dataframe(*ck)
        if cached is not None:
            return cached
        if is_ashare(ticker):
            df = self._ak.fetch_income_statements(ticker, period="quarterly")
        else:
            df = self._yf.fetch_income_statements(ticker, period="quarterly")
        if not df.empty:
            cache.set_dataframe(df, CACHE_TTL["financials_quarterly"], *ck)
        return df

    @_ds_instrument("company_info")
    def fetch_company_info(self, ticker: str) -> dict:
        if is_ashare(ticker):
            info = self._ak.fetch_company_info(ticker)
            name = info.get("name", "")
            # 东财公司信息受限（name 落回代码）时，用 Sina 行情兜底公司名称
            if not info.get("industry") and (
                not name or name == normalize_ashare_code(ticker)
            ):
                quote = self._sina.get_sina_quote(ticker)
                if quote:
                    info["name"] = quote["name"]
                    info.setdefault("currency", "CNY")
            return info
        # 美股优先东财 F10；非美股代码返回的 name 仍是代码本身，据此判定未命中
        info = self._us.fetch_company_info(ticker)
        if info.get("name") and info["name"] != ticker.strip().upper():
            return info
        try:
            yf_info = self._yf.fetch_company_info(ticker)
        except Exception as e:
            logger.warning("东财与 yfinance 均未取到 %s 公司信息，回退基础字段: %s", ticker, e)
            return info
        # 东财缺 sector/industry/summary，用 yfinance 补齐（yfinance 有值时优先）
        for k, v in yf_info.items():
            if v not in (None, "", []):
                info[k] = v
        return info

    # ── 新闻 / 业绩说明会 / 股东大会（Phase 9）───────────────

    @_ds_instrument("news")
    def get_news(self, ticker: str, limit: int = 20) -> dict:
        """最新新闻 + 业绩说明会/投资者关系公告。

        返回 {"articles":[{title,summary,published,source,url}...],
              "briefings":[{title,type,published,source,url}...]}
        A 股 → 东财新闻 + 巨潮公告过滤；其他 → yfinance 新闻（briefings 恒空）。
        """
        cache = get_cache()
        if is_ashare(ticker):
            code = normalize_ashare_code(ticker)
            articles = cache.get_dict("news_em", code)
            if articles is None:
                articles = self._ak.fetch_em_news(code, limit)
                cache.set_dict(articles, CACHE_TTL["news_em"], "news_em", code)
            briefings = cache.get_dict("briefings", code)
            if briefings is None:
                briefings = self._ak.fetch_briefings(code)
                cache.set_dict(briefings, CACHE_TTL["briefings"], "briefings", code)
            return {"articles": articles, "briefings": briefings}

        key = ticker.strip().upper()
        articles = cache.get_dict("news_yf", key)
        if articles is None:
            articles = self._yf.fetch_news(ticker, limit)
            cache.set_dict(articles, CACHE_TTL["news_yf"], "news_yf", key)
        return {"articles": articles, "briefings": []}

    @_ds_instrument("meetings")
    def get_meetings(self, ticker: str, limit: int = 5) -> list[dict] | None:
        """最新股东大会信息。A 股返回东财列表；美股返回 yfinance 财报/分红日历。

        美股为免费日历字段（仅披露日期，无提案），空时返回 None 如实标注
        （Phase 4：补全美股会议/日期类数据）。24 小时缓存。
        """
        if not is_ashare(ticker):
            key = ticker.strip().upper()
            cache = get_cache()
            cached = cache.get_dict("meetings_us", key)
            if cached is not None:
                return cached or None
            meetings = self._yf.fetch_meetings(key, limit)
            cache.set_dict(meetings or [], CACHE_TTL["earnings_calendar"],
                           "meetings_us", key)
            return meetings or None
        code = normalize_ashare_code(ticker)
        cache = get_cache()
        cached = cache.get_dict("meetings", code)
        if cached is not None:
            return cached
        meetings = self._ak.fetch_meetings(code, limit)
        cache.set_dict(meetings, CACHE_TTL["meetings"], "meetings", code)
        return meetings

    # ── 搜索 / 校验 ───────────────────────────────────────────

    def search_tickers(self, query: str, top_k: int = 5) -> list[dict]:
        return self._yf.search_tickers(query, top_k)

    def validate_ticker(self, ticker: str) -> bool:
        if is_ashare(ticker):
            return self._sina.get_sina_quote(ticker) is not None
        return self._yf.validate_ticker(ticker)


def _slice_period(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """将完整日线按 yfinance period 语义裁剪（A 股历史无 period 参数）。"""
    if df.empty:
        return df
    window = {
        "5d": 5, "1mo": 21, "3mo": 63, "6mo": 126, "ytd": 252, "1y": 252,
        "2y": 504, "5y": 1260, "max": len(df),
    }.get(period, len(df))
    return df.tail(window)


# 全局单例
_market: MarketData | None = None


def get_market_data() -> MarketData:
    global _market
    if _market is None:
        _market = MarketData()
    return _market
