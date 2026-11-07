"""东方财富美股 F10 数据源 —— 美股财务与公司信息。

背景：Yahoo 对非住宅出口 IP 返回 403（反爬，非网络问题），yfinance 的美股
通道失效。东财美股 F10 国内直连、零 key，覆盖三大报表与主要指标，作为美股主通道。

约定与其他数据源一致：index=会计期间(DatetimeIndex)，columns=FINANCIAL_METRICS 标准代码。
本模块自行完成科目归一（输出即为标准代码），因此不依赖 CleaningPipeline 的映射表——
该表的 `df.index.dtype == object` 守卫在 pandas 3 下恒为假，列会原样透传。

诚实边界：东财美股 F10 只有年报/季报口径的三表，无分部收入、无 point-in-time。
"""
from __future__ import annotations

import re

import pandas as pd

from app.data.sources.akshare_source import _retry_em
from app.data.sources.base import DataSource

# 东财三大报表的中文报表名
_STATEMENT_SYMBOL = {
    "income": "综合损益表",
    "balance_sheet": "资产负债表",
    "cash_flow": "现金流量表",
}

# 东财美股代码：1-6 位字母，股份类别用**下划线**（实测 BRK_B 可用，BRK.B / BRK-B 不可用）。
# 不匹配 0700.HK / 600519.SS / 600519 等代码 → 立即返回空，交由 yfinance 兜底。
_US_TICKER_RE = re.compile(r"^[A-Z]{1,6}(?:_[A-Z])?$")

# 中文科目名 → 标准代码，**按报表分开**。
#
# 必须分表的原因：同一中文名在不同报表里含义不同。现金流量表里的「存货」是
# **营运资金变动额**、「净利润」是现金流起点，若共用一个映射表，它们会被当成
# 资产负债表/利润表口径写进 INVENTORY / NET_INCOME，污染下游（如存货趋势、净利）。
# 只收录有明确对应的科目；未收录的科目直接丢弃。
EM_US_METRIC_MAP: dict[str, dict[str, str]] = {
    "income": {
        "营业收入": "REVENUE",
        "主营收入": "REVENUE",
        "营业成本": "COST_OF_REVENUE",
        "主营成本": "COST_OF_REVENUE",
        "毛利": "GROSS_PROFIT",
        "营业费用": "OPERATING_EXPENSE",
        "营业利润": "OPERATING_INCOME",
        "净利润": "NET_INCOME",
        "归属于母公司股东净利润": "NET_INCOME",
        "所得税": "TAX_PROVISION",
        "研发费用": "RESEARCH_DEV",
        "利息支出": "INTEREST_EXPENSE",
    },
    "balance_sheet": {
        "现金及现金等价物": "CASH_AND_EQUIVALENTS",
        "应收账款": "ACCOUNTS_RECEIVABLE",
        "存货": "INVENTORY",
        "流动资产合计": "CURRENT_ASSETS",
        "流动负债合计": "CURRENT_LIABILITIES",
        "物业、厂房及设备": "FIXED_ASSETS",
        "商誉": "GOODWILL",
        "总资产": "TOTAL_ASSETS",
        "总负债": "TOTAL_LIABILITIES",
        "股东权益合计": "TOTAL_EQUITY",
        "归属于母公司股东权益": "TOTAL_EQUITY",
    },
    "cash_flow": {
        "经营活动产生的现金流量净额": "OPERATING_CASH_FLOW",
        "投资活动产生的现金流量净额": "INVESTING_CASH_FLOW",
        "筹资活动产生的现金流量净额": "FINANCING_CASH_FLOW",
        "购买固定资产": "CAPEX",
        "购建无形资产及其他资产": "CAPEX",
        "回购股份": "STOCK_BUYBACK",
    },
}

# 多个科目映射到同一标准码时的取舍优先级（数字越小越优先）。
# 取舍依据（已用 SE 实际值核对）：
#   NET_INCOME —— 取归母，与仓库既有 A 股映射（PARENT_NETPROFIT → NET_INCOME）一致；
#                SE FY2025 归母 1,578,149,000 ≠ 净利润 1,610,894,000，两者确有区别。
#   TOTAL_EQUITY —— 取「股东权益合计」，以保持 资产 = 负债 + 权益 恒等式
#                （SE FY2025: 16,722,651 + 12,648,328 = 29,370,979 ✓）。
#   REVENUE / COST_OF_REVENUE —— 东财「合计」口径优先于「主营」口径。
_PRIORITY: dict[str, int] = {
    "归属于母公司股东净利润": 0, "净利润": 1,
    "股东权益合计": 0, "归属于母公司股东权益": 1,
    "营业收入": 0, "主营收入": 1,
    "营业成本": 0, "主营成本": 1,
    "购买固定资产": 0, "购建无形资产及其他资产": 1,
}
_DEFAULT_PRIORITY = 50


