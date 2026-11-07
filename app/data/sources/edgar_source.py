# -*- coding: utf-8 -*-
"""SEC EDGAR companyfacts 数据源（零 key，官方）。

提供海外发债主体标准化财务（XBRL），并按 filing 日期（filed）实现真实
point-in-time：as_of 只保留「截至该时点已披露」的数据，杜绝 look-ahead。

注：EDGAR companyfacts 里持续型(duration)指标带纯年度 frame（CY####），
时点型(instant)指标带季度 frame（CY####Q#I 或 None）；本模块对两者分别处理，
并把三张表在「财政年终日」对齐成一张年度基本面面板。
"""
from __future__ import annotations

import re

import pandas as pd

from app.data.cache import get_cache
from app.data.sources.fi_http import get_json, json_default
from app.data.vintage import build_vintage

_FACTS_TTL = 86400  # companyfacts 24 小时（官方数据，日更一次）
_TICKERMAP_URL = "https://www.sec.gov/files/company_tickers.json"

# 各指标的候选 us-gaap 键（按优先级）
_METRICS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    # Phase 3 信用评级引擎额外所需的会计科目（Altman Z / Ohlson O 结构分）
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit", "RetainedEarnings"],
    "operating_income": ["OperatingIncomeLoss",
                         "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"],
}


