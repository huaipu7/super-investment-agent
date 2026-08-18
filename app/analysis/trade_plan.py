"""投资策略与交易信号合成 —— 技术指标 + 估值锚混合，确定性、无 LLM。

输入 = 深度研报管线的既有输出（历史行情 price_data、估值 valuation_result、
SWOT 评级、公司信息、财务结果）。输出结构化交易计划：
综合评级 / 进场区间 / 止损位 / 止盈位 / 仓位建议 / 理由清单。

数据纪律（约束：绝不编造）：任何关键输入缺失时对应字段显式 None/"—"，并在
reasons 注明缺失来源；数值全部来自真实历史行情与真实 DCF 计算。合规：交易计划
文字会如实含「进场/止损位/止盈位/仓位」等词，由报告渲染侧走 apply_gate 落复核
队列 + 附免责声明（沿用现有机制，见 app/compliance/rules.py）。
"""
from __future__ import annotations

import pandas as pd

from app.analysis.technical import compute_indicators

# 综合评级枚举（确定性阈值映射）
RATING_LABELS = ["强烈看多", "看多", "中性", "看空", "强烈看空"]

# 仓位建议枚举（按综合置信度映射）
POSITION_HINTS = ["观望", "轻仓", "半仓", "标准仓"]

# 52 周位置分档（低位=潜在低估，高位=追高风险）
_LOW_POS = 0.30   # pos_52w < 0.30 → 低位
_HIGH_POS = 0.75  # pos_52w > 0.75 → 高位


