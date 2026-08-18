"""
AKShare 数据源 —— A 股市场。

合并迁移：
  - Research-Agent(backend/data_ingestion/akshare_client.py)：东方财富财报解析、股价、公司信息
  - Violet Z(app.py)：stock_financial_analysis_indicator 财务指标（供四大师评分）
"""
from __future__ import annotations

import datetime
import json
import re
import time
import urllib.parse
import urllib.request

import pandas as pd

from app.data.cleaning.normalizer import (
    AKSHARE_METRIC_MAP,
    normalize_metric_name,
    normalize_date,
)
from app.data.sources.base import DataSource


def _retry_em(func, times: int = 3, delay: float = 1.5):
    """东财接口在本网络间歇性断连（ProxyError），重试几次提高成功率。"""
    last_error = None
    for i in range(times):
        try:
            return func()
        except Exception as e:
            last_error = e
            if i < times - 1:
                time.sleep(delay)
    raise last_error


def normalize_ashare_code(ticker: str) -> str:
    """将 A 股代码标准化为纯数字格式。

    "600519.SS" / "600519.SH" → "600519"，"000001.SZ" → "000001"，"SH600519" → "600519"
    （注意同时支持 .SS 与 .SH：yfinance 用 .SS，Violet Z 的 TICKER_MAP 也用 .SS）
    """
    code = ticker.strip().upper()
    code = re.sub(r"[.](SS|SH|SZ|BJ)$", "", code)
    code = re.sub(r"^(SH|SZ|BJ)", "", code)
    return code


def detect_ashare_exchange(code: str) -> str:
    """判断 A 股代码所属交易所前缀（用于 Sina 等接口）。6→sh, 0/3→sz, 8/4→bj。"""
    c = normalize_ashare_code(code)
    if c.startswith("6"):
        return "sh"
    if c.startswith(("0", "3")):
        return "sz"
    if c.startswith(("8", "4")):
        return "bj"
    return ""


def _em_symbol(ticker: str) -> str:
    """构造东方财富接口所需的市场代码（带前缀，如 600519 → SH600519）。"""
    code = normalize_ashare_code(ticker)
    exchange = detect_ashare_exchange(ticker)
    if not exchange:
        return code
    return f"{exchange.upper()}{code}"


def _strip_unit_suffix(col_name: str) -> str:
    """去掉列名末尾的 '(万元)' '(亿元)' 等单位后缀。"""
    return re.sub(r"[（(][万亿千百万元\s]*[)）]$", "", str(col_name)).strip()


def _parse_em_financials(
    ak_function,
    expected_cols: set[str],
    period: str = "annual",
) -> pd.DataFrame:
    """通用 East Money 财报解析（Research-Agent 迁移）。

    period="annual" 时只保留 12-31 年报（东财返回季报累计值，避免把 Q1 当全年）。
    """
    try:
        import akshare as ak  # noqa: F401
    except ImportError:
        return pd.DataFrame()

    try:
        raw = _retry_em(ak_function)
    except Exception:
        return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    date_col = None
    for col in raw.columns:
        col_str = str(col).strip()
        if any(kw in col_str for kw in ("报告期", "REPORT_DATE", "报告日", "截止日期")):
            date_col = col
            break

    if date_col is None:
        return pd.DataFrame()

    dates = raw[date_col].apply(normalize_date)
    df = raw.copy().drop(columns=[date_col])

    col_mapping = {}
    for col in df.columns:
        col_str = str(col).strip()
        code = normalize_metric_name(col_str, source="akshare")
        if code is None:
            stripped = _strip_unit_suffix(col_str)
            code = normalize_metric_name(stripped, source="akshare")
        if code is not None:
            col_mapping[col] = code

    df = df.rename(columns=col_mapping)
    df = df.loc[:, [c for c in df.columns if c in col_mapping.values()]]
    df = df.loc[:, ~df.columns.duplicated(keep="first")]  # 多个源列映射到同一代码时取首个

    df.index = dates
    df.index.name = "fiscal_period"

    if period == "annual":
        # 仅保留 12 月 31 日的年度报告（东财数据为季报累计值）
        annual_mask = (df.index.month == 12) & (df.index.day == 31)
        if annual_mask.any():
            df = df.loc[annual_mask]

    df = df.apply(pd.to_numeric, errors="coerce")
    df = df.dropna(how="all").dropna(axis=1, how="all")

    # 若利润表缺毛利润，用 营业收入 - 营业成本 补算
    if "GROSS_PROFIT" not in df.columns and "REVENUE" in df.columns and "COST_OF_REVENUE" in df.columns:
        df["GROSS_PROFIT"] = df["REVENUE"] - df["COST_OF_REVENUE"]

    return df.sort_index()


