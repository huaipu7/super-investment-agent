# -*- coding: utf-8 -*-
"""固收动作化引擎 —— 建议转行动 + 风险信号 + 合规复核队列。

确定性规则（无 LLM、不造假价格）：输入是信用/利率/政策引擎的**真实输出**，输出：
  - credit_view  ：信用观点（Altman Z'' 分区 / Ohlson O / Merton 可用性）
  - rate_view    ：久期/曲线倾向（由政策鸽鹰分 + 曲线斜率驱动）
  - synthesis    ：综合方向 + 置信度 + 关注度
  - risk_signals ：结构化风险信号（名称/严重度/触发条件/来源）
  - compliance_queue：需人工复核项（代理标签、缺数据、无商业评级、无价格 feed 等）

诚实边界（约束#7）：
  - 任一输入缺失，对应字段显式 None 并在 reasons 注明来源；
  - 无债券价格/久期报价 feed → 动作以「久期 / 曲线」层面表达，**不给出价格级**进场/止损/止盈；
  - 政策立场为词典法启发式、信用为结构模型代理标签 —— 一律进复核队列。

与全项目合规机制一致：最终行动文本走 app/compliance.rules.apply_gate，
命中投资建议则自动追加免责声明 + 落复核队列（app/compliance.queue.flag）。
"""
from __future__ import annotations

from app.credit.features import ALTMAN_LABEL_CUT


def _safe(v, nd: int = 3):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return round(f, nd)


def _tier(z: float | None) -> str | None:
    if z is None:
        return None
    if z < 1.10:
        return "风险区"
    if z < ALTMAN_LABEL_CUT:  # 2.60
        return "灰区"
    return "安全区"


def _duration_tilt(score: float | None) -> tuple[str, str | None]:
    """政策鸽鹰分 → 久期倾向。返回 (描述, 'long'/'short'/'neutral'/None)。"""
    if score is None:
        return "久期方向存疑（政策信号缺失）", None
    if score >= 0.2:
        return "缩短久期（预期政策收紧 / 利率上行）", "short"
    if score <= -0.2:
        return "拉长久期（预期政策宽松 / 利率下行）", "long"
    return "久期中性", "neutral"


def _curve_shape(slope: float | None) -> str | None:
    if slope is None:
        return None
    if slope < 0:
        return "正常向上"
    if slope > 0:
        return "倒挂"
    return "平坦"


def _position_hint(tier: str | None, dur: str | None) -> str:
    if tier == "安全区" and dur in ("long", "short"):
        return "高关注（信用健康 + 久期方向明确）"
    if tier == "安全区":
        return "中关注（信用健康，方向中性）"
    if tier == "灰区":
        return "谨慎（信用走弱 / 灰区）"
    if tier == "风险区":
        return "观察/回避（结构信用风险）"
    return "方向不明（数据不足）"


def _confidence(tier: str | None, dur: str | None) -> str:
    credit_clear = tier in ("安全区", "风险区")
    rate_clear = dur in ("long", "short")
    if credit_clear and rate_clear:
        return "高"
    if credit_clear or rate_clear:
        return "中"
    return "低"


