"""基金执行车间 - AKShare 基金数据获取与清洗（迁移自 workshops/fund_executor.py）。

数据源统一走 app.data.sources.fund_source（原 fund_data.py，Phase 1 已迁移）。
"""
from __future__ import annotations

import pandas as pd

from app.data.sources.fund_source import (
    fund_realtime_nav,
    fund_historical_nav,
    fund_holdings,
    fund_basic_info,
    fund_ratings,
)


class FundExecutorError(Exception):
    """基金执行车间异常基类"""
    pass


class FundNotFoundError(FundExecutorError):
    """基金代码未找到"""
    pass


class FundNoDataError(FundExecutorError):
    """数据为空"""
    pass


def get_fund_nav(fund_code: str) -> dict:
    """获取基金实时净值

    Args:
        fund_code: 6位基金代码

    Returns:
        dict: 包含单位净值、累计净值、日增长率等

    Raises:
        FundNotFoundError: 基金代码无效或数据为空
    """
    result = fund_realtime_nav(fund_code)
    if result is None:
        raise FundNotFoundError(f"未找到基金 {fund_code}，请检查代码是否正确")
    return result


def get_fund_history(fund_code: str) -> dict:
    """获取基金历史净值走势

    Args:
        fund_code: 6位基金代码

    Returns:
        dict: 包含 DataFrame 和统计摘要

    Raises:
        FundNoDataError: 数据为空
    """
    df = fund_historical_nav(fund_code, indicator="单位净值走势")
    if df is None or df.empty:
        raise FundNoDataError(f"未获取到 {fund_code} 的历史净值数据")

    # 统一列名
    df = df.rename(columns={
        "净值日期": "date",
        "单位净值": "nav",
        "累计净值": "cum_nav",
        "日增长率": "daily_change",
    })

    # 构建统计摘要
    changes = df["daily_change"] if "daily_change" in df.columns else None

    summary = {
        "fund_code": fund_code,
        "start_date": str(df["date"].iloc[0]) if "date" in df.columns else None,
        "end_date": str(df["date"].iloc[-1]) if "date" in df.columns else None,
        "days": len(df),
        "start_nav": float(df["nav"].iloc[0]) if "nav" in df.columns else None,
        "end_nav": float(df["nav"].iloc[-1]) if "nav" in df.columns else None,
    }

    if changes is not None and len(changes) > 0:
        changes_numeric = changes.dropna()
        if len(changes_numeric) > 0:
            summary["max_daily_increase"] = float(changes_numeric.max())
            summary["max_daily_decrease"] = float(changes_numeric.min())
            summary["avg_daily_change"] = float(changes_numeric.mean())

    return {
        "summary": summary,
        "dataframe": df,
    }


def get_fund_holdings_data(fund_code: str) -> list[dict]:
    """获取基金持仓明细

    Args:
        fund_code: 6位基金代码

    Returns:
        list[dict]: 持仓列表

    Raises:
        FundNoDataError: 数据为空
    """
    result = fund_holdings(fund_code)
    if result is None or len(result) == 0:
        raise FundNoDataError(f"未获取到 {fund_code} 的持仓数据")
    return result


def get_fund_info(fund_code: str) -> dict:
    """获取基金基本信息

    Args:
        fund_code: 6位基金代码

    Returns:
        dict: 含基金经理、规模、费率等

    Raises:
        FundNotFoundError: 数据为空
    """
    result = fund_basic_info(fund_code)
    if result is None:
        raise FundNotFoundError(f"未获取到 {fund_code} 的基本信息")
    return result


def get_fund_ratings_data(fund_code: str) -> dict:
    """获取基金评级

    Args:
        fund_code: 6位基金代码

    Returns:
        dict: 各机构评级

    Raises:
        FundNoDataError: 数据为空
    """
    result = fund_ratings(fund_code)
    if result is None:
        raise FundNoDataError(f"未获取到 {fund_code} 的评级数据")
    return result
