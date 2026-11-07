"""五个护栏指标 —— 三个质量护栏 + 两个飞轮健康度。

  grounding_rate          数值接地率（离线 L1 报告）
  degraded_rate           降级事实占比（离线 L0 报告）
  llm_error_rate          LLM 调用失败率（进程内累计，重启清零）
  feedback_negative_rate  👎 占全部反馈的比例（feedback.db 全量）
  feedback_coverage       有反馈的助手消息 / 全部助手消息

三条口径纪律：
  1 每个指标带 meta.scope / as_of / n —— 离线值与实时值混看会误读。
  2 分母为 0 时返回 None，不返回 0：「一次都没评」和「评了但全坏」是两回事。
  3 本机单用户，样本量以个位数计。这些值能演示闭环是否在转，
     不足以支撑任何统计显著的结论。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.config import BASE_DIR

EVAL_DIR = BASE_DIR / "data" / "eval"

_OFFLINE = {
    "grounding_rate": ("l1_report.json", "离线 L1 评测（跑在落盘产物上）"),
    "degraded_rate": ("l0_report.json", "离线 L0 评测（跑在落盘产物上）"),
}


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 4) if den else None


def snapshot(counters: dict | None = None, *, eval_dir: Path | None = None,
             feedback_stats: dict | None = None,
             assistant_total: int | None = None) -> dict:
    """返回 5 个护栏值 + meta。依赖可注入，便于离线自检。"""
    counters = counters or {}
    eval_dir = eval_dir or EVAL_DIR
    values: dict[str, float | None] = {}
    meta: dict[str, dict] = {}

    for key, (fname, scope) in _OFFLINE.items():
        rep = _read_json(eval_dir / fname)
        metrics = (rep or {}).get("metrics") or {}
        v = metrics.get(key)
        values[key] = round(float(v), 4) if isinstance(v, (int, float)) else None
        meta[key] = {"scope": scope, "as_of": (rep or {}).get("generated_at"),
                     "n": metrics.get("fact_total") if key == "degraded_rate"
                     else metrics.get("number_tokens")}

    calls = int(counters.get("llm_calls") or 0)
    errors = int(counters.get("llm_errors") or 0)
    values["llm_error_rate"] = _ratio(errors, calls)
    meta["llm_error_rate"] = {"scope": "进程内累计（重启清零）", "as_of": None,
                              "n": calls}

    if feedback_stats is None:
        from app.feedback import store
        feedback_stats = store.stats()
    total = int(feedback_stats.get("total") or 0)
    values["feedback_negative_rate"] = _ratio(int(feedback_stats.get("down") or 0), total)
    meta["feedback_negative_rate"] = {"scope": "feedback.db 全量", "as_of": None,
                                      "n": total}

    if assistant_total is None:
        from app.memory import db as memory_db
        assistant_total = memory_db.count_messages("assistant")
    values["feedback_coverage"] = _ratio(
        int(feedback_stats.get("distinct_msgs") or 0), int(assistant_total or 0))
    meta["feedback_coverage"] = {"scope": "feedback.db 全量 / memory.db 助手消息",
                                 "as_of": None, "n": int(assistant_total or 0)}

    return {**values, "meta": meta}