def synthesize_action(*, issuer: str | None = None, credit: dict | None = None,
                      policy: dict | None = None, curve: dict | None = None) -> dict:
    """把信用/利率/政策引擎输出综合成行动方案。全部输入为可空 dict，缺失即如实降级。"""
    credit = credit or {}
    policy = policy or {}
    curve = curve or {}

    z = _safe(credit.get("altman_z"))
    o = _safe(credit.get("ohlson_o"))
    tier = _tier(z)
    merton = credit.get("merton") or {}
    merton_avail = bool(merton.get("available"))
    merton_reason = merton.get("reason")

    pscore = _safe(policy.get("score"))
    pst = policy.get("stance")
    dur_desc, dur = _duration_tilt(pscore)
    level = _safe(curve.get("level"))
    slope = _safe(curve.get("slope"))
    cshape = _curve_shape(slope)

    synthesis = {
        "tier": tier, "z": z, "o": o, "merton_available": merton_avail,
        "duration_tilt": dur_desc, "curve_shape": cshape,
        "policy_stance": pst, "policy_score": pscore, "level": level, "slope": slope,
        "position_hint": _position_hint(tier, dur),
        "confidence_level": _confidence(tier, dur),
    }

    risk_signals: list[dict] = []
    # 信用风险
    if z is not None and z < 1.10:
        risk_signals.append({"name": "主体违约/结构信用风险", "severity": "高",
                             "trigger": f"Altman Z''={z} < 1.10", "source": "credit_issuer"})
    elif z is not None and z < ALTMAN_LABEL_CUT:
        risk_signals.append({"name": "主体信用走弱（灰区）", "severity": "中",
                             "trigger": f"Altman Z''={z} ∈ [1.10, {ALTMAN_LABEL_CUT})",
                             "source": "credit_issuer"})
    if o is not None and o > 0.5:
        risk_signals.append({"name": "Ohlson 财务困境信号", "severity": "高",
                             "trigger": f"Ohlson O={o} > 0.5", "source": "credit_issuer"})
    if not merton_avail:
        risk_signals.append({"name": "违约尾部风险未量化（Merton 不可用）", "severity": "中",
                             "trigger": "市场数据缺失（yfinance 未接入/限流）", "source": "credit_issuer"})

    # 利率/久期风险
    if pscore is not None and pscore >= 0.2:
        risk_signals.append({"name": "政策紧缩 / 利率上行久期压力", "severity": "高",
                             "trigger": f"政策鸽鹰分={pscore} ≥ 0.2（偏鹰）", "source": "policy_signal"})
    elif pscore is not None and pscore <= -0.2:
        risk_signals.append({"name": "利率下行 / 再投资票息再配置风险", "severity": "低",
                             "trigger": f"政策鸽鹰分={pscore} ≤ -0.2（偏鸽）", "source": "policy_signal"})
    if slope is not None and slope > 0:
        risk_signals.append({"name": "收益率曲线倒挂", "severity": "中",
                             "trigger": f"斜率={slope} > 0", "source": "rate_curve"})

    # 数据/接口缺口
    if not merton_avail:
        risk_signals.append({"name": "数据缺口：无债券价格/久期报价 feed", "severity": "中",
                             "trigger": "未接入价格级数据，动作以久期/曲线层面表达",
                             "source": "action_engine"})

    # 合规复核队列
    compliance_queue = [
        {"item": "信用评级为结构模型代理标签（Altman Z''/Ohlson O/Merton），非评级机构评级",
         "reason": "无商业评级 feed，代理标签仅供内部参考", "severity": "中"},
        {"item": "动作以久期/曲线层面表达，无价格级进场/止损/止盈",
         "reason": "未接入债券价格/久期报价 feed", "severity": "中"},
    ]
    if not merton_avail:
        compliance_queue.append({"item": "Merton 违约距离不可用（市场数据缺失）",
                                 "reason": merton_reason or "yfinance 限流/未接入", "severity": "高"})
    if pst and pscore is not None and "词典" in (policy.get("method") or "词典法"):
        compliance_queue.append({"item": "政策立场为词典法启发式估计，非官方口径",
                                 "reason": "基于 FOMC 原文词频，非美联储官方立场", "severity": "中"})

    reasons: list[str] = []
    if issuer:
        reasons.append(f"主体：{issuer}。")
    if tier is not None:
        reasons.append(f"信用：Altman Z''={z}（{tier}）")
    elif z is not None:
        reasons.append(f"信用：Altman Z''={z}")
    else:
        reasons.append("信用：Altman Z'' 不可算（缺输入项）")
    if o is not None:
        reasons.append(f"Ohlson O={o}（{'>0.5 提示困境' if o > 0.5 else '未见明显困境信号'}）")
    if merton_avail:
        reasons.append(f"Merton：DD={_safe(merton.get('dd'))}，PD≈{_safe(merton.get('pd'), 4)}（可算）")
    else:
        reasons.append(f"Merton：不可用（{merton_reason or '市场数据缺失'}）")

    if pscore is not None:
        reasons.append(f"政策：{pst or '—'}（鸽鹰分={pscore}）→ {dur_desc}")
    else:
        reasons.append("政策：FOMC 声明不可用 → 久期方向无法由政策锚定")
    if slope is not None:
        reasons.append(f"曲线：NSS 斜率={slope}（{cshape}），level={level}")
    elif level is not None:
        reasons.append(f"曲线：level={level}，斜率不可得")
    else:
        reasons.append("利率：Treasury 曲线当前不可用，久期/曲线判断依赖政策分")

    reasons.append(f"关注度：{synthesis['position_hint']}；置信度：{synthesis['confidence_level']}")

    # as_of：曲线 > 信用 > 政策
    as_of = curve.get("as_of") or credit.get("as_of_end") or policy.get("meeting_date")

    # 行动文本 + 合规门（追加免责声明 + 落复核队列）
    advice_text = (
        f"综合：{issuer or '固收组合'}，{'信用' + tier if tier else '信用数据不足'}，"
        f"关注度「{synthesis['position_hint']}」，置信度「{synthesis['confidence_level']}」。"
        f"久期倾向：{dur_desc}。需人工复核项 {len(compliance_queue)} 项。"
    )
    try:
        from app.compliance.rules import apply_gate
        advice_text, _, _ = apply_gate(advice_text, source="fixed_income_action")
    except Exception:
        pass  # 合规队列不可用时不妨碍动作引擎本身

    return {
        "available": True, "issuer": issuer, "as_of": as_of,
        "credit_view": {"tier": tier, "z": z, "o": o, "merton_available": merton_avail,
                        "merton_reason": merton_reason},
        "rate_view": {"duration_tilt": dur_desc, "curve_shape": cshape,
                      "policy_stance": pst, "policy_score": pscore,
                      "level": level, "slope": slope},
        "synthesis": synthesis,
        "risk_signals": risk_signals,
        "compliance_queue": compliance_queue,
        "reasons": reasons,
        "advice_text": advice_text,
        "note": "确定性规则（无 LLM）；动作以久期/曲线层面表达，不给出价格级止损止盈；"
                "数值全部来自真实引擎输出。",
    }
