"""事实级跨 run 对比：同一标的这一次和上一次，到底哪里不一样。

`ui/report_diff.py` 只比 Markdown 文本，数字变了它看不出来。这一层建立在台账之上，
按事实对齐两份产物，输出五类变化：新增 / 消失 / 改值 / 换源 / 降级反转。

对齐为什么不能只按 path
-----------------------
行情序列是列式的，台账把 `price_history.close` 的 300 个值落成 `close[0]`…`close[299]`。
多一个交易日，全部下标后移，按 path 硬对会得到「1704 条改值」——全是错位，不是真的变了。
实测 AAPL 相邻两期：按 path 1704 条；把**末尾下标**换成该事实的 `as_of` 之后降到 193 条
（其中 1180 条其实是 float32 往返噪声，另外分档处理）。

只换末尾下标是刻意的：
  · `price_history.close[299]` → `price_history.close@2026-11-06`（按日期对齐）
  · `ratios.ROE[3].value`      → `ratios.ROE@2025.value`（按报告期对齐）
  · `sotp_analysis.segments[2].revenue_share` 保持原样——分部顺序有语义，换下标就等于换分部
  · `knowledge.snippets[2].metadata.lex_score` 保持原样——检索排序变了就是变了

纯只读：不改产物、不写台账、不重算任何数值。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from app.trust.ledger import build_ledger

# 末尾下标。只认 `[N]` 与 `[N].value` 两种形态——`.revenue_share` 这类字段名不能用
# 泛匹配 `\.\w+`，否则 `segments[2].revenue_share` 会被归一到 `segments@2024.revenue_share`，
# 分部顺序被打乱，对比结果就是假的。
_TAIL_INDEX = re.compile(r"\[(\d+)\](\.value)?$")

# 默认忽略：跑得快慢不是数据变化
DEFAULT_IGNORE = ("pipeline_time",)

# 幅度分档阈值（相对 |Δ| / max(|旧|,|新|)）
NOISE_REL = 1e-6      # 以下视为浮点往返噪声
MATERIAL_REL = 1e-2   # 以上视为实质变化

CLASS_NOISE = "noise"
CLASS_MINOR = "minor"
CLASS_MATERIAL = "material"
CLASS_UNDEFINED = "undefined"   # 任一侧非有限值，不做算术


def align_key(fact) -> str:
    """按「路径 + 期间」生成对齐键；有 as_of 时归一末尾下标。"""
    m = _TAIL_INDEX.search(fact.path)
    if m and fact.as_of:
        return f"{fact.path[:m.start()]}@{fact.as_of}{m.group(2) or ''}"
    return fact.path


def _rel_delta(a: float, b: float) -> float | None:
    if not (math.isfinite(a) and math.isfinite(b)):
        return None
    denom = max(abs(a), abs(b))
    if denom == 0:
        return 0.0
    return abs(a - b) / denom


def _classify(a: float, b: float) -> tuple[str, float | None]:
    rel = _rel_delta(a, b)
    if rel is None:
        return CLASS_UNDEFINED, None
    if rel == 0:
        return "", 0.0
    if rel < NOISE_REL:
        return CLASS_NOISE, rel
    if rel < MATERIAL_REL:
        return CLASS_MINOR, rel
    return CLASS_MATERIAL, rel


def _finite(v: float) -> float | None:
    """非有限值转 None，避免把 NaN 写进接口 JSON。"""
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None


def _ignored(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + ".") or path.startswith(p + "[")
               for p in prefixes)


def _index(facts, ignore) -> tuple[dict, int]:
    """路径 → 事实。同键多次出现时按下标顺序追加 `#n` 保持唯一且确定。"""
    out: dict[str, object] = {}
    dup = 0
    seen: dict[str, int] = {}
    for f in facts:
        if _ignored(f.path, ignore):
            continue
        key = align_key(f)
        n = seen.get(key, 0) + 1
        seen[key] = n
        if n > 1:
            dup += 1
            key = f"{key}#{n}"
        out[key] = f
    return out, dup


def _same(a: float, b: float) -> bool:
    """数值相等判定：NaN 与 NaN 视为同值（都是坏值，不是「改值」）。"""
    if a == b:
        return True
    return math.isnan(a) and math.isnan(b)


def _entry(key, old_fact, new_fact, *, rel=None, cls="", note=None) -> dict:
    old_fact_ = old_fact
    return {
        "key": key,
        "path": (new_fact or old_fact_).path,
        "metric": (new_fact or old_fact_).metric,
        "old_value": _finite(old_fact_.value) if old_fact_ else None,
        "new_value": _finite(new_fact.value) if new_fact else None,
        "as_of_old": old_fact_.as_of if old_fact_ else None,
        "as_of_new": new_fact.as_of if new_fact else None,
        "provenance_old": old_fact_.provenance if old_fact_ else None,
        "provenance_new": new_fact.provenance if new_fact else None,
        "rel_delta": None if rel is None else round(rel, 8),
        "class": cls,
        "note": note,
    }


def diff_facts(
    old_result: dict,
    new_result: dict,
    *,
    ignore: tuple[str, ...] = DEFAULT_IGNORE,
) -> dict:
    """对比两份管线产物的事实，返回五类变化清单与汇总。

    Returns:
        {
          "old": {"run_id", "fact_total", "compared"},
          "new": {...},
          "summary": {added, removed, value_changed, reprovenanced,
                      degraded_flip, noise, minor, material, undefined},
          "changes": {"added": [...], "removed": [...], "value_changed": [...],
                      "reprovenanced": [...], "degraded_flip": [...]},
        }
    """
    lo = build_ledger(old_result)
    ln = build_ledger(new_result)
    old_idx, dup_old = _index(lo.facts, ignore)
    new_idx, dup_new = _index(ln.facts, ignore)

    changes = {"added": [], "removed": [], "value_changed": [],
               "reprovenanced": [], "degraded_flip": []}
    bucket = {CLASS_NOISE: 0, CLASS_MINOR: 0,
              CLASS_MATERIAL: 0, CLASS_UNDEFINED: 0}

    for key in new_idx.keys() - old_idx.keys():
        changes["added"].append(_entry(key, None, new_idx[key]))
    for key in old_idx.keys() - new_idx.keys():
        changes["removed"].append(_entry(key, old_idx[key], None))

    for key in old_idx.keys() & new_idx.keys():
        a, b = old_idx[key], new_idx[key]
        if _same(a.value, b.value):
            # 值没变，才可能只是换了出处
            if a.provenance != b.provenance:
                category = ("degraded_flip"
                            if "degraded" in (a.provenance, b.provenance)
                            else "reprovenanced")
                changes[category].append(_entry(key, a, b))
            continue
        cls, rel = _classify(a.value, b.value)
        if cls in bucket:
            bucket[cls] += 1
        note = "非有限值，未计算幅度" if cls == CLASS_UNDEFINED else None
        changes["value_changed"].append(_entry(key, a, b, rel=rel, cls=cls, note=note))

    # 实质变化优先，方便阅读；同类内按相对幅度降序
    changes["value_changed"].sort(
        key=lambda e: (e["class"] != CLASS_MATERIAL, -(e["rel_delta"] or 0)))

    summary = {
        "added": len(changes["added"]),
        "removed": len(changes["removed"]),
        "value_changed": len(changes["value_changed"]),
        "reprovenanced": len(changes["reprovenanced"]),
        "degraded_flip": len(changes["degraded_flip"]),
        "noise": bucket[CLASS_NOISE],
        "minor": bucket[CLASS_MINOR],
        "material": bucket[CLASS_MATERIAL],
        "undefined": bucket[CLASS_UNDEFINED],
        "duplicate_keys": dup_old + dup_new,
    }
    return {
        "old": {"run_id": lo.run_id, "fact_total": lo.total,
                "compared": len(old_idx)},
        "new": {"run_id": ln.run_id, "fact_total": ln.total,
                "compared": len(new_idx)},
        "summary": summary,
        "changes": changes,
    }


def diff_artifacts(old_path: str | Path, new_path: str | Path, *,
                   ignore: tuple[str, ...] = DEFAULT_IGNORE) -> dict:
    """从两个产物文件直接对比。"""
    def _load(p):
        return json.loads(Path(p).read_text(encoding="utf-8"))

    out = diff_facts(_load(old_path), _load(new_path), ignore=ignore)
    out["old"]["path"] = str(old_path)
    out["new"]["path"] = str(new_path)
    return out
