"""估算参数统一结构 + 免费源真实参数获取（Phase 1：消除硬编码）。

数据纪律（约束：绝不编造）：任何估算 / 回退参数都显式标注「来源 + 置信度
(high/medium/low) + 说明」，绝不在报告/看板中静默呈现为真实数据。所有函数
确定性：同一输入 → 同一输出；网络失败返回 fallback 估算并标注来源，不抛异常。

涉及输入：
- 无风险利率：A股 → akshare 中国 10 年期国债收益率；美股/港股 → 美国财政部
  官方每日曲线的 10Y（回退 akshare 汇总的美国 10 年期）。
- Beta：个股 5y 日收益对基准指数（A股沪深300 / 美股标普500 / 港股恒指）OLS 回归。
- ERP：基准指数长期年化收益 − 无风险利率，夹到 [0.02, 0.09]；不足回退行业常量。
- 行业 PE：竞品集合真实 trailing PE 的中位数（n≥3 才 high 置信）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.utils.logger import logger

# 无真实数据时的回退无风险利率（美国 10Y 长期均值，仅作兜底）
FALLBACK_RF = 0.025


def make_estimate(value, source: str, confidence: str,
                  note: str | None = None) -> dict:
    """构造可序列化的估算条目。value=None 时 source 说明缺失原因。"""
    return {
        "value": value,
        "source": source,
        "confidence": confidence,  # high / medium / low
        "note": note,
    }


def _cn_10y_akshare() -> tuple[float, str, str]:
    """中国 10 年期国债收益率（akshare 汇总的中债数据）。"""
    import akshare as ak
    df = ak.bond_zh_us_rate(start_date="20240101")
    col = next((c for c in df.columns if "中国" in str(c) and "10年" in str(c)), None)
    if col is None:
        raise ValueError("akshare 债券数据无中国10年期收益率列")
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    if s.empty:
        raise ValueError("akshare 中国10年期收益率无有效值")
    rate = float(s.iloc[-1]) / 100.0
    if not (0.0 < rate < 0.15):
        raise ValueError(f"中国10年期收益率超出合理范围: {rate}")
    # 日期取「日期」列：默认 RangeIndex 是行号，取它会写出「数据日期 7117」这种假日期
    obs = str(df.loc[s.index[-1], "日期"])[:10] if "日期" in df.columns else ""
    return rate, f"akshare bond_zh_us_rate {col}", obs


def _us_10y_treasury() -> tuple[float, str, str]:
    """美国财政部每日收益率曲线 10Y（官方、零 key、可按日回溯）。"""
    from app.data.sources.treasury_source import TreasurySource
    df = TreasurySource().fetch_yield_curve(months=3)
    if df is None or df.empty or "10 Yr" not in df.columns:
        raise ValueError("Treasury 曲线无 10 Yr 列")
    s = pd.to_numeric(df["10 Yr"], errors="coerce").dropna()
    if s.empty:
        raise ValueError("Treasury 10 Yr 无有效值")
    rate = float(s.iloc[-1]) / 100.0
    if not (0.0 < rate < 0.15):
        raise ValueError(f"美国10年期收益率超出合理范围: {rate}")
    return rate, "US Treasury 每日收益率曲线 10 Yr（官方）", str(s.index[-1])[:10]


def _us_10y_akshare() -> tuple[float, str, str]:
    """同上，经 akshare 汇总的美国 10 年期，Treasury 不可达时的第二通路。"""
    import akshare as ak
    df = ak.bond_zh_us_rate(start_date="20240101")
    col = next((c for c in df.columns if "美国" in str(c) and "10年" in str(c)), None)
    if col is None:
        raise ValueError("akshare 债券数据无美国10年期收益率列")
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    if s.empty:
        raise ValueError("akshare 美国10年期收益率无有效值")
    rate = float(s.iloc[-1]) / 100.0
    if not (0.0 < rate < 0.15):
        raise ValueError(f"美国10年期收益率超出合理范围: {rate}")
    obs = str(df.loc[s.index[-1], "日期"])[:10] if "日期" in df.columns else ""
    return rate, f"akshare bond_zh_us_rate {col}", obs


def get_risk_free_rate(ashare: bool = False, market: str | None = None) -> dict:
    """真实无风险利率：cn → 中国 10 年期国债；us/hk → 美国 10 年期国债。

    美国侧走两级真实通路（财政部官方曲线 → akshare 汇总），两条都失败才回退
    FALLBACK_RF 并写明失败原因。港股现金流以港币计价、港币与美元挂钩，用美国
    利率近似并在 note 里说明——这是近似，不是香港本地利率，不能默默替换。
    """
    mkt = (market or ("cn" if ashare else "us")).lower()
    errors: list[str] = []
    if mkt == "cn":
        try:
            rate, src, obs = _cn_10y_akshare()
            return make_estimate(round(rate, 6), src, "high", f"数据日期 {obs}")
        except Exception as e:
            errors.append(f"akshare 中国10年期: {e}")
    else:
        for label, fetch in (("US Treasury 官方曲线", _us_10y_treasury),
                             ("akshare 美国10年期", _us_10y_akshare)):
            try:
                rate, src, obs = fetch()
                note = f"数据日期 {obs}"
                if mkt == "hk":
                    note += "；港币与美元挂钩（联系汇率），以美国10年期近似港币无风险利率"
                return make_estimate(round(rate, 6), src,
                                     "medium" if mkt == "hk" else "high", note)
            except Exception as e:
                errors.append(f"{label}: {e}")
    detail = "; ".join(errors) or "未知原因"
    logger.warning("无风险利率拉取失败(%s)，回退常量 %s", detail, FALLBACK_RF)
    return make_estimate(FALLBACK_RF, f"fallback常量（免费源不可用: {detail}）", "low",
                         "无真实利率数据，使用长期均值近似")


def compute_beta(price_data, benchmark_df) -> dict:
    """个股 vs 基准指数 5y 日收益 OLS beta（协方差/方差，确定性）。

    对齐两序列日期（inner join）；样本 <60 日返回 None + 说明。
    """
    if price_data is None or benchmark_df is None or price_data.empty or benchmark_df.empty:
        return make_estimate(None, "缺个股或基准指数行情", "low", "无法回归")
    try:
        p = price_data["Close"].astype(float)
        b = benchmark_df["Close"].astype(float)
        df = pd.concat([p.pct_change(), b.pct_change()], axis=1, join="inner").dropna()
        df.columns = ["s", "m"]
        if len(df) < 60:
            return make_estimate(None, f"对齐后样本不足({len(df)}日，至少60)", "low",
                                 "无法回归出可靠 beta")
        var_m = float(df["m"].var(ddof=1))
        if var_m <= 0:
            return make_estimate(None, "基准指数收益无波动", "low")
        beta = float(df["s"].cov(df["m"], ddof=1) / var_m)
        return make_estimate(round(beta, 3),
                             f"日收益 OLS 回归（个股 vs 基准指数, n={len(df)}）", "high")
    except Exception as e:
        logger.warning("beta 回归失败: %s", e)
        return make_estimate(None, f"beta 回归异常: {e}", "low")


def estimate_erp(benchmark_df, rf_value, fallback_erp: float) -> dict:
    """从基准指数长期年化收益估算市场风险溢价（年化收益 − Rf）。

    样本 <3y → 回退行业静态 ERP 并标注；结果夹到 [0.02, 0.09]（置信度 low，
    5y 窗口估算噪声大）。
    """
    if benchmark_df is None or benchmark_df.empty or rf_value is None:
        return make_estimate(fallback_erp, "行业静态 ERP 常量", "medium",
                             "缺基准指数或无风险利率，用行业参考值")
    try:
        b = benchmark_df["Close"].astype(float).dropna()
        n = int(len(b))
        if n < 252 * 3:
            return make_estimate(fallback_erp, "行业静态 ERP 常量", "medium",
                                 f"基准指数仅 {n} 根，不足以估计长期 ERP")
        total = float(b.iloc[-1] / b.iloc[0])
        years = n / 252.0
        ann_ret = (total ** (1 / years)) - 1.0
        erp = ann_ret - rf_value
        erp = min(max(erp, 0.02), 0.09)
        return make_estimate(round(erp, 4),
                             f"基准指数年化收益({ann_ret:.1%}) − Rf({rf_value:.1%})", "low",
                             f"窗口 {years:.0f}y，短期估计噪声大，已夹取到 [2%,9%]")
    except Exception as e:
        logger.warning("ERP 估算失败: %s", e)
        return make_estimate(fallback_erp, "行业静态 ERP 常量", "medium",
                             f"估算异常: {e}")


def estimate_peer_pe(peer_data: list[dict]) -> dict:
    """从竞品集合真实 trailing PE 取中位数作为行业 PE 基准（免费源真实数据）。

    n≥3 → high 置信；0<n<3 → low 置信；无 → None + 说明。peer_data 每项含
    {"pe": float|None, "name": str}。
    """
    vals = []
    for d in peer_data or []:
        v = d.get("pe")
        if v is not None:
            try:
                f = float(v)
                if f > 0:
                    vals.append(f)
            except (TypeError, ValueError):
                pass
    if len(vals) >= 3:
        return make_estimate(round(float(np.median(vals)), 2),
                             f"竞品集合真实 trailing PE 中位数（n={len(vals)}）", "high")
    if vals:
        return make_estimate(round(float(np.median(vals)), 2),
                             f"竞品集合真实 trailing PE 中位数（n={len(vals)}，样本少）", "low",
                             "样本不足 3 家，仅作参考")
    return make_estimate(None, "无竞品 PE 数据", "low",
                         "未获取到竞品真实 PE，回退行业静态均值")
