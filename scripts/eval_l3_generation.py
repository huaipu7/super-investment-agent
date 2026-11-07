"""L3 评测：生成质量「规则即断言」（离线，零 LLM 调用）。

不用自由式 LLM 打分。理由：分数不可复现、无法归因、进不了回归门槛。
这里每条都是确定性断言，失败时能直接指出是哪份产物、哪一行不过：

  1 期间一致    文中提到的年份必须在事实期间内；未来年份必须带预测措辞
  2 竞品真实    竞品名必须出现在数据源返回的名单里
  3 敏感性单调  DCF 热力图：折现率↑估值↓、永续增长率↑估值↑
  4 分部占比    各分部占比之和 = 1，且渲染后必须标注「模型估计」
  5 措辞红线    不得出现确定性承诺表述
  6 空节        渲染后的每个章节都必须有内容
  7 红队引用    产物带 redteam 时，每条反方论证必须挂得住本产物内的事实引用
               （默认不跑红队，故多数产物这一条是「无红队段落」直接通过）

产出 data/eval/l3_report.json，供 UI 评测看板直接读取。

运行: D:\\python\\python.exe scripts/eval_l3_generation.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.compliance.rules import check_certainty  # noqa: E402
from app.trust import build_ledger  # noqa: E402
from app.trust import grounding as G  # noqa: E402

ARTIFACT_DIR = ROOT / "data" / "artifacts"
LEGACY_ARTIFACTS = (ROOT / "data" / "phase5_full_aapl.json",)
REPORT_PATH = ROOT / "data" / "eval" / "l3_report.json"
HISTORY_PATH = ROOT / "data" / "eval" / "history.jsonl"

SHARE_TOL = 1e-6
_YEAR_RE = re.compile(r"(20\d{2})年")
# 未来年份必须和这些词同句出现，否则就是在把预测写成了事实
_FORECAST = ("将", "预计", "预期", "预测", "展望", "有望", "若", "可能")
_SHARE_LABEL = "分部占比为模型估计"
_SCENARIO_LABEL = "模型情景推演"
# 情景推演段落里的年份天然是未来，不该按「事实期间」校验；
# 换成要求它必须被标注成推演（见 `_check_scenario_label`）。
_SCENARIO_PREFIX = "geopolitical_analysis.geopolitical_events."


def discover_artifacts() -> list[Path]:
    found = sorted(ARTIFACT_DIR.glob("**/*.json")) if ARTIFACT_DIR.exists() else []
    found = [p for p in found if not p.name.startswith("_")]
    if found:
        return found
    return [p for p in LEGACY_ARTIFACTS if p.exists()]


def _check_period(artifact: dict, ledger) -> tuple[bool, str]:
    """文中年份必须落在事实期间内；未来年份须带预测措辞。"""
    years = {f.as_of[:4] for f in ledger.facts if f.as_of}
    years = {y for y in years if y.isdigit()}
    latest = max((int(y) for y in years), default=0)
    bad: list[str] = []
    for path, text in G.iter_llm_prose(artifact):
        if path.startswith(_SCENARIO_PREFIX):
            continue
        for y in _YEAR_RE.findall(text):
            if y in years:
                continue
            if latest and int(y) > latest and any(m in text for m in _FORECAST):
                continue
            bad.append(f"{y}@{path}")
    return not bad, (f"越界 {len(bad)}: {bad[:3]}" if bad else f"期间 {min(years, default='—')}~{max(years, default='—')}")


def _check_scenario_label(artifact: dict, markdown: str) -> tuple[bool, str]:
    """有情景推演的产物，报告里必须标明那不是事实陈述。"""
    events = (((artifact.get("geopolitical_analysis") or {})
               .get("geopolitical_events") or {}).get("events") or [])
    if not events:
        return True, "无情景段落"
    ok = _SCENARIO_LABEL in markdown
    return ok, (f"{len(events)} 条情景，已标注" if ok
                else f"{len(events)} 条情景但报告未标注")


def _check_competitors(artifact: dict) -> tuple[bool, str]:
    """模型写的竞品名必须能在数据源返回的名单里找到（比对发生在 validator 层）。"""
    src = {str(c.get("name") or "").strip()
           for c in (artifact.get("market_analysis") or {}).get("competitors") or []}
    comps = [str(c.get("name") or "").strip()
             for c in (artifact.get("competitive_analysis") or {}).get("competitors") or []]
    corrected = [(c.get("name_original"), c.get("name"))
                 for c in (artifact.get("fact_checks") or {}).get("corrected_competitors") or []
                 if c.get("name") and c.get("name") != c.get("name_original")]
    if not comps:
        return True, "无竞品段落"
    unknown = [c for c in comps if c and c not in src]
    detail = f"{len(comps)} 个竞品，修正 {len(corrected)} 个"
    if unknown:
        detail += f"，未在数据源: {unknown[:3]}"
    return not unknown, detail


def _check_sensitivity(artifact: dict) -> tuple[bool, str]:
    """折现率↑估值↓（行），永续增长率↑估值↑（列）。None 单元格跳过。"""
    sens = ((artifact.get("valuation_analysis") or {}).get("dcf") or {}).get("sensitivity") or {}
    matrix = sens.get("matrix") or []
    if len(matrix) < 2 or not matrix[0]:
        return True, "无敏感性矩阵"

    def val(i, j):
        try:
            v = matrix[i][j]
        except (IndexError, TypeError):
            return None
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    bad: list[str] = []
    rows, cols = len(matrix), max(len(r) for r in matrix)
    for j in range(cols):  # 同一增长率下，WACC 越高估值越低
        col = [val(i, j) for i in range(rows)]
        seq = [(i, v) for i, v in enumerate(col) if v is not None]
        for (i0, a), (i1, b) in zip(seq, seq[1:]):
            if b > a + SHARE_TOL:
                bad.append(f"WACC↑ 估值反升 行{i0}→{i1} 列{j}: {a}→{b}")
    for i in range(rows):  # 同一 WACC 下，增长率越高估值越高
        row = [(j, val(i, j)) for j in range(len(matrix[i]))]
        seq = [(j, v) for j, v in row if v is not None]
        for (j0, a), (j1, b) in zip(seq, seq[1:]):
            if b < a - SHARE_TOL:
                bad.append(f"增长率↑ 估值反降 行{i} 列{j0}→{j1}: {a}→{b}")
    return not bad, (f"违反 {len(bad)}: {bad[:2]}" if bad else f"{rows}×{cols} 矩阵单调")


def _check_sotp(artifact: dict, markdown: str) -> tuple[bool, str]:
    """各分部占比之和 = 1；有占比的表必须标注模型估计。"""
    segments = (artifact.get("sotp_analysis") or {}).get("segments") or []
    if not segments:
        return True, "无分部数据"
    bad: list[str] = []
    sums: dict[str, float] = {}
    for key in ("revenue_share", "net_income_share"):
        vals = [s.get(key) for s in segments if isinstance(s.get(key), (int, float))]
        if not vals:
            continue
        total = sum(vals)
        sums[key] = round(total, 6)
        if abs(total - 1.0) > SHARE_TOL:
            bad.append(f"{key} 合计 {total:.6f}")
    if _SHARE_LABEL not in markdown:
        bad.append(f"报告未标注「{_SHARE_LABEL}」")
    return not bad, (f"违反: {bad}" if bad else f"合计 {sums}")


def _check_wording(markdown: str) -> tuple[bool, str]:
    hits = check_certainty(markdown)
    return not hits, (f"命中: {hits}" if hits else "无确定性承诺")


def _check_redteam(artifact: dict) -> tuple[bool, str]:
    """红队「没有引用就不许说话」：产物带 redteam 时，每条反方论证都必须挂得住引用。

    引用必须能在**这份产物自己**的事实池里取到——挂一条别处来的引用不算数。
    产物没有 redteam 字段时直接通过：默认不触发红队，L3 数字不受影响。
    """
    from app.agent import redteam as R

    rt = artifact.get("redteam")
    if rt is None:
        return True, "无红队段落"
    pool = R.ref_pool(artifact)
    problems = R.validate_redteam(rt, pool)
    if problems:
        return False, "；".join(problems[:3])
    return True, (f"{len(rt.get('counter_arguments') or [])} 条反方论证全部有真实引用"
                  f"（status={rt.get('status')}，可引用事实 {len(pool)} 条）")


def _check_empty_sections(markdown: str) -> tuple[bool, str]:
    """每个二级标题下都必须有内容——留白等于把「没生成」伪装成「没内容」。"""
    lines = markdown.splitlines()
    sections: list[tuple[str, list[str]]] = []
    for line in lines:
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    if not sections:
        return False, "报告没有任何章节"
    empty = [name for name, body in sections
             if not "\n".join(body).strip()]
    return not empty, (f"空章节: {empty}" if empty else f"{len(sections)} 个章节均有内容")


def evaluate_one(path: Path) -> dict:
    from app.report.markdown import result_to_markdown

    result = json.loads(path.read_text(encoding="utf-8"))
    ledger = build_ledger(result, ticker=result.get("ticker"))
    markdown = result_to_markdown(result)

    checks: list[dict] = []
    for name, (ok, detail) in (
        ("期间一致：年份须在事实期间内", _check_period(result, ledger)),
        ("情景推演已标注（非事实陈述）", _check_scenario_label(result, markdown)),
        ("竞品真实：名称须来自数据源", _check_competitors(result)),
        ("敏感性单调：折现率↑估值↓", _check_sensitivity(result)),
        ("分部占比自洽且已标注", _check_sotp(result, markdown)),
        ("措辞红线：无确定性承诺", _check_wording(markdown)),
        ("无空章节", _check_empty_sections(markdown)),
        ("红队引用可核对：反方论证必须有 evidence_ref", _check_redteam(result)),
    ):
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    return {
        "artifact": str(path.relative_to(ROOT)),
        "run_id": ledger.run_id,
        "ticker": ledger.ticker,
        "checks": checks,
        "status": "pass" if all(c["passed"] for c in checks) else "fail",
    }


def run() -> dict:
    paths = discover_artifacts()
    per_artifact = [evaluate_one(p) for p in paths]
    passed = sum(len([c for c in a["checks"] if c["passed"]]) for a in per_artifact)
    total = sum(len(a["checks"]) for a in per_artifact)
    status = "pass" if per_artifact and all(a["status"] == "pass" for a in per_artifact) else "fail"

    return {
        "tier": "l3",
        "title": "生成质量（规则即断言）",
        "description": "七条确定性断言，失败时能指出是哪份产物的哪一条",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "artifacts": per_artifact,
        "checks_passed": passed,
        "checks_total": total,
        "metrics": {
            "rules": len(per_artifact[0]["checks"]) if per_artifact else 0,
            "rules_passed": passed,
            "rules_total": total,
            "pass_rate": round(passed / total, 4) if total else 0.0,
        },
    }


def save(report: dict) -> Path:
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
    print("L3 生成质量评测（规则即断言）")
    print("=" * 64)
    for a in report["artifacts"]:
        head = "PASS" if a["status"] == "pass" else "FAIL"
        print(f"\n[{head}] {a['artifact']}  ticker={a['ticker']}")
        for c in a["checks"]:
            mark = "  ok " if c["passed"] else "  !! "
            print(f"{mark}{c['name']}  ({c['detail']})")

    print("\n" + "-" * 64)
    print(f"断言 {report['checks_passed']}/{report['checks_total']} 通过 → "
          f"{report['status'].upper()}")
    print(f"报告已写: {REPORT_PATH.relative_to(ROOT)}")
    print("=" * 64)
    sys.exit(0 if report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
