"""调查报告组装 —— 确定性骨架 + 一次可选的行文调用。

骨架部分**完全确定性**（无 LLM），所以即使行文调用失败也一定能出报告；
失败时如实标注"总述生成失败，仅保留确定性结论"，绝不假装成功。

红线：任何缺失数值渲染为 "—"，绝不填 0、绝不编造。被判定为
「模型推断」的证据一律带 `confidence: low` 台账（沿用 app/analysis/supply_chain.py
的 estimates_used 风格），并与其来源分开列出。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.investigation.hypotheses import (
    STATUS_INSUFFICIENT, STATUS_PARTIAL, STATUS_UNVERIFIED, STATUS_VERIFIED,
)
from app.utils.logger import logger

# 状态 → 中文展示与 UI 配色（UI 直接读这里，避免两处定义漂移）
STATUS_DISPLAY: dict[str, dict] = {
    STATUS_VERIFIED: {"label": "已验证", "color": "green"},
    STATUS_PARTIAL: {"label": "部分验证", "color": "orange"},
    STATUS_UNVERIFIED: {"label": "未验证", "color": "gray"},
    STATUS_INSUFFICIENT: {"label": "证据不足", "color": "red"},
}

# 停止原因 → 人话
STOP_DISPLAY: dict[str, str] = {
    "all_hypotheses_settled": "模型判断已无更多可查方向，正常收尾",
    "max_rounds": "达到轮次上限",
    "max_tool_calls": "达到工具调用上限",
    "max_llm_calls": "达到 LLM 调用上限",
    "deadline": "达到墙钟时限（剩余时间不足一轮）",
    "no_progress": "连续多轮无新增证据，提前停止",
    "oscillation": "检测到动作在原地打转，提前停止",
    "llm_exhausted": "引擎报告工具轮次耗尽（该提示不是结论）",
    "llm_error": "LLM 调用失败",
}

_MISSING = "—"


def fmt_num(v, unit: str = "") -> str:
    """数值 → 展示字符串。None/NaN → "—"（显式缺失，不填 0）。"""
    if v is None:
        return _MISSING
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _MISSING
    if f != f:
        return _MISSING
    return f"{round(f, 4)}{unit}"


def _prose_prompt(payload: dict) -> str:
    return (
        "下面是一次投研调查的**确定性结果**。请写一段 200 字以内的总述。\n"
        "硬性要求：\n"
        "1. 只能使用下面给出的数字与结论，不得引入任何新数字、新事实、新来源\n"
        "2. 必须区分「已验证」「部分验证」「未验证」「证据不足」四种强度，不得拔高\n"
        "3. 标为「模型推断」的条目只能说成「待验证线索」，不得当作结论\n"
        "4. 不得给出买卖建议\n"
        "5. 若某项数据缺失（显示为 —），如实说缺失，不要跳过也不要补\n"
        f"调查结果：\n{json.dumps(payload, ensure_ascii=False, indent=1)}"
    )


def build_report(run: dict, *, llm=None, prose: bool = True) -> dict:
    """把 loop 的输出组装成报告。骨架确定性；prose 为可选增强。"""
    hypotheses = list(run.get("hypotheses") or [])
    evidence = list(run.get("evidence") or [])
    by_hyp: dict[str, list[dict]] = {h["id"]: [] for h in hypotheses}
    for e in evidence:
        by_hyp.setdefault(e.get("hypothesis_id") or "", []).append(e)

    rows: list[dict] = []
    for h in hypotheses:
        evs = by_hyp.get(h["id"], [])
        accepted = [e for e in evs if e.get("attribution_valid")]
        rejected = [e for e in evs if not e.get("attribution_valid")]
        rows.append({
            "id": h["id"],
            "family": h.get("family"),
            "family_label": h.get("family_label"),
            "statement": h.get("statement"),
            "status": h.get("status"),
            "status_label": STATUS_DISPLAY.get(h.get("status"), {}).get("label", h.get("status")),
            "status_color": STATUS_DISPLAY.get(h.get("status"), {}).get("color", "gray"),
            "status_reason": h.get("status_reason"),
            "support_count": h.get("support_count", 0),
            "contradict_count": h.get("contradict_count", 0),
            "rejected_count": len(rejected),
            "support_kinds": h.get("support_kinds") or [],
            "prior_confidence": h.get("prior_confidence"),
            "seeded_by": h.get("seeded_by") or [],
            "predicted_observables": h.get("predicted_observables") or [],
            "why_it_matters": h.get("why_it_matters"),
            "evidence": accepted,
            "rejected_evidence": rejected,
        })

    unverified = [r for r in rows if r["status"] != STATUS_VERIFIED]
    verified = [r for r in rows if r["status"] == STATUS_VERIFIED]
    # 「模型推断」台账：无原始来源的条目，一律低置信，与真实来源分开列
    llm_only = [e for e in evidence
                if e.get("source_kind") == "llm_hypothesis" and e.get("attribution_valid")]

    material = run.get("material_signals") or []
    signals = run.get("signals") or []

    report: dict = {
        "ticker": run.get("ticker"),
        "period": run.get("period"),
        "depth": run.get("depth"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stop_reason": run.get("stop_reason"),
        "stop_reason_label": STOP_DISPLAY.get(run.get("stop_reason"), run.get("stop_reason")),
        "limits": run.get("limits") or {},
        "spent": run.get("spent") or {},
        "budget_overshoot_seconds": run.get("budget_overshoot_seconds"),
        "signals": signals,
        "material_signals": material,
        "seeded_families": run.get("seeded_families") or [],
        "hypotheses": rows,
        "verified": [r["id"] for r in verified],
        "unverified_causes": [
            {"id": r["id"], "statement": r["statement"], "status": r["status_label"],
             "status_reason": r["status_reason"]} for r in unverified],
        "status_counts": run.get("status_counts") or {},
        "llm_hypothesis_ledger": [{
            "hypothesis_id": e.get("hypothesis_id"),
            "claim": e.get("rationale"),
            "confidence": "low",
            "source_kind": "llm_hypothesis",
            "note": "模型推断，无原始来源，仅作待验证线索",
        } for e in llm_only],
        "no_data": bool(run.get("no_data")),
        "data_note": run.get("data_note") or "",
        "degrade_note": run.get("degrade_note") or "",
        "disclaimer": run.get("disclaimer") or "",
        "prose": "",
        "prose_note": "",
        "narrative": run.get("narrative") or "",
        # deep 档由编排层挂上；quick/standard 没有这一段，取不到就是 None
        "redteam": run.get("redteam"),
    }

    if prose and llm is not None and hypotheses:
        report["prose"], report["prose_note"] = _write_prose(report, llm)
    return report


def _write_prose(report: dict, llm) -> tuple[str, str]:
    """可选总述。失败即空串 + 如实标注，绝不影响确定性结论。"""
    payload = {
        "标的": report.get("ticker"),
        "触发的真实财务信号": [{
            "科目": s.get("label"), "值": s.get("value"),
            "相对变化(%)": s.get("rel_value"), "单位": s.get("unit"),
            "方向": s.get("direction"), "阈值": s.get("threshold"),
        } for s in report.get("material_signals") or []],
        "停止原因": report.get("stop_reason_label"),
        "结论清单": [{
            "假设": r["statement"], "状态": r["status_label"],
            "支持证据数": r["support_count"],
            "来源类型": r["support_kinds"],
            "未采信数": r["rejected_count"],
            "状态说明": r["status_reason"],
        } for r in report.get("hypotheses") or []],
        "模型推断线索": report.get("llm_hypothesis_ledger") or [],
        "数据缺失说明": report.get("data_note") or "",
    }
    try:
        out = llm.chat(messages=[{"role": "user", "content": _prose_prompt(payload)}],
                       task="investigation", max_tokens=600)
        text = (out or "").strip()
        if not text:
            return "", "总述为空，仅保留确定性结论"
        return text, ""
    except Exception as e:                    # noqa: BLE001
        logger.warning("调查总述生成失败: %s", e)
        return "", f"总述生成失败（{type(e).__name__}），仅保留确定性结论"


def report_markdown(report: dict) -> str:
    """确定性 Markdown 渲染。缺失数值一律 "—"。"""
    L: list[str] = []
    L.append(f"# 自主深度调查：{report.get('ticker') or _MISSING}")
    L.append("")
    L.append(f"- 生成时间（UTC）：{report.get('generated_at')}")
    L.append(f"- 档位：{report.get('depth')}　报表口径：{report.get('period')}")
    spent = report.get("spent") or {}
    limits = report.get("limits") or {}
    L.append(f"- 消耗：轮次 {spent.get('rounds', 0)}/{limits.get('max_rounds')}，"
             f"工具调用 {spent.get('tool_calls', 0)}/{limits.get('max_tool_calls')}，"
             f"LLM 调用 {spent.get('llm_calls', 0)}/{limits.get('max_llm_calls')}，"
             f"去重命中 {spent.get('duplicates', 0)}")
    L.append(f"- 停止原因：{report.get('stop_reason_label')}"
             f"（`{report.get('stop_reason')}`）")
    over = report.get("budget_overshoot_seconds") or 0
    if over > 1:
        L.append(f"- 墙钟超出上限 {round(over, 1)}s（在途请求无法中断，如实记录）")
    L.append("")

    if report.get("prose"):
        L.append("## 总述")
        L.append(report["prose"])
        L.append("")
    if report.get("prose_note"):
        L.append(f"> {report['prose_note']}")
        L.append("")
    if report.get("degrade_note"):
        L.append(f"> 降级说明：{report['degrade_note']}")
    if report.get("data_note"):
        L.append(f"> 数据说明：{report['data_note']}")
    L.append("")

    L.append("## 触发的真实财务信号（material）")
    mats = report.get("material_signals") or []
    if not mats:
        L.append(f"无（{report.get('data_note') or '未达到阈值的异常信号'}）")
    else:
        L.append("| 信号 | 值 | 相对变化 | 单位 | 方向 | 阈值 |")
        L.append("|---|---|---|---|---|---|")
        for s in mats:
            rel = s.get("rel_value")
            rel_txt = f"{fmt_num(rel)}%" if rel is not None else _MISSING
            L.append(f"| {s.get('label')} | {fmt_num(s.get('value'))} | {rel_txt} | "
                     f"{s.get('unit') or _MISSING} | {s.get('direction')} | "
                     f"{s.get('threshold') or _MISSING} |")
    L.append("")

    L.append("## 成因假设与证据")
    if not report.get("hypotheses"):
        L.append(f"未生成假设（{report.get('data_note') or '无 material 信号'}）")
    for r in report.get("hypotheses") or []:
        L.append(f"### {r['id']}　{r['status_label']}　{r.get('family_label')}")
        L.append(f"- 假设：{r.get('statement')}")
        L.append(f"- 判定依据：{r.get('status_reason')}")
        L.append(f"- 支持 {r['support_count']} 条 / 反驳 {r['contradict_count']} 条 / "
                 f"未采信 {r['rejected_count']} 条"
                 + (f"；来源类型：{'、'.join(r['support_kinds'])}" if r["support_kinds"] else ""))
        if r.get("predicted_observables"):
            L.append(f"- 可观察点：{'；'.join(r['predicted_observables'])}")
        if r.get("evidence"):
            L.append("")
            L.append("| 来源类型 | 关系 | 出处 | 片段 |")
            L.append("|---|---|---|---|")
            for e in r["evidence"]:
                L.append(f"| {e.get('source_kind')} | {e.get('relation')} | "
                         f"{e.get('source_ref') or _MISSING} | "
                         f"{(e.get('snippet') or _MISSING)[:60]} |")
        if r.get("rejected_evidence"):
            L.append("")
            L.append("未采信（计零，仅留档）：")
            for e in r["rejected_evidence"]:
                L.append(f"  - {e.get('tool')}：{e.get('reject_reason')}")
        L.append("")

    if report.get("unverified_causes"):
        L.append("## 未验证原因（不得当作结论）")
        for u in report["unverified_causes"]:
            L.append(f"- **{u['id']}**（{u['status']}）{u['statement']} —— {u['status_reason']}")
        L.append("")

    ledger = report.get("llm_hypothesis_ledger") or []
    if ledger:
        L.append("## 模型推断台账（无原始来源，低置信）")
        for e in ledger:
            L.append(f"- [{e['hypothesis_id']}] {e.get('claim') or _MISSING}"
                     f"　confidence: {e['confidence']}　（{e['note']}）")
        L.append("")

    L.append("## 免责与口径")
    L.append(report.get("disclaimer") or "")
    L.append(
        "> 诚实边界：财报快照非 point-in-time；上游/下游关系无真实数据源，仅来自真实文本的"
        "推断；免费源不提供一致预期/目标价，因此不存在「业绩不及预期」类假设。")
    return "\n".join(L)
