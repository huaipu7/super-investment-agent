"""清洗管道编排 —— 从 Research-Agent(backend/data_cleaning/pipeline.py) 迁移。

串联：标准化指标名 → 缺失值处理 → 去重。
"""
import pandas as pd

from app.data.cleaning.normalizer import FinancialNormalizer
from app.data.cleaning.missing_handler import MissingHandler
from app.data.cleaning.duplicate_remover import DuplicateRemover


class CleaningPipeline:
    """数据清洗管道。"""

    def __init__(self, source: str = "auto"):
        self.normalizer = FinancialNormalizer(source=source)
        self.missing_handler = MissingHandler()
        self.dedup_income = DuplicateRemover()
        self.dedup_balance = DuplicateRemover()
        self.dedup_cashflow = DuplicateRemover()

    def run(self, financials: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        """执行完整清洗流程：标准化 → 缺失值处理 → 去重。"""
        normalized = self.normalizer.normalize(financials)
        filled = self.missing_handler.auto_impute(normalized)
        return {
            "income": self.dedup_income.remove_duplicates(filled.get("income", pd.DataFrame())),
            "balance_sheet": self.dedup_balance.remove_duplicates(
                filled.get("balance_sheet", pd.DataFrame())
            ),
            "cash_flow": self.dedup_cashflow.remove_duplicates(
                filled.get("cash_flow", pd.DataFrame())
            ),
        }

    def report(self) -> dict:
        impute = self.missing_handler.report()
        return {
            "imputation": {"total_imputed": impute["total_imputed"]},
            "deduplication": {
                "income_removed": self.dedup_income.removed_count,
                "balance_removed": self.dedup_balance.removed_count,
                "cashflow_removed": self.dedup_cashflow.removed_count,
            },
        }

    def report_dataframe(self) -> pd.DataFrame:
        impute = self.missing_handler.report()
        impute_df = impute.get("details", pd.DataFrame())
        parts = [impute_df] if not impute_df.empty else []
        if parts:
            return pd.concat(parts, ignore_index=True)
        return pd.DataFrame({"info": ["无插补/去重操作"]})
