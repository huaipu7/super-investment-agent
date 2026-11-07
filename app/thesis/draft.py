"""从已有产物生成论点草稿 —— 只抄真实数字，不替用户写论点。

草稿的边界：
  · statement（一句话论点）永远留空。论点只有人能写，机器写出来的就是编造。
  · 证伪条件的阈值默认取「当前真实值」，并在 note 里写明这是当前值、待改成
    认错线。数字本身来自台账，不来自模型。
  · 关键假设只收录产物里 DCF 真正用过的参数（WACC / 增速 / 永续增速），
    它们有据可查；产物没给的参数不补。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.thesis.checks import load_ledger, resolve_metric

# (台账路径, 默认比较符, 中文说明)
# 路径以 `[]` 结尾的下标表示「最新一期」，避免写死期数后悄悄对错年份判定。
CANDIDATES: list[tuple[str, str, str]] = [
    ("valuation_analysis.dcf.fair_value", "<", "DCF 公平价值"),
    ("valuation_analysis.dcf.upside", "<", "DCF 上行空间"),
    ("valuation_analysis.relative_valuation.pe.company", ">", "当前 PE（相对估值）"),
    ("financial_analysis.ratios.GROSS_MARGIN[].value", "<", "毛利率（最新期）"),
    ("financial_analysis.ratios.NET_MARGIN[].value", "<", "净利率（最新期）"),
    ("financial_analysis.ratios.OPERATING_MARGIN[].value", "<", "营业利润率（最新期）"),
    ("financial_analysis.ratios.ROE[].value", "<", "ROE（最新期）"),
    ("financial_analysis.ratios.CURRENT_RATIO[].value", "<", "流动比率（最新期）"),
    ("financial_analysis.ratios.DEBT_TO_EQUITY[].value", ">", "负债权益比（最新期）"),
    ("financial_analysis.ratios.FREE_CASH_FLOW[].value", "<", "自由现金流（最新期）"),
]

_DCF_ASSUMPTIONS = [
    ("wacc_used", "WACC {:.2%}"),
    ("growth_assumption", "首年 FCF 增速 {:.2%}"),
    ("terminal_growth", "永续增速 {:.2%}"),
]


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def build_draft(artifact_path: str | Path, *, root: Path) -> dict:
    """读产物 → 草稿。产物读不了会抛 FileNotFoundError/ValueError，由调用方转 404。"""
    path = Path(artifact_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("产物不是 JSON 对象")

    artifact_ref = _rel(path, root)
    ledger = load_ledger(path)
    notes: list[str] = []
    warnings: list[str] = []

    if ledger is None:
        return {
            "ticker": str(data.get("ticker") or ""),
            "artifact_ref": artifact_ref,
            "statement": "", "stance": "neutral", "horizon": "",
            "conviction": 3, "key_assumptions": [], "falsifiers": [],
            "notes": [],
            "warnings": ["产物无法重建台账，没有可抄的真实数字：请手写证伪条件。"],
        }

    falsifiers = []
    for metric, op, label in CANDIDATES:
        fact = resolve_metric(ledger, metric)
        if fact is None:
            continue
        if fact.is_unknown:
            warnings.append(f"{label} 在台账里标记为 UNKNOWN，未纳入草稿。")
            continue
        try:
            value = float(fact.value)
        except (TypeError, ValueError):
            continue
        if value != value or value in (float("inf"), float("-inf")):
            warnings.append(f"{label} 值非有限数，未纳入草稿。")
            continue
        as_of = fact.as_of
        # 阈值存精确值，不四舍五入：366.8528718 舍到 6 位会变成 366.852872，
        # `<` 当场成立，草稿一建出来就「被证伪」。显示时再截断即可。
        falsifiers.append({
            "metric": metric,
            "op": op,
            "value": value,
            "note": f"{label}：当前值 {round(value, 6)}"
                    f"{'（' + as_of + '）' if as_of else ''}，阈值请改成你的认错线",
            "as_of": as_of,
        })

    assumptions = []
    dcf = (data.get("valuation_analysis") or {}).get("dcf") or {}
    for key, tpl in _DCF_ASSUMPTIONS:
        v = dcf.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            assumptions.append(f"DCF 假设：{tpl.format(float(v))}")

    if assumptions:
        notes.append("关键假设已收录 DCF 实际使用的参数，可删改。")
    if falsifiers:
        notes.append("证伪条件阈值默认取当前值，必须改成你自己认可的认错线后再保存。")
        notes.append("下游指标写作 []. 表示取最新一期，换新产物不会对错年份。")
    else:
        warnings.append("产物里没找到任何候选指标，需手写证伪条件。")

    unknown_n = len(ledger.unknown())
    if unknown_n:
        warnings.append(f"该产物台账含 {unknown_n} 条 UNKNOWN 事实，相关指标一律不纳入草稿。")

    return {
        "ticker": str(data.get("ticker") or ledger.ticker or ""),
        "artifact_ref": artifact_ref,
        "statement": "",
        "stance": "neutral",
        "horizon": "",
        "conviction": 3,
        "key_assumptions": assumptions,
        "falsifiers": falsifiers,
        "notes": notes,
        "warnings": warnings,
    }
