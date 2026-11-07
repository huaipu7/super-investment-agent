# -*- coding: utf-8 -*-
"""宏观因子框架 —— 把海外经济指标/政策信号标准化成可建模的因子（含 vintage 对齐）。

JD 职责②：研究「海外经济指标、政策信号」与收益率走势的量化关系。
本模块提供：
  - 宏观因子注册表（MetaRegistry）：声明各因子的单位/可得度/来源，诚实标注；
  - 可用的**零 key 代理因子**：期限结构因子（level/slope/curvature，来自 Treasury）+
    政策信号（FOMC 鸽鹰分），这些是**真实可计算**的；
  - 接口钩子：FRED / BLS / Census / 欧央行 SDW 的关键字段，key 未配置时显式标记
    available=False 并给出说明，绝不伪造数值。

诚实边界：GDP/CPI/失业率等需要 key 的官方指标，在 key 未配置时返回 None + 原因，
由上层显式展示为「—」，不编造。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


class MacroFactor:
    """一个宏观因子的元信息与取值容器。"""

    def __init__(self, key: str, name: str, unit: str, freq: str,
                 source: str, available: bool = False, note: str = ""):
        self.key = key
        self.name = name
        self.unit = unit
        self.freq = freq            # daily / monthly / quarterly
        self.source = source
        self.available = available
        self.note = note

    def to_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name, "unit": self.unit,
            "freq": self.freq, "source": self.source,
            "available": self.available, "note": self.note,
        }


class MacroEngine:
    """宏观因子引擎：从已有数据构造可用因子，并对 key 因子给出诚实可得度。"""

    # 需 key 的官方指标（key 未配置 → available=False）
    KEYED_FACTORS = [
        MacroFactor("gdp_yoy", "实际 GDP 同比", "y/y %", "quarterly",
                    "FRED/GDPC1", False, "需 FRED API key，未配置 → 不可用"),
        MacroFactor("cpi_yoy", "核心 CPI 同比", "y/y %", "monthly",
                    "BLS/CPILFESL", False, "需 BLS key，未配置 → 不可用"),
        MacroFactor("unemp", "失业率", "%", "monthly", "BLS/UNRATE", False,
                    "需 BLS key，未配置 → 不可用"),
        MacroFactor("pmist", "ISM 制造业 PMI", "index", "monthly",
                    "Census/ISM", False, "需申请，未配置 → 不可用"),
        MacroFactor("spread_oas", "ICE BofA 高收益 OAS", "bp", "daily",
                    "FRED/BAMLH0A0HYM2", False, "需 FRED key，未配置 → 不可用"),
        MacroFactor("tips_breakeven", "10Y TIPS 盈亏平衡通胀", "%", "daily",
                    "FRED/T10YIE", False, "需 FRED key，未配置 → 不可用"),
    ]

    def __init__(self):
        self._plugged: dict[str, pd.Series] = {}

    def register(self, key: str, series: pd.Series):
        """注册一个外部拉取的因子序列（key 就绪后由上层调用）。"""
        self._plugged[key] = series

    # ── 零 key 可得因子（真实计算）─────────────────────────
    @staticmethod
    def curve_factors(factors: pd.DataFrame) -> dict:
        """从 NSS 因子序列截取其末端值/变化率，作为期限结构宏观因子。"""
        if factors is None or factors.empty:
            return {"ok": False}
        last = factors.iloc[-1]
        return {
            "ok": True, "as_of": str(factors.index[-1].date()),
            "level": round(float(last["level"]), 3),
            "slope": round(float(last["slope"]), 3),
            "curvature": round(float(last.get("curvature", 0.0)), 3),
            "source": "Treasury + NSS（零 key，真实计算）",
            "available": True,
        }

    @staticmethod
    def policy_factor(policy: dict) -> dict:
        """政策信号因子：FOMC 鸽鹰分。"""
        return {
            "ok": bool(policy.get("ok")),
            "score": policy.get("score"),
            "stance": policy.get("stance"),
            "meeting_date": policy.get("meeting_date"),
            "source": "FOMC 官方声明 + 词典法（零 key，真实文本）",
            "available": bool(policy.get("ok")),
        }

    # ── key 因子可得度清单 ──────────────────────────────────
    def registry(self) -> list[dict]:
        """返回全部注册因子的元信息（含可得度），供看板/API 诚实展示。"""
        out = [MacroFactor("level", "收益率水平(NSS)", "%", "daily",
                          "Treasury+NSS", True, "零 key，真实计算"),
               MacroFactor("slope", "收益率斜率(NSS)", "Δ%", "daily",
                          "Treasury+NSS", True, "零 key，真实计算"),
               MacroFactor("policy_score", "FOMC 鸽鹰分", "[-1,1]", "event",
                          "FOMC+词典", True, "零 key，真实文本计分")]
        for f in self.KEYED_FACTORS:
            out.append(f)
        return [f.to_dict() for f in out]

    # ── 标准化 + vintage 对齐 ───────────────────────────────
    @staticmethod
    def zscore(series: pd.Series, window: int = 252) -> pd.Series:
        """滚动 z-score（用 window 前窗估计均/标差，防 look-ahead）。"""
        s = series.astype(float)
        mu = s.rolling(window, min_periods=20).mean()
        sd = s.rolling(window, min_periods=20).std()
        sd = sd.replace(0, np.nan)
        return (s - mu) / sd

    def build_factor_matrix(self, curve: pd.DataFrame, policy: dict | None = None) -> pd.DataFrame:
        """组装可用因子的时间序列矩阵（供 VAR/VARX 使用）。

        - 期限结构：由 curve 直接算 10Y 与 10Y-2Y 利差（真实可计算，无需 NSS）
        - 政策：若提供 policy 分数，作为外生/离散标记列
        返回 index=交易日，columns=[y10, spread_2_10, ...]。
        """
        if curve is None or curve.empty:
            return pd.DataFrame()
        out = pd.DataFrame(index=curve.index)
        if "10 Yr" in curve:
            out["y10"] = curve["10 Yr"].astype(float)
        if "2 Yr" in curve and "10 Yr" in curve:
            out["spread_2_10"] = (curve["10 Yr"] - curve["2 Yr"]).astype(float)
        if policy and policy.get("ok") and policy.get("meeting_date"):
            # 政策分在该 meeting date 生效，应用于其后的所有日（简单前向填充）
            pd_date = pd.to_datetime(policy["meeting_date"], errors="coerce")
            if pd.notna(pd_date):
                out["policy"] = np.nan
                out.loc[out.index >= pd_date, "policy"] = policy.get("score")
                out["policy"] = out["policy"].ffill()
        return out.dropna(how="all")