def normalize_us_ticker(ticker: str) -> str:
    """美股代码规范化：`BRK.B` / `BRK-B` → `BRK_B`（东财以含下划线表示股份类别）。"""
    return re.sub(r"[.\-]", "_", ticker.strip().upper())


def is_us_ticker(ticker: str) -> bool:
    """是否为东财美股 F10 可服务的美股代码（不含 .HK/.SS 等交易所后缀）。"""
    return bool(_US_TICKER_RE.match(normalize_us_ticker(ticker)))


def _to_wide(long_df: pd.DataFrame, kind: str) -> pd.DataFrame:
    """东财美股长表（每行一个科目×一期）→ 宽表 index=报告期, columns=标准代码。"""
    if long_df is None or long_df.empty:
        return pd.DataFrame()
    if not {"REPORT_DATE", "ITEM_NAME", "AMOUNT"}.issubset(long_df.columns):
        return pd.DataFrame()

    metric_map = EM_US_METRIC_MAP.get(kind)
    if not metric_map:
        return pd.DataFrame()

    df = long_df.copy()
    df["metric"] = df["ITEM_NAME"].map(metric_map)
    df = df[df["metric"].notna()]
    if df.empty:
        return pd.DataFrame()

    df["_prio"] = df["ITEM_NAME"].map(
        lambda n: _PRIORITY.get(n, _DEFAULT_PRIORITY)
    )
    df["REPORT_DATE"] = pd.to_datetime(
        df["REPORT_DATE"], errors="coerce", format="mixed"
    )
    df = df[df["REPORT_DATE"].notna()]
    if df.empty:
        return pd.DataFrame()

    df = df.sort_values("_prio").drop_duplicates(
        subset=["REPORT_DATE", "metric"], keep="first"
    )
    wide = df.pivot(index="REPORT_DATE", columns="metric", values="AMOUNT")
    wide = wide.apply(pd.to_numeric, errors="coerce")
    wide = wide.dropna(how="all").dropna(axis=1, how="all")

    # 利润表缺毛利时用 收入 - 成本 补算（与 A 股 _parse_em_financials 同口径）
    if kind == "income" and "GROSS_PROFIT" not in wide.columns:
        if {"REVENUE", "COST_OF_REVENUE"}.issubset(wide.columns):
            wide["GROSS_PROFIT"] = wide["REVENUE"] - wide["COST_OF_REVENUE"]

    wide.index.name = "fiscal_period"
    return wide.sort_index()


