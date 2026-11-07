"""
基金数据源 —— 基于 AKShare，封装天天基金/东方财富数据。

完整迁移自 Agent-for-Investment(workshops/fund_data.py)：
基金搜索、实时净值、历史净值、持仓、基本信息、评级、累计净值走势。
"""
from __future__ import annotations

import logging
import re
import time
from functools import lru_cache

import akshare as ak
import pandas as pd

logger = logging.getLogger(__name__)

# 上游用这些字面量表示「这一栏没有数」，它们不是数值
_MISSING_TOKENS = frozenset({"", "-", "--", "—", "nan", "none", "null"})


def _num(v) -> float | None:
    """净值字段转数值。

    上游对空栏位返回的是空串而不只是 '-'，只判 '-' 会让 float('') 抛
    ValueError，把整张快照打掉。缺值一律返回 None，不填 0。
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if v != v else float(v)  # v != v 只在 NaN 时为真
    s = str(v).strip()
    if s.lower() in _MISSING_TOKENS:
        return None
    try:
        return float(s)
    except ValueError:
        logger.warning(f"净值字段无法解析为数值: {v!r}")
        return None


# ========== 全局缓存 ==========

_FUND_LIST_CACHE: list[dict] | None = None  # 全量基金列表
_FUND_LIST_TIME: float = 0
_FUND_DAILY_CACHE: pd.DataFrame | None = None  # 全量基金日净值


def _load_fund_list() -> list[dict]:
    """加载全量基金列表（缓存，按需刷新，1 小时内复用）。"""
    global _FUND_LIST_CACHE, _FUND_LIST_TIME
    now = time.time()
    if _FUND_LIST_CACHE is not None and now - _FUND_LIST_TIME < 3600:
        return _FUND_LIST_CACHE
    try:
        df = ak.fund_name_em()
        df.columns = ["基金代码", "拼音缩写", "基金简称", "基金类型", "拼音全称"]
        records = df.to_dict("records")
        _FUND_LIST_CACHE = records
        _FUND_LIST_TIME = now
        return records
    except Exception as e:
        logger.warning(f"加载基金列表失败: {e}")
        return _FUND_LIST_CACHE or []


def fund_search(query: str, topk: int = 10) -> list[dict]:
    """按基金代码或名称模糊搜索。"""
    if not query:
        return []
    funds = _load_fund_list()
    q = query.strip().upper()
    results = []
    for f in funds:
        code = str(f.get("基金代码", ""))
        name = str(f.get("基金简称", ""))
        if code.startswith(q) or q in name.upper():
            results.append({
                "基金代码": code,
                "基金简称": f.get("基金简称", ""),
                "基金类型": f.get("基金类型", ""),
            })
        if len(results) >= topk:
            break
    return results


@lru_cache(maxsize=128)
def fund_search_by_code(fund_code: str) -> dict | None:
    """精确按基金代码查找。"""
    funds = _load_fund_list()
    for f in funds:
        if str(f.get("基金代码", "")) == fund_code.strip():
            return {
                "基金代码": f["基金代码"],
                "基金简称": f["基金简称"],
                "基金类型": f["基金类型"],
            }
    return None


def _load_daily_nav() -> pd.DataFrame:
    """加载全量基金日净值数据（每 60 秒刷新一次）。"""
    global _FUND_DAILY_CACHE
    if _FUND_DAILY_CACHE is not None:
        if time.time() - _FUND_DAILY_CACHE.attrs.get("_loaded_at", 0) < 60:
            return _FUND_DAILY_CACHE
    try:
        df = ak.fund_open_fund_daily_em()
        _FUND_DAILY_CACHE = df
        _FUND_DAILY_CACHE.attrs["_loaded_at"] = time.time()
        return df
    except Exception as e:
        logger.warning(f"加载日净值数据失败: {e}")
        return _FUND_DAILY_CACHE if _FUND_DAILY_CACHE is not None else pd.DataFrame()


def fund_realtime_nav(fund_code: str) -> dict | None:
    """获取基金最新净值数据。"""
    df = _load_daily_nav()
    if df.empty:
        return None
    row = df[df.iloc[:, 0] == fund_code]
    if row.empty:
        return None
    row = row.iloc[0]
    cols = list(df.columns)

    def _nav_col(keyword: str) -> str | None:
        """挑最新的非空净值列。

        东财按日期倒序列出两列净值（今日在前、昨日在后）。原先「后写覆盖」
        取到的是最后一列，也就是昨日那列——今日净值已发布时，会把隔夜值
        当成今日值报出去。
        """
        cands = [c for c in cols if c.endswith(keyword)]
        dated: list[tuple[str, str]] = []
        for c in cands:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", c)
            if m:
                dated.append((m.group(1), c))
        dated.sort()
        for _d, c in reversed(dated):
            if _num(row[c]) is not None:
                return c
        for c in cands:
            if _num(row[c]) is not None:
                return c
        return dated[-1][1] if dated else None

    unit_nav_col = _nav_col("单位净值")
    cum_nav_col = _nav_col("累计净值")

    nav_date = None
    if unit_nav_col:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", unit_nav_col)
        if m:
            nav_date = m.group(1)

    result = {
        "基金代码": row.iloc[0],
        "基金简称": row.iloc[1],
        "单位净值": _num(row[unit_nav_col]) if unit_nav_col else None,
        "累计净值": _num(row[cum_nav_col]) if cum_nav_col else None,
        "日增长率": _num(row["日增长率"]) if "日增长率" in cols else None,
        "日增长值": _num(row["日增长值"]) if "日增长值" in cols else None,
        "申购状态": str(row["申购状态"]) if "申购状态" in cols else None,
        "赎回状态": str(row["赎回状态"]) if "赎回状态" in cols else None,
        "手续费": str(row["手续费"]) if "手续费" in cols else None,
        "净值日期": nav_date,
    }
    return result


def fund_historical_nav(fund_code: str, indicator: str = "单位净值走势") -> pd.DataFrame | None:
    """获取基金历史净值走势。"""
    try:
        df = ak.fund_open_fund_info_em(symbol=fund_code, indicator=indicator)
        if df is not None and not df.empty:
            return df
        return None
    except Exception as e:
        logger.warning(f"获取 {fund_code} 历史净值失败: {e}")
        return None


def fund_holdings(fund_code: str, year: str | None = None) -> list[dict] | None:
    """获取基金持仓明细。year 为空时取最新。"""
    try:
        df = ak.fund_portfolio_hold_em(symbol=fund_code, date=year or "")
        if df is not None and not df.empty:
            return df.to_dict("records")
        return None
    except Exception as e:
        logger.warning(f"获取 {fund_code} 持仓失败: {e}")
        return None


def fund_basic_info(fund_code: str) -> dict | None:
    """获取基金基本信息（基金经理、规模、费率等）。"""
    try:
        df = ak.fund_overview_em(symbol=fund_code)
        if df is not None and not df.empty:
            row = df.iloc[0].to_dict()
            for k, v in row.items():
                if isinstance(v, str):
                    row[k] = v.strip()
            return row
        return None
    except Exception as e:
        logger.warning(f"获取 {fund_code} 基本信息失败: {e}")
        return None


def fund_ratings(fund_code: str) -> dict | None:
    """获取基金评级（多家机构）。"""
    try:
        df = ak.fund_rating_all()
        if df.empty:
            return None
        row = df[df["代码"] == fund_code]
        if row.empty:
            return None
        r = row.iloc[0]
        return {
            "上海证券": r.get("上海证券"),
            "招商证券": r.get("招商证券"),
            "济安金信": r.get("济安金信"),
            "晨星评级": r.get("晨星评级"),
            "5星评级家数": r.get("5星评级家数"),
        }
    except Exception as e:
        logger.warning(f"获取 {fund_code} 评级失败: {e}")
        return None


def fund_cumulative_nav_trend(fund_code: str) -> pd.DataFrame | None:
    """获取累计净值走势数据。"""
    try:
        df = ak.fund_open_fund_info_em(symbol=fund_code, indicator="累计净值走势")
        if df is not None and not df.empty:
            return df
        return None
    except Exception as e:
        logger.warning(f"获取 {fund_code} 累计净值失败: {e}")
        return None
