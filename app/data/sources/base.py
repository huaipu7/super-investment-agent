"""数据源抽象基类 —— 定义统一的数据接入接口（迁移自 Research-Agent data_ingestion/base.py）。

下游分析管线（Phase 2）将 YFinanceSource / AKShareSource 视为同一接口。
"""
from abc import ABC, abstractmethod

import pandas as pd


class DataSource(ABC):
    """所有数据源必须实现的抽象基类。"""

    @abstractmethod
    def fetch_income_statements(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        """获取利润表。index=会计期间, columns=指标代码。"""

    @abstractmethod
    def fetch_balance_sheets(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        """获取资产负债表。"""

    @abstractmethod
    def fetch_cash_flow_statements(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        """获取现金流量表。"""

    @abstractmethod
    def fetch_price_history(
        self, ticker: str, start: str = "", end: str = ""
    ) -> pd.DataFrame:
        """获取股价历史。index=交易日, columns=[Open, High, Low, Close, Volume, ...]。"""

    @abstractmethod
    def fetch_company_info(self, ticker: str) -> dict:
        """获取公司基本信息。"""

    def fetch_all_financials(
        self, ticker: str, period: str = "annual"
    ) -> dict[str, pd.DataFrame]:
        """批量获取所有财务报表。"""
        return {
            "income": self.fetch_income_statements(ticker, period),
            "balance_sheet": self.fetch_balance_sheets(ticker, period),
            "cash_flow": self.fetch_cash_flow_statements(ticker, period),
        }

    @abstractmethod
    def search_competitors(self, ticker: str) -> list[str]:
        """搜索同行业竞争对手 ticker 列表。"""
