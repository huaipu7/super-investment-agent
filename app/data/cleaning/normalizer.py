"""数据标准化 —— 从 Research-Agent(backend/data_cleaning/normalizer.py) 迁移。"""
import re
from typing import Optional

import pandas as pd

from app.constants import FINANCIAL_METRICS, UNIT_MULTIPLIERS, DATE_FORMATS


# ── yfinance 英文指标名 → FINANCIAL_METRICS 代码 ─────────────
YFINANCE_METRIC_MAP: dict[str, str] = {
    "Total Revenue": "REVENUE",
    "Revenue": "REVENUE",
    "Cost of Revenue": "COST_OF_REVENUE",
    "Cost Of Revenue": "COST_OF_REVENUE",
    "Gross Profit": "GROSS_PROFIT",
    "Operating Expense": "OPERATING_EXPENSE",
    "Operating Expenses": "OPERATING_EXPENSE",
    "Operating Income": "OPERATING_INCOME",
    "Net Income": "NET_INCOME",
    "Net Income Common Stockholders": "NET_INCOME",
    "Net Income from Continuing Operations": "NET_INCOME",
    "EBITDA": "EBITDA",
    "Interest Expense": "INTEREST_EXPENSE",
    "Interest Expense Net": "INTEREST_EXPENSE",
    "Tax Provision": "TAX_PROVISION",
    "Income Tax Expense": "TAX_PROVISION",
    "Income Tax": "TAX_PROVISION",
    "Pretax Income": None,
    "Research and Development": "RESEARCH_DEV",
    "Selling General and Administrative": None,
    "Selling, General and Administrative": None,
    "Total Operating Expenses": None,
    "Total Assets": "TOTAL_ASSETS",
    "Total Liabilities Net Minority Interest": "TOTAL_LIABILITIES",
    "Total Liabilities": "TOTAL_LIABILITIES",
    "Stockholders Equity": "TOTAL_EQUITY",
    "Total Equity Gross Minority Interest": "TOTAL_EQUITY",
    "Current Assets": "CURRENT_ASSETS",
    "Current Liabilities": "CURRENT_LIABILITIES",
    "Cash and Cash Equivalents": "CASH_AND_EQUIVALENTS",
    "Cash And Cash Equivalents": "CASH_AND_EQUIVALENTS",
    "Net Receivables": "ACCOUNTS_RECEIVABLE",
    "Inventory": "INVENTORY",
    "Property Plant and Equipment": "FIXED_ASSETS",
    "Property Plant Equipment": "FIXED_ASSETS",
    "Goodwill": "GOODWILL",
    "Total Debt": None,
    "Operating Cash Flow": "OPERATING_CASH_FLOW",
    "Cash from Operating Activities": "OPERATING_CASH_FLOW",
    "Investing Cash Flow": "INVESTING_CASH_FLOW",
    "Cash from Investing Activities": "INVESTING_CASH_FLOW",
    "Financing Cash Flow": "FINANCING_CASH_FLOW",
    "Cash from Financing Activities": "FINANCING_CASH_FLOW",
    "Free Cash Flow": "FREE_CASH_FLOW",
    "Capital Expenditure": "CAPEX",
    "Capital Expenditures": "CAPEX",
    "Repurchase Of Capital Stock": "STOCK_BUYBACK",
    "Stock Repurchased": "STOCK_BUYBACK",
    "Common Stock Repurchased": "STOCK_BUYBACK",
}

# ── AKShare 中文指标名 → FINANCIAL_METRICS 代码 ─────────────
AKSHARE_METRIC_MAP: dict[str, str] = {
    "营业收入": "REVENUE",
    "营业总收入": "REVENUE",
    "营业成本": "COST_OF_REVENUE",
    "营业总成本": None,
    "毛利润": "GROSS_PROFIT",
    "营业利润": "OPERATING_INCOME",
    "净利润": "NET_INCOME",
    "归属于母公司所有者的净利润": "NET_INCOME",
    "所得税费用": "TAX_PROVISION",
    "利息费用": "INTEREST_EXPENSE",
    "利息支出": "INTEREST_EXPENSE",
    "资产总计": "TOTAL_ASSETS",
    "总资产": "TOTAL_ASSETS",
    "负债合计": "TOTAL_LIABILITIES",
    "总负债": "TOTAL_LIABILITIES",
    "股东权益合计": "TOTAL_EQUITY",
    "所有者权益合计": "TOTAL_EQUITY",
    "归属于母公司股东权益合计": "TOTAL_EQUITY",
    "流动资产": "CURRENT_ASSETS",
    "流动负债": "CURRENT_LIABILITIES",
    "货币资金": "CASH_AND_EQUIVALENTS",
    "应收账款": "ACCOUNTS_RECEIVABLE",
    "存货": "INVENTORY",
    "固定资产": "FIXED_ASSETS",
    "商誉": "GOODWILL",
    "经营活动产生的现金流量净额": "OPERATING_CASH_FLOW",
    "经营活动现金流量净额": "OPERATING_CASH_FLOW",
    "投资活动产生的现金流量净额": "INVESTING_CASH_FLOW",
    "投资活动现金流量净额": "INVESTING_CASH_FLOW",
    "筹资活动产生的现金流量净额": "FINANCING_CASH_FLOW",
    "筹资活动现金流量净额": "FINANCING_CASH_FLOW",
    "自由现金流": "FREE_CASH_FLOW",
    "资本支出": "CAPEX",
    "购建固定资产、无形资产和其他长期资产支付的现金": "CAPEX",
}

