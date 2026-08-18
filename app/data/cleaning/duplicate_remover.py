"""去重 —— 从 Research-Agent(backend/data_cleaning/duplicate_remover.py) 迁移。"""
import pandas as pd


class DuplicateRemover:
    """财务数据去重器：按 index（fiscal period）去重，保留第一个。"""

    def __init__(self):
        self.removed_count = 0

    def remove_duplicates(self, df: pd.DataFrame) -> pd.DataFrame:
        before = len(df)
        deduped = df[~df.index.duplicated(keep="first")]
        self.removed_count = before - len(deduped)
        return deduped

    def report(self) -> dict:
        return {"removed_rows": self.removed_count}