def _safe(v):
    """None/NaN → None，否则 float。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _fmt_price(v, nd: int = 2) -> str:
    return "—" if v is None else f"{v:,.{nd}f}"


def _fmt_pct(v, nd: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{nd}f}%"


def build_trade_plan(price_data=None, valuation_result: dict | None = None,
                     swot_rating: str | None = None, company_info: dict | None = None,
                     financial_result: dict | None = None) -> dict:
    """合成投资策略与交易信号。

    Args:
        price_data: MarketData.get_history() 返回的 OHLCV DataFrame（或 None）
        valuation_result: pipeline 的 valuation_analysis（含 dcf/relative_valuation）
        swot_rating: swot_analysis.rating（"看好/中性/谨慎"等，LLM 生成，作辅助参考）
        company_info: pipeline 的 company_info
        financial_result: pipeline 的 financial_analysis（含 current_price）

    Returns:
        dict: {
            "available": bool,
            "reason": str|None,
            "current_price": float|None,
            "indicators": dict,            # technical.compute_indicators 输出
            "valuation": {"dcf_fair_value", "dcf_upside", "pe_company",
                          "pe_industry_avg", "pe_premium"},
            "rating": str,                 # 强烈看多/看多/中性/看空/强烈看空
            "score_breakdown": {           # 确定性给分明细（可追溯）
                "tech_score": int, "val_score": int, "total": int,
                "factors": {"trend": str, "rsi": str, "macd": str,
                            "position": str, "valuation": str},
            },
            "entry_zone": [float|None, float|None],   # [下限, 上限]
            "stop_loss": float|None,
            "take_profit": float|None,
            "position_hint": str,
            "swot_rating": str|None,
            "reasons": list[str],
        }
    """
    reasons: list[str] = []
    out: dict = {"available": True, "reason": None, "swot_rating": swot_rating}

    # ── 当前价格 ──
    current_price = None
    if financial_result:
        current_price = _safe(financial_result.get("current_price"))
    if current_price is None and price_data is not None and len(price_data) > 0:
        current_price = _safe(price_data["Close"].iloc[-1])
    out["current_price"] = current_price

    # ── 技术指标 ──
    ind = compute_indicators(price_data) if price_data is not None else {
        "available": False, "reason": "无历史行情数据"}
    out["indicators"] = ind
    if not ind.get("available"):
        reasons.append(f"技术面：{ind.get('reason') or '不可用'}")

    # ── 估值锚（DCF + PE）──
    val: dict = {"dcf_fair_value": None, "dcf_upside": None,
                 "pe_company": None, "pe_industry_avg": None, "pe_premium": None}
    va = valuation_result or {}
    dcf = va.get("dcf") if isinstance(va.get("dcf"), dict) else None
    if dcf:
        val["dcf_fair_value"] = _safe(dcf.get("fair_value"))
        val["dcf_upside"] = _safe(dcf.get("upside"))
    rel = va.get("relative_valuation") if isinstance(va.get("relative_valuation"), dict) else None
    if rel and isinstance(rel.get("pe"), dict):
        pe = rel["pe"]
        val["pe_company"] = _safe(pe.get("company"))
        val["pe_industry_avg"] = _safe(pe.get("industry_avg"))
        val["pe_premium"] = _safe(pe.get("premium"))
    out["valuation"] = val

    # ── 确定性给分 ──
    f = {}  # factors
    tech_score = 0
    trend = ind.get("trend")
    if trend == "多头":
        tech_score += 1
        f["trend"] = "MA20>MA60 多头"
    elif trend == "空头":
        tech_score -= 1
        f["trend"] = "MA20<MA60 空头"
    elif trend == "粘合":
        f["trend"] = "MA20≈MA60 粘合"
    else:
        f["trend"] = "数据不足"

    rsi = _safe(ind.get("rsi14"))
    if rsi is not None:
        if rsi <= 30:
            tech_score += 1
            f["rsi"] = f"RSI={rsi:.0f} 超卖"
        elif rsi >= 70:
            tech_score -= 1
            f["rsi"] = f"RSI={rsi:.0f} 超买"
        else:
            f["rsi"] = f"RSI={rsi:.0f} 中性"
    else:
        f["rsi"] = "数据不足"

    macd_hist = _safe(ind.get("macd_hist"))
    if macd_hist is not None:
        if macd_hist > 0:
            tech_score += 1
            f["macd"] = f"MACD 柱 {macd_hist:+.3f} 红柱"
        elif macd_hist < 0:
            tech_score -= 1
            f["macd"] = f"MACD 柱 {macd_hist:+.3f} 绿柱"
        else:
            f["macd"] = "MACD 零轴"
    else:
        f["macd"] = "数据不足"

    pos = _safe(ind.get("pos_52w"))
    if pos is not None:
        if pos <= _LOW_POS:
            tech_score += 1
            f["position"] = f"52周位置 {pos:.0%} 低位"
        elif pos >= _HIGH_POS:
            tech_score -= 1
            f["position"] = f"52周位置 {pos:.0%} 高位"
        else:
            f["position"] = f"52周位置 {pos:.0%} 中位"
    else:
        f["position"] = "数据不足"

    val_score = 0
    ups = val["dcf_upside"]
    if ups is not None:
        if ups >= 0.20:
            val_score += 2
        elif ups >= 0.05:
            val_score += 1
        elif ups <= -0.20:
            val_score -= 2
        elif ups <= -0.05:
            val_score -= 1
        f["valuation"] = f"DCF 空间 {ups:+.1%}"
    elif val["dcf_fair_value"] is None:
        f["valuation"] = "无 DCF 估值"
    else:
        f["valuation"] = "DCF 无空间评估"

    total = tech_score + val_score
    out["score_breakdown"] = {
        "tech_score": tech_score, "val_score": val_score, "total": total,
        "factors": f,
    }

    # ── 评级映射（total ∈ [-6, 6]）──
    if total >= 3:
        rating = "强烈看多"
    elif total >= 1:
        rating = "看多"
    elif total <= -3:
        rating = "强烈看空"
    elif total <= -1:
        rating = "看空"
    else:
        rating = "中性"
    out["rating"] = rating

    # ── 仓位建议 ──
    if rating == "强烈看多":
        hint = "标准仓"
    elif rating == "看多":
        hint = "半仓"
    elif rating == "中性":
        hint = "轻仓"
    else:
        hint = "观望"
    out["position_hint"] = hint

    # ── 交易计划：进场区间 / 止损 / 止盈 ──
    close = _safe(ind.get("close")) or current_price
    support = _safe(ind.get("support_20"))
    resistance = _safe(ind.get("resistance_20"))
    boll_lower = _safe(ind.get("boll_lower"))
    fv = val["dcf_fair_value"]
    overvalued = (close is not None and fv is not None and fv < close)

    entry_low = support if support is not None else boll_lower
    if fv is not None and close is not None and fv > close:
        # 估值合理/低估：进场上限锚定 DCF 内在价值
        entry_high = fv
    else:
        # 高估或 DCF 缺失：进场上限用技术压力位（避免止盈/进场锚定低于现价的 DCF）
        entry_high = resistance if resistance is not None else close
    if entry_low is not None and entry_high is not None and entry_low > entry_high:
        entry_high = entry_low
    out["entry_zone"] = [entry_low, entry_high]

    atr = _safe(ind.get("atr14"))
    if entry_low is not None:
        if atr is not None:
            out["stop_loss"] = round(entry_low - 1.5 * atr, 2)
        else:
            out["stop_loss"] = round(entry_low * 0.95, 2)
    else:
        out["stop_loss"] = None

    # 止盈：候选 = {高于现价的 DCF 内在价值, 高于现价的技术压力位}，取最大。
    # 修复：原逻辑在 fv>现价时 entry_high=fv，导致止盈恒 None，盈亏比算不出。
    tp_candidates = []
    if fv is not None and (close is None or fv > close):
        tp_candidates.append(fv)
    if resistance is not None and (close is None or resistance > close):
        tp_candidates.append(resistance)
    out["take_profit"] = max(tp_candidates) if tp_candidates else None

    # ── 风险收益比：(止盈−进场上限)/(进场上限−止损) ──
    entry_ref = out["entry_zone"][0] if out.get("entry_zone") else None
    sl_val = out.get("stop_loss")
    tp_val = out.get("take_profit")
    rr = None
    if entry_ref is not None and sl_val is not None and tp_val is not None \
            and (entry_ref - sl_val) > 0:
        rr = round((tp_val - entry_ref) / (entry_ref - sl_val), 2)
    out["risk_reward_ratio"] = rr

    # ── 多周期共振：周线趋势（周K MA5 vs MA20）──
    weekly_trend = None
    try:
        if price_data is not None and len(price_data) >= 60:
            wk = price_data.resample("W-FRI").agg(
                {"Open": "first", "High": "max", "Low": "min",
                 "Close": "last", "Volume": "sum"}).dropna()
            if len(wk) >= 40:
                wc = wk["Close"].astype(float)
                wma5 = wc.rolling(5).mean().iloc[-1]
                wma20 = wc.rolling(20).mean().iloc[-1]
                if pd.notna(wma5) and pd.notna(wma20):
                    tol = max(float(wma20) * 0.005, 1e-9)
                    if wma5 > wma20 + tol:
                        weekly_trend = "多头"
                    elif wma5 < wma20 - tol:
                        weekly_trend = "空头"
                    else:
                        weekly_trend = "粘合"
    except Exception:
        weekly_trend = None
    out["weekly_trend"] = weekly_trend
    if weekly_trend and trend:
        out["weekly_resonance"] = "共振" if weekly_trend == trend else "背离"
    elif weekly_trend:
        out["weekly_resonance"] = "周线参考"
    else:
        out["weekly_resonance"] = None

    # ── 置信度分级：技术/估值方向一致性 ──
    tech_dir = 1 if tech_score > 0 else -1 if tech_score < 0 else 0
    val_dir = 1 if val_score > 0 else -1 if val_score < 0 else 0
    if tech_dir != 0 and val_dir != 0 and tech_dir == val_dir:
        confidence_level = "高"
    elif tech_dir != 0 or val_dir != 0:
        confidence_level = "中"
    else:
        confidence_level = "低"
    out["confidence_level"] = confidence_level

    # ── 理由清单（引用真实数值，缺失显式说明）──
    if ind.get("available") and close is not None:
        reasons.append(f"技术面：最新收盘 {_fmt_price(close)}，"
                       f"MA5/MA20/MA60 = {_fmt_price(ind.get('ma5'))}/"
                       f"{_fmt_price(ind.get('ma20'))}/{_fmt_price(ind.get('ma60'))}"
                       f"（{trend or '—'}）")
        if rsi is not None:
            reasons.append(f"动量：RSI(14)={rsi:.1f}"
                           f"{'（超卖，存在修复动力）' if rsi <= 30 else '（超买，注意回撤风险）' if rsi >= 70 else '（中性）'}")
        if macd_hist is not None:
            reasons.append(f"动量：MACD 柱 {macd_hist:+.3f}"
                           f"{'（红柱，多头动能）' if macd_hist > 0 else '（绿柱，空头动能）' if macd_hist < 0 else '（零轴附近）'}")
        if pos is not None:
            reasons.append(f"位置：现价位于 52 周区间 {_fmt_price(ind.get('low_52w'))}~"
                           f"{_fmt_price(ind.get('high_52w'))} 的 {pos:.0%} 分位"
                           f"{'（偏低，安全边际较好）' if pos <= _LOW_POS else '（偏高，追高需谨慎）' if pos >= _HIGH_POS else ''}")
        if atr is not None:
            reasons.append(f"风险：ATR(14)={atr:.2f}，近 20 日年化波动率 "
                           f"{_fmt_pct(ind.get('volatility_20d'), 0)}")
        if out["entry_zone"][0] is not None:
            reasons.append(f"进场区间：{_fmt_price(out['entry_zone'][0])} ~ "
                           f"{_fmt_price(out['entry_zone'][1])}"
                           f"（技术支撑 ~ 估值/压力）")
        if out["stop_loss"] is not None:
            reasons.append(f"止损位：{_fmt_price(out['stop_loss'])}"
                           f"（跌破支撑 {_fmt_price(entry_low)} 或 ATR×1.5）")
        if out["take_profit"] is not None:
            if overvalued:
                reasons.append(f"止盈位：{_fmt_price(out['take_profit'])}"
                               f"（技术压力位；DCF 内在价值 {_fmt_price(fv)} 低于现价，高估）")
            elif fv is not None and out["take_profit"] == fv:
                reasons.append(f"止盈位：{_fmt_price(out['take_profit'])}（DCF 内在价值）")
            else:
                reasons.append(f"止盈位：{_fmt_price(out['take_profit'])}（技术压力位）")
        if weekly_trend:
            reasons.append(f"周期：日线{trend or '—'}，周线{weekly_trend}（{out['weekly_resonance']}）")
        if rr is not None:
            reasons.append(f"风险收益比：{rr:.2f}（止盈/止损空间比）")
        if confidence_level:
            reasons.append(f"信号置信度：{confidence_level}（技术/估值方向一致性）")
    else:
        reasons.append("技术面：无足够历史行情，仅按估值给出方向。")

    if ups is not None and fv is not None and close is not None:
        reasons.append(f"估值：DCF 内在价值 {_fmt_price(fv)}，相对现价 {_fmt_price(close)} "
                       f"空间 {_fmt_pct(ups)}"
                       + ("（现价高于内在价值，估值偏高）" if overvalued else ""))
    elif fv is not None:
        reasons.append(f"估值：DCF 内在价值 {_fmt_price(fv)}（现价缺失，无法比较空间）")
    elif val["dcf_fair_value"] is None:
        reasons.append(f"估值：{dcf.get('reason') if isinstance(dcf, dict) and dcf.get('reason') else '无 DCF 估值数据'}")

    pe_c, pe_a = val["pe_company"], val["pe_industry_avg"]
    if pe_c is not None:
        pm = val["pe_premium"]
        seg = f"估值：PE={pe_c:.1f}"
        if pe_a is not None:
            seg += f" vs 行业 {pe_a:.1f}"
        if pm is not None:
            seg += f"（{_fmt_pct(pm)}）"
        reasons.append(seg)

    if swot_rating:
        reasons.append(f"基本面参考：SWOT 评级「{swot_rating}」")

    if not reasons:
        reasons.append("数据不足：暂无法给出方向判断。")
    out["reasons"] = reasons

    return out
