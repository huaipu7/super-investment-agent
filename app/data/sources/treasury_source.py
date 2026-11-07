# -*- coding: utf-8 -*-
"""美国财政部每日收益率曲线数据源（零 key，官方）。

真实观测值：每日收益率曲线本身就是当天的真实观测，按日期 query 即为
point-in-time（用某历史日的曲线，就是当天真实值），不走「最新值改写」。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from app.data.cache import get_cache, CACHE_TTL
from app.data.sources.fi_http import get
from app.data.vintage import build_vintage

SAFE_DATE_COL = "Date"
_CACHE_TTL = 3600  # 一小时（日更数据，足够）


class TreasurySource:
    """美元国债每日收益率曲线。

    返回 DataFrame：index=交易日(DatetimeIndex)，columns=各期(如 '1 Mo'/'10 Yr')，
    值为收益率(%)，缺失为 NaN。
    """

    def __init__(self):
        self.cache = get_cache()

    def fetch_yield_curve(self, months: int = 13, as_of=None) -> pd.DataFrame:
        """拉取近 months 个月每日曲线。as_of 非空时截断到 ≤as_of（point-in-time）。"""
        ck = ("treasury_curve", months)
        df = self.cache.get_dataframe(*ck)
        if df is None:
            df = self._download(months)
            if df is not None and not df.empty:
                self.cache.set_dataframe(df, _CACHE_TTL, *ck)
        if df is None:
            df = pd.DataFrame()
        df = df.copy()
        if as_of is not None:
            a = pd.to_datetime(str(as_of), errors="coerce")
            if pd.notna(a):
                df = df.loc[df.index <= a]
        df.attrs["vintage"] = build_vintage(
            requested_as_of=str(as_of) if as_of else None,
            data_observation_date=str(df.index[-1].date()) if not df.empty else None,
            source="treasury",
            is_point_in_time=True,
            note="美国财政部每日收益率曲线真实观测值（按日期点对点）",
        )
        return df

    def _download(self, months: int) -> pd.DataFrame:
        today = dt.date.today()
        # Treasury 已改为「按年」下载：field_tdr_date_value=YYYY + /YYYY/all（按月旧格式已 404）。
        # 覆盖近 months 个月需当年 + 往前 n_years 个年；取回后用滑窗缩小到所需窗口。
        n_years = max(2, (months + 11) // 12 + 1)
        frames = []
        consec_fail = 0
        for y in range(today.year - n_years + 1, today.year + 1):
            url = (
                "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                f"daily-treasury-rates.csv/{y}/all"
                f"?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv"
            )
            try:
                r = get(url, timeout=8)
                r.raise_for_status()
                f = pd.read_csv(pd.io.common.StringIO(r.text))
                frames.append(f)
                consec_fail = 0
            except Exception:
                consec_fail += 1
                if consec_fail >= 4:  # 连续失败过早退出（年度请求少，4 次已足够）
                    break
                continue
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        if SAFE_DATE_COL not in df.columns:
            return pd.DataFrame()
        df = df.drop_duplicates(subset=[SAFE_DATE_COL], keep="last")
        df[SAFE_DATE_COL] = pd.to_datetime(df[SAFE_DATE_COL], errors="coerce")
        df = df.dropna(subset=[SAFE_DATE_COL]).sort_values(SAFE_DATE_COL)
        df = df.set_index(SAFE_DATE_COL)
        df = df.apply(pd.to_numeric, errors="coerce")
        df = df.dropna(how="all", axis=1)
        if months and not df.empty:  # 按当前月滑窗截取最近 months 个月
            df = df.loc[df.index >= today - pd.DateOffset(months=months)]
        df.attrs["source"] = "US Treasury daily yield curve"
        return df
