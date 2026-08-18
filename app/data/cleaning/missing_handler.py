"""缺失值处理 —— 从 Research-Agent(backend/data_cleaning/missing_handler.py) 迁移。"""
from typing import Optional

import pandas as pd


class ImputationRecord:
    """记录插补操作。"""

    def __init__(self):
        self.entries: list[dict] = []

    def add(self, statement: str, period: str, metric: str, method: str, value: float):
        self.entries.append({
            "statement": statement,
            "period": period,
            "metric": metric,
            "method": method,
            "value": value,
        })

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.entries)

    @property
    def count(self) -> int:
        return len(self.entries)


class MissingHandler:
    """缺失值处理器。"""

    def __init__(self):
        self.record = ImputationRecord()

    def forward_fill(self, df: pd.DataFrame, statement_name: str = "") -> pd.DataFrame:
        """时间序列前向填充：用上一期数据填充本期缺失值。"""
        filled = df.copy()
        for col in filled.columns:
            series = filled[col]
            if isinstance(series, pd.DataFrame):
                series = series.iloc[:, 0]
                filled[col] = series
            if series.isna().any():
                prev_val = None
                for idx in series.index:
                    if pd.isna(series.at[idx]) and prev_val is not None:
                        filled.at[idx, col] = prev_val
                        self.record.add(
                            statement_name, str(idx), str(col),
                            "forward_fill", float(prev_val),
                        )
                    elif not pd.isna(series.at[idx]):
                        prev_val = series.at[idx]
        return filled

    def median_impute(self, df: pd.DataFrame, statement_name: str = "") -> pd.DataFrame:
        """用非缺失值的中位数插补。"""
        filled = df.copy()
        for col in filled.columns:
            series = filled[col]
            if isinstance(series, pd.DataFrame):
                series = series.iloc[:, 0]
                filled[col] = series
            if series.isna().any():
                median_val = series.median(skipna=True)
                if pd.isna(median_val):
                    continue  # 整列都为空，无法插补
                mask = series.isna()
                filled.loc[mask, col] = median_val
                for idx in series.index[mask]:
                    self.record.add(
                        statement_name, str(idx), str(col),
                        "median_impute", float(median_val),
                    )
        return filled

    def auto_impute(self, financials: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        """时间序列数据 → 前向填充；剩余缺失 → 中位数插补。"""
        self.record = ImputationRecord()
        result = {}
        for name, df in financials.items():
            if df.empty:
                result[name] = df
                continue
            filled = self.forward_fill(df, name)
            filled = self.median_impute(filled, name)
            result[name] = filled
        return result

    def report(self) -> dict:
        return {
            "total_imputed": self.record.count,
            "details": self.record.to_dataframe(),
        }
