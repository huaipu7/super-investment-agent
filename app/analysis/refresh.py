"""增量刷新：只重取行情与「由现价决定」的部分，不调 LLM。

全量管线一次 3–4 分钟，其中几乎全部时间花在新闻/会议/SWOT/竞争/地缘/供应链这些
LLM 章节上——而这些一天之内不会变。每天真正变的是行情、技术指标，以及由现价决定
的估值结论。

这里只重算五处（全部确定性计算）：
    price_history                          ← 重取 5y 行情
    financial_analysis.current_price       ← 最新有效收盘
    financial_analysis.valuation_context.current_pe
                                           ← 与 financial.py 同一公式
    valuation_analysis.dcf.upside / summary ← 既有 fair_value 与新价
    trade_plan                             ← 技术面 + 估值锚重新合成

`pe_percentile` **不重算**：它需要逐年均价序列与对齐后的财报 DataFrame，产物里没有；
硬算半个只会得到混血数据。它连同其余章节一起进 `refresh.carried_over`，逐条写明
「这是哪一天的结果」。

沿用章节里的数值保持原有 provenance 与 as_of——它们是那个日期的真实事实，不是新事实。
"""
from __future__ import annotations

import copy
import json
import time
from datetime import datetime
from pathlib import Path

from app.analysis.price_series import build_price_history
from app.analysis.pricing import build_pricing
from app.analysis.trade_plan import build_trade_plan
from app.analysis.valuation import (
    FAIR_VALUE_PRICE_BAND,
    band_degrade_reason,
    build_valuation_summary,
)
from app.trust.completeness import check_completeness
from app.trust.sanitize import sanitize_result
from app.utils.logger import logger

# 整段沿用、不重算的章节
CARRIED_SECTIONS = (
    "company_info", "sotp_analysis", "market_analysis", "competitive_analysis",
    "swot_analysis", "fact_checks", "geopolitical_analysis",
    "supply_chain_analysis", "news_analysis", "meeting_analysis",
    "knowledge", "sector_adapter",
)

# 本次重算的字段（写进产物，供读者核对「哪些是新的」）
RECOMPUTED = (
    "price_history",
    "pricing",
    "financial_analysis.current_price",
    "financial_analysis.valuation_context.current_pe",
    "valuation_analysis.dcf.upside",
    "valuation_analysis.summary",
    "trade_plan",
)


def _latest_metric(raw_metrics: dict | None, key: str) -> float | None:
    """raw_metrics[key] 里最后一个非空值。"""
    for s in reversed((raw_metrics or {}).get(key) or []):
        if isinstance(s, dict) and s.get("value") is not None:
            try:
                return float(s["value"])
            except (TypeError, ValueError):
                return None
    return None


def _current_pe(close, shares, net_income) -> float | None:
    """与 financial.py `_compute_pe_percentile` 同一公式：price × shares ÷ 净利。

    同样 `round(..., 2)`——全量管线写 22.10，刷新写 22.103340689355033，
    同一个指标两条路径精度不同，跨 run 对比会凭空多出一条「改值」。
    """
    try:
        if close is None or not shares or float(shares) <= 0:
            return None
        if net_income is None or float(net_income) <= 0:
            return None
        return round(float(close) * float(shares) / float(net_income), 2)
    except (TypeError, ValueError):
        return None


def _baseline_pe_date(prev: dict, base_generated_at: str | None, *, depth: int = 0) -> str:
    """基线里那个 `pe_percentile` 的时点：优先已有标注，其次基线现价日期。

    刷新产物自己带 `pe_percentile_as_of`（分位可能比现价更旧），直接用；
    全量产物没有这个键，退到它的现价日期。

    边界情况：本改动之前生成的刷新产物既没有 `pe_percentile_as_of`、也没有 `pricing`，
    它的分位其实来自再上一份产物——直接取它的现价日期会把分位标新一个交易日。
    这时沿 `refresh.base_artifact` 回溯（最多 3 层）找真正算分位的那份产物。
    """
    vc = (prev.get("financial_analysis") or {}).get("valuation_context")
    if isinstance(vc, dict) and isinstance(vc.get("pe_percentile_as_of"), str):
        return vc["pe_percentile_as_of"]
    pricing = prev.get("pricing")
    if isinstance(pricing, dict) and isinstance(pricing.get("price_as_of"), str):
        return pricing["price_as_of"]

    ref = (prev.get("refresh") or {}).get("base_artifact")
    if isinstance(ref, str) and ref and depth < 3:
        try:
            base = json.loads(Path(ref).read_text(encoding="utf-8"))
            if isinstance(base, dict) and base:
                return _baseline_pe_date(base, None, depth=depth + 1)
        except (OSError, ValueError):
            pass

    dates = (prev.get("price_history") or {}).get("dates")
    if isinstance(dates, list) and dates and isinstance(dates[-1], str):
        return dates[-1]
    return (base_generated_at or "")[:10] or "?"