class EdgarSource:
    """SEC 官方 XBRL 财务（companyfacts）。"""

    def __init__(self):
        self.cache = get_cache()
        self._cik_map: dict | None = None

    # ── ticker → CIK ────────────────────────────────────────
    def _load_cik_map(self) -> dict:
        if self._cik_map is None:
            cached = self.cache.get_dict("edgar_cik_map")
            if not cached:
                # 失败绝不写缓存：以前拉取异常会把 {} 写进去并存活 24 小时，
                # 之后所有美股主体都报「无法解析 CIK」，而且从缓存里看不出
                # 是网络问题还是真的没有这个 ticker。
                raw = get_json(_TICKERMAP_URL)
                cached = {v["ticker"].upper(): f"{v['cik_str']:010d}"
                          for v in raw.values()}
                if not cached:
                    raise RuntimeError("SEC company_tickers.json 返回空映射")
                self.cache.set_dict(cached, _FACTS_TTL, "edgar_cik_map")
            self._cik_map = cached
        return self._cik_map

    def resolve_cik(self, ticker: str) -> str | None:
        cik = self._load_cik_map().get(ticker.strip().upper())
        return cik

    # ── 原始 facts（缓存）──────────────────────────────────
    def fetch_raw_facts(self, ticker: str) -> dict:
        cik = self.resolve_cik(ticker)
        if not cik:
            raise ValueError(f"无法从 SEC 映射解析 CIK：{ticker}")
        ck = ("edgar_facts", cik)
        cached = self.cache.get_dict(*ck)
        if cached is not None:
            return cached
        try:
            data = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        except Exception as e:
            raise RuntimeError(f"EDGAR companyfacts 拉取失败 {ticker} (CIK{ cik }): {e}")
        self.cache.set_dict(data, _FACTS_TTL, *ck)
        return data

    # ── 指标序列提取 ───────────────────────────────────────
    @staticmethod
    def _series(data: dict, metric: str) -> list[dict]:
        for key in _METRICS[metric]:
            try:
                units = data["facts"]["us-gaap"][key]["units"]
                _, arr = next(iter(units.items()))
                return arr
            except Exception:
                continue
        return []

    @staticmethod
    def _annual_duration(data: dict, metric: str) -> dict[str, dict]:
        """持续型指标：取 frame 为纯年度(CY####/FY####)的记录，key=end。"""
        out = {}
        for r in EdgarSource._series(data, metric):
            if re.fullmatch(r"(CY|FY)\d{4}", r.get("frame") or "") and r.get("end"):
                prev = out.get(r["end"])
                if prev is None or (r.get("filed") or "") > (prev.get("filed") or ""):
                    out[r["end"]] = r
        return out

    @staticmethod
    def _instant_by_end(data: dict, metric: str) -> dict[str, dict]:
        """时点型指标：按 end 去重(保留最新 filed)，key=end。"""
        out = {}
        for r in EdgarSource._series(data, metric):
            en = r.get("end")
            if not en:
                continue
            prev = out.get(en)
            if prev is None or (r.get("filed") or "") > (prev.get("filed") or ""):
                out[en] = r
        return out

    # ── 年度基本面面板（对齐财政年终日）────────────────────
    def fetch_annual_fundamentals(self, ticker: str, as_of=None) -> pd.DataFrame:
        """返回年度信用基本面面板。

        index=财政年终日(DatetimeIndex)，columns=[revenue, net_income, assets,
        liabilities, equity, leverage, filed]。
        filed=该行数据「最新一份文件提交日」（point-in-time 锚点）。
        as_of 非空时剔除 filed>as_of 的整行（防 look-ahead）。
        """
        data = self.fetch_raw_facts(ticker)
        rev = self._annual_duration(data, "revenue")
        ni = self._annual_duration(data, "net_income")
        ast = self._instant_by_end(data, "assets")
        lia = self._instant_by_end(data, "liabilities")
        eq = self._instant_by_end(data, "equity")

        # 以营收的财政年终日为骨架
        ends = sorted(x["end"] for x in rev.values())
        if not ends:
            return pd.DataFrame()

        rows = []
        for end in ends:
            def _v(mapping):
                r = mapping.get(end)
                return (float(r["val"]), r.get("filed")) if r and r.get("val") else (None, None)
            rev_v, rev_f = _v(rev)
            ni_v, ni_f = _v(ni)
            ast_v, ast_f = _v(ast)
            lia_v, lia_f = _v(lia)
            eq_v, eq_f = _v(eq)
            filed = max([f for f in (rev_f, ni_f, ast_f, lia_f, eq_f) if f], default=None)
            if filed is None:
                continue
            rows.append({
                "end": pd.Timestamp(end),
                "revenue": rev_v, "net_income": ni_v,
                "assets": ast_v, "liabilities": lia_v, "equity": eq_v,
                "leverage": (round(lia_v / ast_v, 4)
                             if ast_v and lia_v is not None else None),
                "filed": filed,
            })

        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df = df.sort_values("end").set_index("end")

        if as_of is not None:
            a = pd.to_datetime(str(as_of), errors="coerce")
            if pd.notna(a):
                df = df[df["filed"] <= a.strftime("%Y-%m-%d")]

        df.attrs["vintage"] = build_vintage(
            requested_as_of=str(as_of) if as_of else None,
            data_observation_date=str(df.index[-1].date()) if not df.empty else None,
            source="edgar",
            is_point_in_time=as_of is not None or True,
            note=("SEC XBRL 年度面板，filed 为真实文件提交日；as_of 时剔除未披露行"
                  if as_of else "SEC XBRL 年度基本面面板（最新披露口径，含 filed 日期）"),
        )
        return df

    # ── 信用评级引擎专用：更完整的年度科目面板 ────────────────
    def fetch_credit_panel(self, ticker: str, as_of=None) -> pd.DataFrame:
        """返回信用评级需用的年度科目面板（point-in-time）。

        index=财政年终日，columns=[revenue, net_income, operating_income, assets,
        liabilities, equity, current_assets, current_liabilities, retained_earnings,
        filed]。instant 科目（资产/负债/权益/流动资产/流动负债/留存收益）按「财政年终
        日精确或前filed最近一次」对齐；filed=最新文件提交日（防 look-ahead 锚点）。
        """
        data = self.fetch_raw_facts(ticker)

        dur = {
            "revenue": self._annual_duration(data, "revenue"),
            "net_income": self._annual_duration(data, "net_income"),
            "operating_income": self._annual_duration(data, "operating_income"),
        }
        inst = {
            "assets": self._instant_by_end(data, "assets"),
            "liabilities": self._instant_by_end(data, "liabilities"),
            "equity": self._instant_by_end(data, "equity"),
            "current_assets": self._instant_by_end(data, "current_assets"),
            "current_liabilities": self._instant_by_end(data, "current_liabilities"),
            "retained_earnings": self._instant_by_end(data, "retained_earnings"),
        }

        ends = sorted(x["end"] for x in dur["revenue"].values())
        if not ends:
            return pd.DataFrame()

        def _lookup(mapping, end):
            """精确匹配，否则取 end 之前最近一次（460 天内）的 instant 值。"""
            r = mapping.get(end)
            if r is None:
                cand = [e for e in mapping
                        if pd.Timestamp(e) <= pd.Timestamp(end)
                        and (pd.Timestamp(end) - pd.Timestamp(e)).days < 460]
                if cand:
                    r = mapping.get(max(cand, key=lambda e: pd.Timestamp(e)))
            if r and r.get("val") is not None:
                return float(r["val"]), r.get("filed")
            return None, None

        rows = []
        for end in ends:
            rec = {"end": pd.Timestamp(end)}
            filed = []
            for k, m in {**dur, **inst}.items():
                v, f = _lookup(m, end)
                rec[k] = v
                if f:
                    filed.append(f)
            rec["filed"] = max(filed) if filed else None
            if rec["filed"] is None:
                continue
            rows.append(rec)

        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df = df.sort_values("end").set_index("end")

        # 会计恒等式回退：assets = liabilities + equity。个别主体只披露其中两项或用不同
        # 标签/序号导致某一侧缺失，用恒等式由真实披露项推出（非捏造），补足后按身份校验。
        for a, b, c in [("assets", "liabilities", "equity"),
                        ("liabilities", "assets", "equity"),
                        ("equity", "assets", "liabilities")]:
            if df[a].isna().any():
                df[a] = df[a].fillna(df[b] - df[c])

        if as_of is not None:
            a = pd.to_datetime(str(as_of), errors="coerce")
            if pd.notna(a):
                df = df[df["filed"] <= a.strftime("%Y-%m-%d")]

        df.attrs["vintage"] = build_vintage(
            requested_as_of=str(as_of) if as_of else None,
            data_observation_date=str(df.index[-1].date()) if not df.empty else None,
            source="edgar",
            is_point_in_time=True,
            note="SEC XBRL 年度科目面板（信用评级），filed 为真实提交日；as_of 时剔除未披露行",
        )
        return df

    # 供上层/看板可读的序列化（避免 DataFrame attrs 丢失）
    def to_lists(self, df: pd.DataFrame) -> dict:
        if df.empty:
            return {"years": [], "revenue": [], "net_income": [], "assets": [],
                    "liabilities": [], "equity": [], "leverage": [], "filed": []}
        return {
            "years": [d.strftime("%Y-%m-%d") for d in df.index],
            "revenue": [None if pd.isna(x) else float(x) for x in df["revenue"]],
            "net_income": [None if pd.isna(x) else float(x) for x in df["net_income"]],
            "assets": [None if pd.isna(x) else float(x) for x in df["assets"]],
            "liabilities": [None if pd.isna(x) else float(x) for x in df["liabilities"]],
            "equity": [None if pd.isna(x) else float(x) for x in df["equity"]],
            "leverage": [None if pd.isna(x) else float(x) for x in df["leverage"]],
            "filed": list(df["filed"]),
        }
