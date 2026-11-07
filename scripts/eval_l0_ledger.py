"""L0 评测：台账闭合性（离线，零 LLM 调用）。

L0 不问「数字对不对」，只问「每个数字是否都被登记在案」。
这是其余四层评测的地基——台账不闭合，后面的接地率、召回率都无从谈起。

对每个产物检查：
  1. 闭合   台账条数 == 独立实现的数值叶子计数
  2. 无盲区 未归类路径（UNKNOWN）必须为 0
  3. 标识   fact_id 唯一且连续；path 唯一
  4. 出处   全部落在五种合法出处内
  5. 确定性 同一产物两次构建，事实序列与内容哈希完全一致
  6. 数值   metric 非空、value 有限（无 NaN/Inf）
  7. 可追溯 degraded / imputed 事实必须带 evidence_ref（说得出为什么降级）
  8. 期间   财务比率类事实必须带 as_of（否则无法审计是哪一期）

产出 data/eval/l0_report.json，供 UI 评测看板直接读取。

运行: D:\\python\\python.exe scripts/eval_l0_ledger.py
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.trust import DEGRADED, IMPUTED, UNKNOWN, build_ledger  # noqa: E402
from app.trust.ledger import content_hash  # noqa: E402
from app.trust.provenance import VALID  # noqa: E402

ARTIFACT_DIR = ROOT / "data" / "artifacts"
LEGACY_ARTIFACTS = (ROOT / "data" / "phase5_full_aapl.json",)
REPORT_PATH = ROOT / "data" / "eval" / "l0_report.json"
HISTORY_PATH = ROOT / "data" / "eval" / "history.jsonl"

# 断言失败的出处（降级/插补）必须能被解释，否则「降级」就成了新的一层遮掩
_MUST_EXPLAIN = frozenset({DEGRADED, IMPUTED})


def count_numeric_leaves(node) -> int:
    """独立实现（不复用 ledger 的遍历），用来交叉验证台账完整性。"""
    if isinstance(node, bool):
        return 0
    if isinstance(node, (int, float)):
        return 1
    if isinstance(node, dict):
        return sum(count_numeric_leaves(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return sum(count_numeric_leaves(v) for v in node)
    return 0


def discover_artifacts() -> list[Path]:
    """优先用采集来的产物；没有则退回仓库里的历史产物。"""
    found = sorted(ARTIFACT_DIR.glob("**/*.json")) if ARTIFACT_DIR.exists() else []
    found = [p for p in found if not p.name.startswith("_")]
    if found:
        return found
    return [p for p in LEGACY_ARTIFACTS if p.exists()]


def evaluate_one(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    ledger = build_ledger(result, ticker=result.get("ticker"))
    checks: list[dict] = []

    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    # 1. 闭合
    expected = count_numeric_leaves(result)
    chk("台账闭合：事实数 == 数值叶子数", ledger.total == expected,
        f"{ledger.total} vs {expected}")

    # 2. 无盲区
    unknown = ledger.unknown()
    chk("无未归类路径", not unknown,
        f"{len(unknown)} 条: {[f.path for f in unknown[:5]]}" if unknown else "0 条")

    # 3. 标识
    ids = [f.fact_id for f in ledger.facts]
    chk("fact_id 唯一", len(ids) == len(set(ids)))
    chk("fact_id 连续编号", ids == [f"F{i:03d}" for i in range(1, len(ids) + 1)])
    paths = [f.path for f in ledger.facts]
    chk("path 唯一", len(paths) == len(set(paths)))

    # 4. 出处合法
    bad_prov = [f.path for f in ledger.facts if f.provenance not in VALID]
    chk("出处均在五种合法值内", not bad_prov,
        f"越界 {len(bad_prov)} 条")

    # 5. 确定性
    again = build_ledger(result, ticker=result.get("ticker"))
    chk("两次构建事实序列一致",
        [f.to_dict() for f in again.facts] == [f.to_dict() for f in ledger.facts])
    chk("内容哈希可复算", content_hash(result) == ledger.artifact_hash)
    chk("run_id 由内容派生", ledger.run_id == ledger.artifact_hash[:12])

    # 6. 数值有限
    bad_num = [f.path for f in ledger.facts
               if not f.metric or not math.isfinite(f.value)]
    chk("metric 非空且 value 有限", not bad_num, f"{len(bad_num)} 条异常")

    # 7. 降级可追溯
    silent = [f.path for f in ledger.facts
              if f.provenance in _MUST_EXPLAIN and not f.evidence_ref]
    chk("降级/插补事实均带 evidence_ref", not silent,
        f"{len(silent)} 条静默降级: {silent[:5]}" if silent else "")

    # 8. 财务比率期间
    ratio_metrics = {"ROE", "ROA", "GROSS_MARGIN", "NET_MARGIN", "EV_EBITDA"}
    no_asof = [f.path for f in ledger.facts
               if f.metric in ratio_metrics and not f.as_of]
    chk("财务比率带期间 as_of", not no_asof,
        f"{len(no_asof)} 条缺期间: {no_asof[:5]}" if no_asof else "")

    total = ledger.total or 1
    counts = ledger.by_provenance()
    metrics = {
        "fact_total": ledger.total,
        "unknown_count": len(unknown),
        "computed_rate": round(counts.get("computed", 0) / total, 4),
        "source_rate": round(counts.get("source", 0) / total, 4),
        "llm_rate": round(counts.get("llm", 0) / total, 4),
        "imputed_rate": round(counts.get("imputed", 0) / total, 4),
        "degraded_rate": round(counts.get("degraded", 0) / total, 4),
        "as_of_coverage": round(
            sum(1 for f in ledger.facts if f.as_of) / total, 4),
        "unit_coverage": round(
            sum(1 for f in ledger.facts if f.unit) / total, 4),
    }

    return {
        "artifact": str(path.relative_to(ROOT)),
        "run_id": ledger.run_id,
        "ticker": ledger.ticker,
        "counts": counts,
        "metrics": metrics,
        "checks": checks,
        "status": "pass" if all(c["passed"] for c in checks) else "fail",
    }


def run() -> dict:
    """返回统一结构的评测报告；供 run_eval.py 汇总与 UI 读取。"""
    paths = discover_artifacts()
    per_artifact = [evaluate_one(p) for p in paths]

    passed = sum(len([c for c in a["checks"] if c["passed"]]) for a in per_artifact)
    total_checks = sum(len(a["checks"]) for a in per_artifact)
    status = "pass" if per_artifact and all(a["status"] == "pass" for a in per_artifact) else "fail"

    report = {
        "tier": "l0",
        "title": "台账闭合性",
        "description": "每个数值是否都被登记在案，并说得出出处与期间",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "artifacts": per_artifact,
        "checks_passed": passed,
        "checks_total": total_checks,
        # 单产物直接沿用其指标（保留整数类型），多产物按事实数加权
        "metrics": (dict(per_artifact[0]["metrics"]) if len(per_artifact) == 1
                    else _aggregate(per_artifact)),
    }
    return report


# 可加总的计数类指标（其余都是比率，按事实数加权）
_ADDITIVE = frozenset({"fact_total", "unknown_count"})


def _aggregate(per_artifact: list[dict]) -> dict:
    """多产物时按事实数加权，避免小产物拉偏均值。"""
    weight = sum(a["metrics"]["fact_total"] for a in per_artifact) or 1
    keys = per_artifact[0]["metrics"].keys()
    out: dict[str, float] = {}
    for k in keys:
        if k in _ADDITIVE:
            out[k] = sum(a["metrics"][k] for a in per_artifact)
        else:
            out[k] = round(
                sum(a["metrics"][k] * a["metrics"]["fact_total"] for a in per_artifact) / weight,
                4)
    return out


def save(report: dict) -> Path:
    """写本次报告 + 追加一行历史（UI 看趋势用，追加不覆盖）。"""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "tier": report["tier"],
            "generated_at": report["generated_at"],
            "status": report["status"],
            "checks_passed": report["checks_passed"],
            "checks_total": report["checks_total"],
            "metrics": report["metrics"],
        }, ensure_ascii=False) + "\n")
    return REPORT_PATH


def main() -> None:
    report = run()
    save(report)

    print("=" * 64)
    print("L0 台账闭合性评测")
    print("=" * 64)
    for a in report["artifacts"]:
        head = "PASS" if a["status"] == "pass" else "FAIL"
        print(f"\n[{head}] {a['artifact']}  ticker={a['ticker']}  "
              f"run_id={a['run_id']}  facts={a['metrics']['fact_total']}")
        for c in a["checks"]:
            mark = "  ok " if c["passed"] else "  !! "
            suffix = f"  ({c['detail']})" if c["detail"] else ""
            print(f"{mark}{c['name']}{suffix}")

    m = report["metrics"]
    print("\n" + "-" * 64)
    print(f"聚合指标  事实 {m['fact_total']} 条")
    print(f"  出处分布  computed={m['computed_rate']:.1%}  source={m['source_rate']:.1%}  "
          f"llm={m['llm_rate']:.1%}  imputed={m['imputed_rate']:.1%}  "
          f"degraded={m['degraded_rate']:.1%}")
    print(f"  期间覆盖  as_of_coverage={m['as_of_coverage']:.1%}   "
          f"unit_coverage={m['unit_coverage']:.1%}")
    print(f"  未归类    unknown={m['unknown_count']}")
    print("-" * 64)
    print(f"断言 {report['checks_passed']}/{report['checks_total']} 通过 → "
          f"{report['status'].upper()}")
    print(f"报告已写: {REPORT_PATH.relative_to(ROOT)}")
    print("=" * 64)
    sys.exit(0 if report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