def refresh_price(
    prev: dict,
    *,
    price_data=None,
    market=None,
    base_ref: str | None = None,
    base_generated_at: str | None = None,
) -> dict:
    """基于上一份产物做行情增量刷新。

    Args:
        prev: 上一份管线产物（全量或刷新产物均可）。
        price_data: 可选行情 DataFrame；None 时按 ticker 实时取 5y。
        market: 可选 MarketData 实例（测试注入用）。
        base_ref: 基线产物路径，写进 `refresh.base_artifact`。
        base_generated_at: 基线生成时刻（ISO 串），写进 `refresh.base_generated_at`。

    Raises:
        ValueError: 基线不完整，或取不到行情（宁可失败，也不产出半成品）。
    """
    if not isinstance(prev, dict) or not prev:
        raise ValueError("缺少基线产物")
    ticker = prev.get("ticker")
    if not ticker:
        raise ValueError("基线产物缺少 ticker")

    started = time.time()

    if price_data is None:
        from app.data.router import get_market_data

        price_data = (market or get_market_data()).get_history(ticker, "5y")

    price_history = build_price_history(price_data)
    if price_history.get("available") is not True:
        raise ValueError(f"取不到 {ticker} 的行情，未刷新：{price_history.get('reason')}")

    closes = price_history.get("close") or []
    if not closes:
        raise ValueError(f"{ticker} 行情无收盘价，未刷新")
    close = float(closes[-1])
    pricing = build_pricing(price_data)
    # 与全量管线同一口径；取不到时退回序列末日，绝不留空
    price_date = pricing.get("price_as_of") or (price_history.get("dates") or [""])[-1]

    new = copy.deepcopy(prev)
    # report_id / request_id 标识的是「某一份报告」，刷新产物是新的报告，由调用方重新分配；
    # 留着会让反馈归因到旧的内容哈希上。
    for key in ("refresh", "data_quality", "completeness", "report_id", "request_id"):
        new.pop(key, None)
    new["price_history"] = price_history
    new["pricing"] = pricing

    # ── 财务：只动价格与其派生的 PE ──
    fin = new.get("financial_analysis")
    if not isinstance(fin, dict):
        fin = {}
        new["financial_analysis"] = fin
    fin["current_price"] = close

    shares = (new.get("company_info") or {}).get("shares_outstanding")
    net_income = _latest_metric(fin.get("raw_metrics"), "NET_INCOME")
    vc = fin.get("valuation_context")
    pe_date = _baseline_pe_date(prev, base_generated_at)
    if isinstance(vc, dict):
        vc["current_pe"] = _current_pe(close, shares, net_income)
        # 分位不重算，但必须自报时点：同一个 dict 里两个日期不同的数，读者要能分辨
        if vc.get("pe_percentile") is not None:
            vc["pe_percentile_as_of"] = pe_date

    # ── 估值：fair_value 不动，空间与摘要跟着新价重算 ──
    va = new.get("valuation_analysis")
    if not isinstance(va, dict):
        va = {}
        new["valuation_analysis"] = va
    dcf = va.get("dcf")
    if isinstance(dcf, dict) and dcf.get("fair_value") is not None:
        fv = float(dcf["fair_value"])
        lo, hi = FAIR_VALUE_PRICE_BAND
        ratio = fv / close
        if not (lo <= ratio <= hi):
            # 基线里的 fair_value 本身就不可用（旧版无闸产物），刷新不能把它洗白
            dcf["fair_value"] = None
            dcf["upside"] = None
            dcf["degraded"] = True
            dcf["reason"] = band_degrade_reason(fv, close, ratio)
            logger.warning("%s", dcf["reason"])
        else:
            dcf["upside"] = (fv - close) / close
    va["summary"] = build_valuation_summary(
        dcf, va.get("relative_valuation"), close,
        va.get("supply_chain_risk_premium") or 0.0)

    # ── 交易信号：技术面来自新行情，估值锚来自上面重算后的结果 ──
    swot = new.get("swot_analysis")
    try:
        new["trade_plan"] = build_trade_plan(
            price_data=price_data,
            valuation_result=va,
            swot_rating=(swot.get("rating") if isinstance(swot, dict) else None),
            company_info=new.get("company_info") or {},
            financial_result=fin,
        )
    except Exception as e:
        logger.warning("交易信号重算失败，保留原值: %s", e)

    # ── 沿用的部分逐条记账 ──
    elapsed = time.time() - started
    base_date = (base_generated_at or "")[:10] or "?"
    carried = [
        {"section": "financial_analysis", "as_of": base_date,
         "note": "三表与比率沿用，仅更新 current_price / current_pe"},
        {"section": "financial_analysis.valuation_context.pe_percentile", "as_of": pe_date,
         "note": f"历史分位需要年报均价序列，增量刷新不重算；分位沿用 {pe_date}，"
                 "现价与 PE 已按新行情重算"},
    ]
    carried += [
        {"section": s, "as_of": base_date, "note": "沿用上次分析结果，未重算"}
        for s in CARRIED_SECTIONS if s in new
    ]
    new["refresh"] = {
        "mode": "price",
        "refreshed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "base_artifact": base_ref or "",
        "base_generated_at": base_generated_at or "",
        "effective_price_date": price_date,
        "recomputed": list(RECOMPUTED),
        "carried_over": carried,
        "elapsed": f"{elapsed:.1f}s",
    }
    new["pipeline_time"] = round(elapsed, 2)

    # 与全量管线同一条净化 / 完整性链路，不另开旁路
    new = sanitize_result(new)
    new["completeness"] = check_completeness(new)
    if not new["completeness"]["complete"]:
        logger.warning("刷新后核心章节不完整: %s", new["completeness"]["gaps"])
    logger.info("行情刷新完成 %s: 价格 %s（%s）, 耗时 %.1fs",
                ticker, close, price_date, elapsed)
    return new


def summarize_refresh(result: dict) -> dict:
    """给 UI / CLI 用的刷新摘要（只读产物里的 refresh 块）。"""
    r = result.get("refresh") if isinstance(result, dict) else None
    if not isinstance(r, dict):
        return {"refreshed": False}
    return {
        "refreshed": True,
        "mode": r.get("mode"),
        "refreshed_at": r.get("refreshed_at"),
        "base_generated_at": r.get("base_generated_at"),
        "effective_price_date": r.get("effective_price_date"),
        "recomputed": r.get("recomputed") or [],
        "carried_over": r.get("carried_over") or [],
    }