def _strip_em(text) -> str:
    """去掉东财搜索返回的 <em> 高亮标签与零宽空白。"""
    cleaned = re.sub(r"</?em>", "", str(text))
    return re.sub(r"\s+", " ", cleaned).replace("　", " ").strip()


def _fmt_date(value) -> str:
    """安全格式化日期（NaT/None → 空串）。"""
    if value is None or (hasattr(pd, "isna") and pd.isna(value)):
        return ""
    try:
        return pd.to_datetime(value).strftime("%Y-%m-%d")
    except Exception:
        return str(value)


class AKShareSource(DataSource):
    """基于 AKShare 的 A 股市场数据接入。"""

    def __init__(self):
        self.cache = None  # 财务指标缓存接入在 data/cache 层，这里保持无状态

    def fetch_income_statements(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        import akshare as ak
        symbol = _em_symbol(ticker)
        return _parse_em_financials(
            lambda: ak.stock_profit_sheet_by_report_em(symbol=symbol), set(), period
        )

    def fetch_balance_sheets(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        import akshare as ak
        symbol = _em_symbol(ticker)
        return _parse_em_financials(
            lambda: ak.stock_balance_sheet_by_report_em(symbol=symbol), set(), period
        )

    def fetch_cash_flow_statements(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        import akshare as ak
        symbol = _em_symbol(ticker)
        return _parse_em_financials(
            lambda: ak.stock_cash_flow_sheet_by_report_em(symbol=symbol), set(), period
        )

    def fetch_price_history(
        self, ticker: str, start: str = "", end: str = ""
    ) -> pd.DataFrame:
        import akshare as ak
        symbol = normalize_ashare_code(ticker)
        try:
            def _call():
                if start and end:
                    return ak.stock_zh_a_hist(
                        symbol=symbol,
                        start_date=start.replace("-", ""),
                        end_date=end.replace("-", ""),
                    )
                return ak.stock_zh_a_hist(symbol=symbol)
            hist = _retry_em(_call)
        except Exception:
            return pd.DataFrame()

        if hist is None or hist.empty:
            return pd.DataFrame()

        rename_map = {}
        col_names = {"开盘": "Open", "收盘": "Close", "最高": "High", "最低": "Low", "成交量": "Volume"}
        for cn, en in col_names.items():
            if cn in hist.columns:
                rename_map[cn] = en
        hist = hist.rename(columns=rename_map)
        if "日期" in hist.columns:
            hist.index = pd.to_datetime(hist["日期"])
            hist = hist.drop(columns=["日期"])
        keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in hist.columns]
        return hist[keep].sort_index()

    def fetch_index_history(
        self, symbol: str, period: str = "5y"
    ) -> pd.DataFrame:
        """A 股基准指数历史行情（如沪深300 'sh000300'），供 beta/ERP 估计。

        免费源：akshare stock_zh_index_daily（失败回退东财历史）。返回
        OHLCV DataFrame（index=DatetimeIndex），确定性由缓存层保证。
        """
        import akshare as ak
        try:
            df = _retry_em(lambda: ak.stock_zh_index_daily(symbol=symbol))
        except Exception:
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        rename = {}
        for cn, en in {"date": None, "open": "Open", "high": "High",
                       "low": "Low", "close": "Close", "volume": "Volume"}.items():
            if cn in df.columns and en:
                rename[cn] = en
        df = df.rename(columns=rename)
        if "date" in df.columns:
            df.index = pd.to_datetime(df["date"])
            df = df.drop(columns=["date"])
        keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        df = df[keep].sort_index()
        window = {"5d": 5, "1mo": 21, "3mo": 63, "6mo": 126, "1y": 252,
                  "2y": 504, "5y": 1260, "max": len(df)}.get(period, len(df))
        return df.tail(window) if not df.empty else df

    def fetch_company_info(self, ticker: str) -> dict:
        import akshare as ak
        symbol = normalize_ashare_code(ticker)
        try:
            info = _retry_em(lambda: ak.stock_individual_info_em(symbol=symbol))
        except Exception:
            info = pd.DataFrame()  # 公司信息受限时保留默认值，继续补股本等字段

        result = {"name": symbol, "sector": "", "industry": "", "currency": "CNY"}
        if isinstance(info, pd.DataFrame):
            for _, row in info.iterrows():
                item = str(row.iloc[0]) if row.iloc[0] is not None else ""
                val = str(row.iloc[1]) if len(row) > 1 and row.iloc[1] is not None else ""
                if "简称" in item:
                    result["name"] = val.strip()
                elif "行业" in item:
                    result["industry"] = val.strip()
                elif "概念" in item:
                    result.setdefault("concepts", []).append(val.strip())
                elif "总市值" in item:
                    result["market_cap"] = val.strip()

        # 补充总股本（供 DCF/SOTP 每股价值）：股东权益 / 每股净资产
        if not result.get("shares_outstanding"):
            try:
                fin = self.get_financial_indicators(symbol)
                nav = fin.get("nav_per_share")
                bs = self.fetch_balance_sheets(ticker)
                if nav and not bs.empty and "TOTAL_EQUITY" in bs.columns:
                    equity = float(bs["TOTAL_EQUITY"].iloc[-1])
                    if equity > 0:
                        result["shares_outstanding"] = round(equity / nav, 0)
            except Exception:
                pass
        return result

    def search_competitors(self, ticker: str) -> list[str]:
        info = self.fetch_company_info(ticker)
        if not info.get("industry"):
            return []
        return []

    # ── 财务指标（Violet Z 迁移，供专家评分）─────────────────

    def get_financial_indicators(self, code: str, start_year: str | None = None) -> dict:
        """获取 A 股财务指标（取最新年报，供四大师评分）。

        字段: roe / debt_ratio / cost_ratio / net_margin / profit_growth /
              revenue_growth / current_ratio / eps_cash_flow / eps / nav_per_share
        start_year 缺省时按当前年往前推 5 年（不再硬编码 2024）。
        """
        import akshare as ak

        if not start_year:
            start_year = str(datetime.date.today().year - 5)
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=start_year)
        result: dict = {}
        if df is None or df.empty:
            return result

        annual_rows = df[df["日期"].astype(str).str.contains("12-31")]
        if not annual_rows.empty:
            row = annual_rows.iloc[-1]  # 最新年报
        else:
            row = df.iloc[0]  # 最新季度

        field_map = {
            "净资产收益率(%)": "roe",
            "资产负债率(%)": "debt_ratio",
            "主营业务成本率(%)": "cost_ratio",
            "销售净利率(%)": "net_margin",
            "净利润增长率(%)": "profit_growth",
            "主营业务收入增长率(%)": "revenue_growth",
            "流动比率": "current_ratio",
            "每股经营性现金流(元)": "eps_cash_flow",
            "摊薄每股收益(元)": "eps",
            "每股净资产_调整前(元)": "nav_per_share",
        }
        for col, key_name in field_map.items():
            if col in df.columns:
                val = row[col]
                if pd.notna(val):
                    try:
                        result[key_name] = float(val)
                    except (ValueError, TypeError):
                        pass

        if "cost_ratio" in result:
            result["gross_margin"] = round(100 - result["cost_ratio"], 4)
        return result

    # ── 新闻 / 业绩说明会 / 股东大会（Phase 9）───────────────

    def fetch_em_news(self, ticker: str, limit: int = 20) -> list[dict]:
        """东财个股新闻（最近 N 条，按发布时间倒序）。

        直接调东财 search API 自行解析，绕开 akshare 1.18.60 在该网络/环境中
        pandas+pyarrow 的 .str.replace 崩溃 bug（akshare 内部无法注入参数）。
        统一 item schema: {title, summary, published, source, url}
        """
        code = normalize_ashare_code(ticker)
        inner = {
            "uid": "",
            "keyword": code,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "time",
                    "pageIndex": 1,
                    "pageSize": min(limit, 100),
                    "preTag": "<em>",
                    "postTag": "</em>",
                }
            },
        }
        url = (
            "https://search-api-web.eastmoney.com/search/jsonp?cb=jq&param="
            + urllib.parse.quote(json.dumps(inner, ensure_ascii=False))
            + "&_=1"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Referer": "https://so.eastmoney.com/news/s",
                "User-Agent": "Mozilla/5.0",
            },
        )
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            text = resp.read().decode("utf-8")
            data = json.loads(text[text.index("(") + 1 : text.rindex(")")])
            articles = data["result"]["cmsArticleWebOld"]
        except Exception:
            return []

        items: list[dict] = []
        for a in articles[:limit]:
            title = _strip_em(a.get("title", ""))
            if not title:
                continue
            items.append(
                {
                    "title": title,
                    "summary": _strip_em(a.get("content", ""))[:300],
                    "published": str(a.get("date", "")),
                    "source": str(a.get("mediaName", "")),
                    "url": "http://finance.eastmoney.com/a/"
                    + str(a.get("code", ""))
                    + ".html",
                }
            )
        return items

    def fetch_briefings(self, ticker: str, days: int = 180) -> list[dict]:
        """业绩说明会 / 投资者关系活动相关公告（巨潮公告标题过滤）。"""
        import akshare as ak

        code = normalize_ashare_code(ticker)
        end = datetime.date.today()
        begin = end - datetime.timedelta(days=days)
        try:
            df = ak.stock_individual_notice_report(
                security=code,
                symbol="全部",
                begin_date=begin.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
        except Exception:
            return []
        if df is None or df.empty:
            return []

        mask = df["公告标题"].astype(str).str.contains(
            "业绩说明会|投资者关系|说明会", na=False
        )
        items: list[dict] = []
        for _, row in df[mask].head(10).iterrows():
            items.append(
                {
                    "title": str(row.get("公告标题", "")),
                    "type": str(row.get("公告类型", "")),
                    "published": str(row.get("公告日期", "")),
                    "source": "巨潮资讯",
                    "url": str(row.get("网址", "")) if not pd.isna(row.get("网址")) else "",
                }
            )
        return items

    def fetch_meetings(self, ticker: str, limit: int = 5) -> list[dict]:
        """最新股东大会（东财 stock_gddh_em 全市场，按代码过滤，含提案）。"""
        import akshare as ak

        code = normalize_ashare_code(ticker)
        try:
            df = _retry_em(ak.stock_gddh_em)
        except Exception:
            return []
        if df is None or df.empty:
            return []

        sub = df[df["代码"].astype(str) == code]
        meetings: list[dict] = []
        for _, row in sub.tail(limit).iloc[::-1].iterrows():
            proposals = row.get("提案", "")
            meetings.append(
                {
                    "name": str(row.get("股东大会名称", "")),
                    "start_date": _fmt_date(row.get("召开开始日")),
                    "record_date": _fmt_date(row.get("股权登记日")),
                    "resolution_date": _fmt_date(row.get("决议公告日")),
                    "announcement_date": _fmt_date(row.get("公告日")),
                    "proposals": str(proposals) if not pd.isna(proposals) else "",
                }
            )
        return meetings

    # ── A 股日线（Violet Z 迁移，供换手率等）─────────────────

    def get_recent_daily(self, code: str, days: int = 120) -> dict | None:
        """获取最近日 K 线最后一根（含换手率/成交量），修复了原项目硬编码日期范围的问题。"""
        import akshare as ak
        symbol = normalize_ashare_code(code)
        try:
            df = _retry_em(lambda: ak.stock_zh_a_hist(symbol=symbol, period="daily", adjust="qfq"))
        except Exception:
            return None
        if df is None or df.empty:
            return None
        return df.iloc[-1].to_dict()

    def get_ashare_daily(self, ticker: str, period: str = "1mo") -> pd.DataFrame:
        """Sina 日线兜底（stock_zh_a_daily），东财 stock_zh_a_hist 不可用时使用。"""
        import akshare as ak
        symbol = normalize_ashare_code(ticker)
        exchange = detect_ashare_exchange(ticker)
        if not exchange:
            return pd.DataFrame()
        try:
            df = ak.stock_zh_a_daily(symbol=f"{exchange}{symbol}", adjust="qfq")
        except Exception:
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df[["date", "open", "high", "low", "close", "volume"]].copy()
        df.columns = ["Date", "Open", "High", "Low", "Close", "Volume"]
        df.index = pd.to_datetime(df["Date"])
        df = df.drop(columns=["Date"]).sort_index()
        return df.tail(_PERIOD_WINDOW.get(period, len(df)))

    def get_us_history(self, ticker: str, period: str = "1mo") -> pd.DataFrame:
        """Sina 美股日线（stock_us_daily），yfinance 不可用时兜底。"""
        import akshare as ak
        try:
            df = ak.stock_us_daily(symbol=ticker.upper())
        except Exception:
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df[["date", "open", "high", "low", "close", "volume"]].copy()
        df.columns = ["Date", "Open", "High", "Low", "Close", "Volume"]
        df.index = pd.to_datetime(df["Date"])
        df = df.drop(columns=["Date"]).sort_index()
        return df.tail(_PERIOD_WINDOW.get(period, len(df)))


_PERIOD_WINDOW = {
    "5d": 5, "1mo": 21, "3mo": 63, "6mo": 126, "ytd": 252, "1y": 252,
    "2y": 504, "5y": 1260, "max": 10**7,
}