# ── 东方财富新版报表英文列名 → FINANCIAL_METRICS 代码 ──────
# akshare 的 stock_*_sheet_by_report_em 现返回英文代码列
EM_METRIC_MAP: dict[str, str] = {
    # 利润表
    "OPERATE_INCOME": "REVENUE",
    "TOTAL_OPERATE_INCOME": "REVENUE",
    "OPERATE_COST": "COST_OF_REVENUE",
    "RESEARCH_EXPENSE": "RESEARCH_DEV",
    "OPERATE_PROFIT": "OPERATING_INCOME",
    "PARENT_NETPROFIT": "NET_INCOME",
    "INCOME_TAX": "TAX_PROVISION",
    "INTEREST_EXPENSE": "INTEREST_EXPENSE",
    # 资产负债表
    "TOTAL_ASSETS": "TOTAL_ASSETS",
    "TOTAL_LIABILITIES": "TOTAL_LIABILITIES",
    "TOTAL_EQUITY": "TOTAL_EQUITY",
    "TOTAL_PARENT_EQUITY": "TOTAL_EQUITY",
    "TOTAL_CURRENT_ASSETS": "CURRENT_ASSETS",
    "TOTAL_CURRENT_LIAB": "CURRENT_LIABILITIES",
    "MONETARYFUNDS": "CASH_AND_EQUIVALENTS",
    "ACCOUNTS_RECE": "ACCOUNTS_RECEIVABLE",
    "INVENTORY": "INVENTORY",
    "FIXED_ASSET": "FIXED_ASSETS",
    "GOODWILL": "GOODWILL",
    # 现金流量表
    "NETCASH_OPERATE": "OPERATING_CASH_FLOW",
    "NETCASH_INVEST": "INVESTING_CASH_FLOW",
    "NETCASH_FINANCE": "FINANCING_CASH_FLOW",
    "CONSTRUCT_LONG_ASSET": "CAPEX",
}


def normalize_metric_name(name: str, source: str = "auto") -> Optional[str]:
    """将数据源原始指标名映射为标准化代码。找不到则返回 None。"""
    name = str(name).strip()

    if source in ("yfinance", "auto"):
        if name in YFINANCE_METRIC_MAP:
            return YFINANCE_METRIC_MAP[name]

    if source in ("akshare", "auto"):
        if name in AKSHARE_METRIC_MAP:
            return AKSHARE_METRIC_MAP[name]
        if name in EM_METRIC_MAP:
            return EM_METRIC_MAP[name]

    for lookup_name, code in YFINANCE_METRIC_MAP.items():
        if name.lower().replace(" ", "") == lookup_name.lower().replace(" ", ""):
            return code
    for lookup_name, code in AKSHARE_METRIC_MAP.items():
        if name.replace(" ", "") == lookup_name.replace(" ", ""):
            return code

    return None


def normalize_metric_name_with_fallback(name: str, source: str = "auto") -> str:
    """标准化指标名，找不到映射时原样返回（不会丢失数据）。"""
    result = normalize_metric_name(name, source)
    return result if result is not None else name


def convert_unit(value: float, from_unit: str = "", to_unit: str = "元") -> float:
    """数值单位转换（如 亿元 → 元）。"""
    from_mul = UNIT_MULTIPLIERS.get(from_unit.strip(), 1)
    to_mul = UNIT_MULTIPLIERS.get(to_unit.strip(), 1)
    return value * from_mul / to_mul


def detect_unit_from_column_name(col_name: str) -> str:
    """从列名中检测单位标记，如 '营业收入(万元)' → '万元'。"""
    m = re.search(r"[（(](\s*[万亿千百万]+\s*)[)）]", col_name)
    if m:
        unit_str = m.group(1).strip()
        for unit in sorted(UNIT_MULTIPLIERS, key=len, reverse=True):
            if unit and unit in unit_str:
                return unit
    return ""


def normalize_date(date_val) -> Optional[pd.Timestamp]:
    """将各种日期格式统一为 pd.Timestamp。"""
    if pd.isna(date_val) or date_val is None:
        return None
    if isinstance(date_val, pd.Timestamp):
        return date_val
    date_str = str(date_val).strip()
    for fmt in DATE_FORMATS:
        try:
            # pandas 3.0 移除了 Timestamp.strptime，改用 to_datetime(format=)
            return pd.Timestamp(pd.to_datetime(date_str, format=fmt))
        except (ValueError, TypeError):
            continue
    try:
        return pd.Timestamp(pd.to_datetime(date_str))
    except (ValueError, TypeError):
        return None


class FinancialNormalizer:
    """财务数据标准化器。"""

    def __init__(self, source: str = "auto"):
        self.source = source

    def normalize_income(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._rename_metrics(df)

    def normalize_balance_sheet(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._rename_metrics(df)

    def normalize_cash_flow(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._rename_metrics(df)

    def _rename_metrics(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.index.dtype == object:
            new_index = []
            for name in df.index:
                mapped = normalize_metric_name(str(name), self.source)
                new_index.append(mapped if mapped is not None else str(name))
            df = df.rename(index=dict(zip(df.index, new_index)))
            df = df.drop(
                index=[n for n in df.index if normalize_metric_name(str(n), self.source) is None],
                errors="ignore",
            )
        return df

    def normalize(self, financials: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        return {
            "income": self.normalize_income(financials.get("income", pd.DataFrame())),
            "balance_sheet": self.normalize_balance_sheet(
                financials.get("balance_sheet", pd.DataFrame())
            ),
            "cash_flow": self.normalize_cash_flow(financials.get("cash_flow", pd.DataFrame())),
        }
