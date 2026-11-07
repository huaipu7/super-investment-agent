"""证伪条件判定 —— 拿台账里的真实值去对照论点写下的「什么情况下我认错」。

不调用 LLM，不做语义判断。每条证伪条件形如：

    {"metric": "valuation_analysis.fair_value", "op": "<", "value": 1200}

metric 是产物台账里的字段路径，直接走 Ledger.find() 取带出处的事实。

三种结果，三种含义，不许混为一谈：
  breached  条件命中 —— 论点已被事实推翻
  holding   条件未命中，且取到了真实值
  unknown   取不到值（路径不在台账 / 台账标记 UNKNOWN / 非有限数）
            「查不到」不等于「没出事」，所以它绝不能算作 holding。
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.trust.ledger import Ledger, build_ledger

BREACHED = "breached"
HOLDING = "holding"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class FalsifierResult:
    metric: str
    op: str
    threshold: float
    state: str
    observed: float | None = None
    unit: str | None = None
    as_of: str | None = None
    provenance: str | None = None
    reason: str | None = None
    # 实际取到值的那条台账路径。metric 写作 xxx[] 时与 metric 不同，
    # 让「我判的是哪一期」在结果里可见。
    resolved_path: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _compare(observed: float, op: str, threshold: float) -> bool:
    if op == "<":
        return observed < threshold
    if op == "<=":
        return observed <= threshold
    if op == ">":
        return observed > threshold
    if op == ">=":
        return observed >= threshold
    if op == "==":
        return observed == threshold
    if op == "!=":
        return observed != threshold
    raise ValueError(f"不支持的比较符：{op!r}")


def resolve_metric(ledger: Ledger, metric: str) -> Any:
    """按路径取事实；路径里含 `[]` 时取同容器的「最新一期」。

    `financial_analysis.ratios.GROSS_MARGIN[26].value` 这种写死了下标，
    换一份产物下标含义就变了（26 期可能是 2026 也可能是 2025）——
    阈值会静默对着错误的期数判定。写成 `...GROSS_MARGIN[].value` 则按
    as_of 取最新一条，期数错位就不可能发生。精确路径优先，行为不变。
    """
    fact = ledger.find(metric)
    if fact is not None or "[]" not in metric:
        return fact

    prefix, _, suffix = metric.partition("[]")
    best = None
    best_key = None
    for f in ledger.facts:
        if not f.path.startswith(prefix + "[") or not f.path.endswith(suffix):
            continue
        mid = f.path[len(prefix) + 1:len(f.path) - len(suffix)]
        if not mid.endswith("]") or not mid[:-1].isdigit():
            continue
        key = (f.as_of or "", int(mid[:-1]))
        if best_key is None or key > best_key:
            best, best_key = f, key
    return best


def _as_finite(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def evaluate_falsifier(spec: dict, ledger: Ledger | None) -> FalsifierResult:
    """判定单条证伪条件。任何取不到值的情况都返回 unknown 并写明原因。"""
    metric = str(spec.get("metric") or "")
    op = str(spec.get("op") or "")
    try:
        threshold = float(spec.get("value"))
    except (TypeError, ValueError):
        return FalsifierResult(metric, op, float("nan"), UNKNOWN,
                               reason="invalid_threshold")

    base = dict(metric=metric, op=op, threshold=threshold)

    if ledger is None:
        return FalsifierResult(**base, state=UNKNOWN, reason="no_ledger")

    fact = resolve_metric(ledger, metric)
    if fact is None:
        return FalsifierResult(**base, state=UNKNOWN, reason="path_not_in_ledger")

    observed = _as_finite(fact.value)
    if fact.is_unknown:
        return FalsifierResult(**base, state=UNKNOWN, observed=observed,
                               unit=fact.unit, as_of=fact.as_of,
                               provenance=fact.provenance, reason="ledger_unknown",
                               resolved_path=fact.path)
    if observed is None:
        return FalsifierResult(**base, state=UNKNOWN, unit=fact.unit,
                               as_of=fact.as_of, provenance=fact.provenance,
                               reason="non_finite", resolved_path=fact.path)

    hit = _compare(observed, op, threshold)
    return FalsifierResult(**base, state=BREACHED if hit else HOLDING,
                           observed=observed, unit=fact.unit, as_of=fact.as_of,
                           provenance=fact.provenance, resolved_path=fact.path)


def evaluate(thesis: dict, ledger: Ledger | None) -> dict:
    """判定一条论点的全部证伪条件，给出总体结论。

    总体结论只描述「证伪条件的状态」，不改写论点自身的 status ——
    是否把论点标记为 falsified 由人来定，机器只负责把事实摆出来。
    """
    specs = thesis.get("falsifiers") or []
    results = [evaluate_falsifier(s, ledger) for s in specs]

    breached = [r for r in results if r.state == BREACHED]
    unknown = [r for r in results if r.state == UNKNOWN]

    if breached:
        verdict, reason = "falsified", None
    elif unknown:
        verdict, reason = "inconclusive", "unknown_falsifier"
    elif not results:
        verdict, reason = "inconclusive", "no_falsifiers"
    else:
        verdict, reason = "holding", None

    return {
        "thesis_id": thesis.get("id"),
        "verdict": verdict,
        "reason": reason,
        "counts": {
            "total": len(results),
            "breached": len(breached),
            "holding": sum(1 for r in results if r.state == HOLDING),
            "unknown": len(unknown),
        },
        "results": [r.to_dict() for r in results],
    }


def load_ledger(artifact_path: str | Path) -> Ledger | None:
    """从产物文件重建台账；读不了返回 None（调用方会把结果标为 unknown）。"""
    try:
        data = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # 产物本身不嵌台账，用与 L0 相同的构建器现算，口径一致
    return build_ledger(data, ticker=data.get("ticker"))
