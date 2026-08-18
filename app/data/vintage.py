"""数据时点标记（vintage）—— as_of 历史快照的诚实标注。

Phase B 核心约束：免费数据源（yfinance/akshare/Sina）无法真正回退历史财务
快照。行情 as_of 用「历史序列取 ≤as_of 最后一根」是真实点对点；财务 as_of
只能返回最新一期并显式标注 is_point_in_time=False，绝不伪造历史值。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Vintage:
    """一条数据时点标记。

    is_point_in_time=True 表示「该时点数据的的确确长这样」；
    False 表示「当前最新值仅被标记了 as_of，可能含 restatement」。
    """

    requested_as_of: str | None = None
    data_observation_date: str | None = None
    source: str = ""
    is_point_in_time: bool = True
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "requested_as_of": self.requested_as_of,
            "data_observation_date": self.data_observation_date,
            "source": self.source,
            "is_point_in_time": self.is_point_in_time,
            "note": self.note,
        }


def build_vintage(requested_as_of=None, data_observation_date=None,
                  source="", is_point_in_time=True, note="") -> dict:
    """构造 vintage 字典。

    Args:
        requested_as_of: 调用方请求的数据时点（ISO 日期或 None=实时）。
        data_observation_date: 数据实际反映的日期（as_of 行情=bar 日期；
            实时=抓取时点；财务=数据源返回快照的日期）。
        source: 数据源标签（sina / yfinance / akshare）。
        is_point_in_time: True=真实点对点；False=当前值仅被标记 as_of。
        note: 显式说明，供审计 / 报告溯源。
    """
    return Vintage(
        requested_as_of=str(requested_as_of) if requested_as_of else None,
        data_observation_date=str(data_observation_date) if data_observation_date else None,
        source=source or "",
        is_point_in_time=bool(is_point_in_time),
        note=note or "",
    ).to_dict()