class AKShareUSSource(DataSource):
    """东方财富美股 F10 —— 美股财务三表与公司信息。"""

    def __init__(self):
        self.cache = None  # 缓存统一在 data/cache 层（router），此处无状态

    # ── 三大报表 ─────────────────────────────────────────────

    def _fetch_statement(self, ticker: str, kind: str, period: str = "annual") -> pd.DataFrame:
        if not is_us_ticker(ticker):
            return pd.DataFrame()
        indicator = "年报" if period == "annual" else "累计季报"
        symbol = _STATEMENT_SYMBOL[kind]

        def _call():
            import akshare as ak
            return ak.stock_financial_us_report_em(
                stock=normalize_us_ticker(ticker), symbol=symbol, indicator=indicator
            )

        try:
            long_df = _retry_em(_call)
        except Exception:
            return pd.DataFrame()
        return _to_wide(long_df, kind)

    def fetch_income_statements(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        return self._fetch_statement(ticker, "income", period)

    def fetch_balance_sheets(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        return self._fetch_statement(ticker, "balance_sheet", period)

    def fetch_cash_flow_statements(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        return self._fetch_statement(ticker, "cash_flow", period)

    # ── 公司信息 ─────────────────────────────────────────────

    def fetch_company_info(self, ticker: str) -> dict:
        """公司基本信息。

        shares_outstanding 为**推算值**（市值 ÷ 现价，两者均为 Sina 真实行情），
        语义等同 yfinance 的 impliedSharesOutstanding；无行情时留 None，不编造。
        sector/industry/country/summary 东财美股 F10 不提供，如实留空。
        """
        # name 默认回显原代码，供 router 判定「未命中」并回退 yfinance
        result: dict = {
            "name": ticker.strip().upper(), "sector": "", "industry": "",
            "currency": "USD", "country": "", "summary": "", "employees": None,
            "website": "", "shares_outstanding": None, "fiscal_year_end": "",
            "market_cap": None,
        }
        if not is_us_ticker(ticker):
            return result
        code = normalize_us_ticker(ticker)

        # 名称与币种取自三表（东财美股报表带 SECURITY_NAME_ABBR / CURRENCY）
        try:
            from app.data.sources.sina_source import SinaSource
            quote = SinaSource().get_us_quote(code)
        except Exception:
            quote = None
        if quote:
            result["name"] = quote.get("company_name") or code
            result["currency"] = quote.get("currency") or "USD"
            result["market_cap"] = quote.get("market_cap")
            price = quote.get("current_price")
            if result["market_cap"] and price:
                result["shares_outstanding"] = round(result["market_cap"] / price)
        return result

    def fetch_reporting_currency(self, ticker: str) -> str | None:
        """报表记账币种（东财 CURRENCY_ABBR，如 USD / CNY / KRW）。

        必须与三表同源：东财三表金额按**报表本币**计价（BABA 人民币、GRVY 韩元），
        而行情的 currency 是**挂牌币种**（美股 ADR 恒为 USD）。两者混用会把
        人民币/韩元当成美元，造成数量级错误。
        """
        if not is_us_ticker(ticker):
            return None

        def _call():
            import akshare as ak
            return ak.stock_financial_us_analysis_indicator_em(
                symbol=normalize_us_ticker(ticker), indicator="年报"
            )

        try:
            df = _retry_em(_call)
        except Exception:
            return None
        if df is None or df.empty or "CURRENCY_ABBR" not in df.columns:
            return None
        vals = df["CURRENCY_ABBR"].dropna()
        return str(vals.iloc[0]).strip().upper() if not vals.empty else None

    def get_financials(self, ticker: str) -> dict:
        """关键财务指标概览（美股）。

        只填真实可得的字段：利润表口径的营收/净利/营收增速，以及 Sina 行情的
        PE/市值/现价。拿不到的字段**不填**（如 forward_pe / enterprise_value /
        dividend_yield 免费源无此口径），绝不填充近似值。
        """
        if not is_us_ticker(ticker):
            return {}
        code = normalize_us_ticker(ticker)
        out: dict = {"ticker": code}

        # currency 是**报表记账币种**（非挂牌币种），供调用方正确折算成美元
        reporting_ccy = self.fetch_reporting_currency(ticker)
        if reporting_ccy:
            out["currency"] = reporting_ccy

        inc = self.fetch_income_statements(ticker)
        if not inc.empty:
            if "REVENUE" in inc.columns:
                rev = inc["REVENUE"].dropna()
                if not rev.empty:
                    out["revenue"] = float(rev.iloc[-1])
                    if len(rev) >= 2 and rev.iloc[-2]:
                        out["revenue_growth"] = float(rev.iloc[-1] / rev.iloc[-2] - 1)
            if "NET_INCOME" in inc.columns:
                ni = inc["NET_INCOME"].dropna()
                if not ni.empty:
                    out["net_income"] = float(ni.iloc[-1])
                    if out.get("revenue"):
                        out["profit_margin"] = float(ni.iloc[-1] / out["revenue"])

        try:
            from app.data.sources.sina_source import SinaSource
            quote = SinaSource().get_us_quote(code)
        except Exception:
            quote = None
        if quote:
            # 刻意不取 quote 的 currency —— 那是挂牌币种(USD)，会覆盖报表记账币种
            for src_key in ("pe_ratio", "market_cap", "company_name", "current_price"):
                if quote.get(src_key) is not None:
                    out[src_key] = quote[src_key]
        return out

    # ── 行情（委托既有 akshare 美股日线）────────────────────

    def fetch_price_history(
        self, ticker: str, start: str = "", end: str = ""
    ) -> pd.DataFrame:
        """美股日线。start/end 为可选截断区间。"""
        if not is_us_ticker(ticker):
            return pd.DataFrame()
        df = self._fetch_us_daily(ticker)
        if df.empty:
            return df
        if start:
            df = df.loc[df.index >= pd.Timestamp(start)]
        if end:
            df = df.loc[df.index <= pd.Timestamp(end)]
        return df

    @staticmethod
    def _fetch_us_daily(ticker: str) -> pd.DataFrame:
        def _call():
            import akshare as ak
            return ak.stock_us_daily(symbol=normalize_us_ticker(ticker))

        try:
            df = _retry_em(_call)
        except Exception:
            return pd.DataFrame()
        if df is None or df.empty or "date" not in df.columns:
            return pd.DataFrame()
        out = df.copy()
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out = out.dropna(subset=["date"]).set_index("date").sort_index()
        out = out.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        })
        keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in out.columns]
        return out[keep].apply(pd.to_numeric, errors="coerce")

    # ── 其他 ─────────────────────────────────────────────────

    def search_competitors(self, ticker: str) -> list[str]:
        """东财美股 F10 无竞品接口 —— 返回空，由 LLM/行业分类确定（同 yfinance 源语义）。"""
        return []

    def fetch_news(self, ticker: str, limit: int = 20) -> list[dict]:
        """东财美股新闻暂无可用接口 —— 如实返回空列表，不阻塞主流程。"""
        return []
