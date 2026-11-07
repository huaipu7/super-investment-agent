# -*- coding: utf-8 -*-
"""固收统一数据入口 —— 上层(利率/信用引擎/看板)只依赖本模块。

与 MarketData（股票）平级：固收是不等权领域（利率曲线 / 主体信用 / 政策信号），
不复用股票 DataSource 抽象，而是独立 facade + 三个零 key 官方源，
统一走 SQLite 缓存并以 build_vintage 标注 as-of（防 look-ahead）。

数据真实性与诚实边界同全项目：
  - Treasury 曲线按日期真实观测 → is_point_in_time=True
  - EDGAR 财务用真实 filed 日期、剔除 as_of 后未披露行 → 防 look-ahead
  - FOMC 声明为官方原文；逐句政策语义解析属 Phase 2
"""
from __future__ import annotations

import pandas as pd

from app.config import get_settings
from app.data.sources.edgar_source import EdgarSource
from app.data.sources.fomc_source import FomcSource
from app.data.sources.treasury_source import TreasurySource


class FixedIncomeData:
    """固收数据门面，按数据源开关路由到对应源。"""

    def __init__(self):
        self._treasury = TreasurySource()
        self._edgar = EdgarSource()
        self._fomc = FomcSource()

    def _require(self, flag: str, name: str) -> None:
        st = get_settings()
        if not getattr(st, flag, True):
            raise ValueError(f"数据源 {name} 未启用（{flag}=False），无法取数")

    # ── 利率曲线 ─────────────────────────────────────────────
    def get_yield_curve(self, months: int = 13, as_of=None) -> pd.DataFrame:
        """美元国债每日收益率曲线；as_of 截断 ≤as_of（真实 point-in-time）。"""
        self._require("SOURCE_TREASURY", "Treasury")
        df = self._treasury.fetch_yield_curve(months=months, as_of=as_of)
        return df if df is not None else pd.DataFrame()

    # ── 主体信用基本面（EDGAR）───────────────────────────────
    def get_credit_fundamentals(self, ticker: str, as_of=None) -> pd.DataFrame:
        """年度信用基本面面板（revenue/net_income/assets/liabilities/equity/leverage/filed）。"""
        self._require("SOURCE_EDGAR", "EDGAR")
        return self._edgar.fetch_annual_fundamentals(ticker, as_of=as_of)

    def get_credit_fundamentals_lists(self, ticker: str, as_of=None) -> dict:
        """同上，但转成可序列化 dict（看板/API 用，避免丢 attrs）。"""
        df = self.get_credit_fundamentals(ticker, as_of=as_of)
        return self._edgar.to_lists(df)

    def get_credit_panel(self, ticker: str, as_of=None) -> pd.DataFrame:
        """信用评级引擎专用的完整年度科目面板（含流动资产/负债/留存收益/营业利润）。"""
        self._require("SOURCE_EDGAR", "EDGAR")
        return self._edgar.fetch_credit_panel(ticker, as_of=as_of)

    # ── 政策信号 ──────────────────────────────────────────────
    def get_policy_signal(self) -> dict:
        """最近一份 FOMC 声明（官方原文）。"""
        self._require("SOURCE_FOMC", "FOMC")
        return self._fomc.fetch_latest_statement()


_fi: FixedIncomeData | None = None


def get_fixed_income_data() -> FixedIncomeData:
    global _fi
    if _fi is None:
        _fi = FixedIncomeData()
    return _fi
