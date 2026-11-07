"""L1 评测：数值接地（离线，零 LLM 调用）。

L0 问「每个数字有没有登记」，L1 问「模型写出来的数字，能不能在登记里找到」。

两种文本都要过：

  产物文本  标的分析产物里模型撰写的段落（SWOT、地缘、新闻、竞品理由），
            抽出的每个数字都必须能在台账里找到等值事实（按显示位数取整/截断）。
            同一标的只评最新一次产物，被取代的历史产物在报告 `skipped_superseded` 里列名
  报告正文  确定性报价报告的全部数字必须来自注入数据

无法比对的两类单列、不进分母，但都在报告里列出，不静默放过：
  model_judgment  模型自己下的判断（概率/置信度/情绪分），只校验取值域
  future_year     晚于产物生成时间的年份，是预测不是事实

分母只算**数值断言**：带单位/货币/百分号，或处于带单位的区间（600-1000 元）。
名称内嵌数字（国窖1573）、法条年份（《1974年贸易法》）、海关编码（HS 8517/8471）
单列为 non_claim 并抽样列出——它们不是在陈述某个量的值，计入分母只会让
接地率随标的名与措辞抖动。年份统一计入 year_ref，期间一致性交给 L3 的
「期间一致：年份须在事实期间内」，不在 L1 重复计分。

反向控制：往模型文本里塞一个 99.99%，接地率必须下降。控制不生效，
说明匹配器太松，此时比率再高也没有意义。

产出 data/eval/l1_report.json，供 UI 评测看板直接读取。

运行: D:\\python\\python.exe scripts/eval_l1_grounding.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.trust import build_ledger  # noqa: E402
from app.trust import grounding as G  # noqa: E402

ARTIFACT_DIR = ROOT / "data" / "artifacts"
LEGACY_ARTIFACTS = (ROOT / "data" / "phase5_full_aapl.json",)
REPORT_PATH = ROOT / "data" / "eval" / "l1_report.json"
HISTORY_PATH = ROOT / "data" / "eval" / "history.jsonl"

# 门槛 0.98 而非 1.0：残余未接地的典型形态是名称内嵌数字（国窖1573）
# 与模型自己推导的区间端点（WACC 约 8.5-9.5%）。前者是抽取器能力的边界，
# 后者本就该人工复核——所以不把门槛调到刚好让它们通过，而是留出余量、
# 同时把每一条都逐字列出来。
MIN_RATE = 0.98


def discover_artifacts() -> list[Path]:
    """每个标的取最新一次产物。

    L1 度量的是「当前这套提示词与代码写出来的数字有没有出处」，不是历史累积：
    全量计分会让已经被取代的旧产物永久压住分数，改好提示词也翻不了绿。
    历史文件一个不删（趋势另有 history.jsonl），只是同一标的只评最近一次；
    某标的**最新一次**退步仍然会红。被跳过的文件在报告里点名列出，不静默放过。
    """
    found = sorted(ARTIFACT_DIR.glob("**/*.json")) if ARTIFACT_DIR.exists() else []
    found = [p for p in found if not p.name.startswith("_")]
    if not found:
        return [p for p in LEGACY_ARTIFACTS if p.exists()]
    latest: dict[str, Path] = {}
    for p in found:
        key = p.parent.name if p.parent != ARTIFACT_DIR else p.stem
        if key not in latest or p.name > latest[key].name:
            latest[key] = p
    return sorted(latest.values(), key=lambda p: p.parent.name + p.name)


def superseded_artifacts() -> list[str]:
    """被「每标的取最新」规则排除的历史产物，供报告如实列出。"""
    found = sorted(ARTIFACT_DIR.glob("**/*.json")) if ARTIFACT_DIR.exists() else []
    keep = {str(p) for p in discover_artifacts()}
    return [str(p.relative_to(ROOT)) for p in found
            if not p.name.startswith("_") and str(p) not in keep]


def _report_fixture() -> dict:
    """确定性报价报告 + 注入数据：文本接地的老用例，保留为 L1 最小样本。"""
    from app.agent import expressor
    from app.constants import DATA_SOURCE_DISCLAIMER

    quote = {
        "ticker": "AAPL",  # 无数字 ticker，避免代码被误当作数值声明
        "company_name": "贵州茅台",
        "current_price": 123.45,
        "currency": "CNY",
        "change_percent": 0.0234,
        "open": 120.0,
        "previous_close": 118.0,
        "day_high": 125.0,
        "day_low": 119.0,
        "volume": 1234567,
        "market_cap": 123400000000.0,
        "pe_ratio": 25.5,
    }
    execution = {
        "results": [{"step": {"action": "get_quote"}, "data": quote, "error": None}],
        "summary": {"total": 1, "success": 1, "failed": 0},
    }
    report = expressor.generate_report(
        execution, {"intent": "query_price", "tickers": ["AAPL"]})
    body = report.split(DATA_SOURCE_DISCLAIMER)[0]

    base = G.ground_text(body, quote)
    fake = G.ground_text(
        body + "\n**预期增长率**: 99.99%\n", quote)
    return {
        "fixture": "expressor 报价报告",
        "rate": base["rate"],
        "tokens": base["tokens"],
        "unmatched": base["unmatched"],
        "control_detected": fake["rate"] < base["rate"] and bool(fake["unmatched"]),
        "control_rate": fake["rate"],
        "control_value": 99.99,
    }


def evaluate_one(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    ledger = build_ledger(result, ticker=result.get("ticker"))

    prose = G.ground_llm_prose(ledger, result)
    control = G.control_llm_prose(ledger, result)
    probe = G.probe_document_facts(ledger, result)

    checks: list[dict] = []

    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    chk("扫到模型撰写字段（白名单未腐化）", prose["fields"] > 0,
        f"{prose['fields']} 个字段")
    chk(f"接地率 ≥ {MIN_RATE}", prose["rate"] >= MIN_RATE,
        f"{prose['rate']}（未接地 {prose['unmatched']}/{prose['matched'] + prose['unmatched']}；"
        f"另有 {prose['non_claim']} 个非断言 token、{prose['year_ref']} 个年份与 "
        f"{prose['time_ref']} 个时间跨度不计入）")
    chk("反向控制：伪造 99.99% 被检出", control.get("detected", False),
        (f"{control.get('rate_before')} → {control.get('rate_after')}"
         if control.get("applicable") else control.get("note", "")))
    chk("模型判断落在合法取值域", not prose["domain_violations"],
        str(prose["domain_violations"][:3]))
    # 现有产物的片段是模型自己写的研报，几乎不会照抄原句，doc_grounded 天然接近 0。
    # 所以这一项不靠自然命中，而是把片段里真实出现、台账里没有的数写回去，
    # 验证「片段这一路确实接得住」；接不住说明扩展没生效。
    chk("正向控制：片段里的数被计成 doc_grounded",
        (not probe.get("applicable")) or probe["detected"],
        (f"写入 {probe['injected']} → doc_grounded "
         f"{probe['doc_grounded_before']} → {probe['doc_grounded_after']}，"
         f"rate {probe['rate_before']} → {probe['rate_after']}"
         if probe.get("applicable") else probe.get("note", "")))

    metrics = {
        "grounding_rate": prose["rate"],
        "prose_fields": prose["fields"],
        "number_tokens": prose["tokens"],
        "claims": prose["matched"] + prose["unmatched"],
        "matched": prose["matched"],
        "anchored": prose["anchored"],
        "unmatched": prose["unmatched"],
        "doc_facts": prose["doc_facts"],
        "doc_grounded": prose["doc_grounded"],
        "non_claim": prose["non_claim"],
        "non_claim_grounded": prose["non_claim_grounded"],
        "non_claim_unmatched": prose["non_claim_unmatched"],
        "year_ref": prose["year_ref"],
        "time_ref": prose["time_ref"],
        "model_judgment": prose["model_judgment"],
        "future_year": prose["future_year"],
        "ticker_ref": prose["ticker_ref"],
    }
    return {
        "artifact": str(path.relative_to(ROOT)),
        "run_id": ledger.run_id,
        "ticker": ledger.ticker,
        "metrics": metrics,
        "misses": prose["misses"],
        "anchored_matches": prose["anchored_matches"],
        "doc_grounded_probe": probe,
        "non_claim_samples": prose["non_claim_samples"],
        "checks": checks,
        "status": "pass" if all(c["passed"] for c in checks) else "fail",
    }


def _aggregate(per_artifact: list[dict], fixture: dict) -> dict:
    """比率按数字 token 数加权；计数类直接相加。"""
    total_tokens = sum(a["metrics"]["number_tokens"] for a in per_artifact) or 1
    denom = sum(a["metrics"]["matched"] + a["metrics"]["unmatched"] for a in per_artifact) or 1
    matched = sum(a["metrics"]["matched"] for a in per_artifact)
    out = {
        "grounding_rate": round(matched / denom, 4),
        "prose_fields": sum(a["metrics"]["prose_fields"] for a in per_artifact),
        "number_tokens": total_tokens,
        "claims": denom,
        "matched": matched,
        "anchored": sum(a["metrics"]["anchored"] for a in per_artifact),
        "unmatched": sum(a["metrics"]["unmatched"] for a in per_artifact),
        "doc_facts": sum(a["metrics"]["doc_facts"] for a in per_artifact),
        "doc_grounded": sum(a["metrics"]["doc_grounded"] for a in per_artifact),
        "doc_grounded_probe": sum(
            1 for a in per_artifact
            if (a.get("doc_grounded_probe") or {}).get("applicable")),
        "non_claim": sum(a["metrics"]["non_claim"] for a in per_artifact),
        "non_claim_grounded": sum(a["metrics"]["non_claim_grounded"] for a in per_artifact),
        "non_claim_unmatched": sum(a["metrics"]["non_claim_unmatched"] for a in per_artifact),
        "year_ref": sum(a["metrics"]["year_ref"] for a in per_artifact),
        "time_ref": sum(a["metrics"]["time_ref"] for a in per_artifact),
        "model_judgment": sum(a["metrics"]["model_judgment"] for a in per_artifact),
        "future_year": sum(a["metrics"]["future_year"] for a in per_artifact),
        "ticker_ref": sum(a["metrics"]["ticker_ref"] for a in per_artifact),
        "report_text_rate": fixture["rate"],
    }
    return out


def run() -> dict:
    paths = discover_artifacts()
    per_artifact = [evaluate_one(p) for p in paths]
    fixture = _report_fixture()

    passed = sum(len([c for c in a["checks"] if c["passed"]]) for a in per_artifact)
    total_checks = sum(len(a["checks"]) for a in per_artifact)
    if per_artifact:
        passed += int(fixture["rate"] == 1.0) + int(fixture["control_detected"])
        total_checks += 2

    status = ("pass"
              if per_artifact
              and all(a["status"] == "pass" for a in per_artifact)
              and fixture["rate"] == 1.0 and fixture["control_detected"]
              else "fail")

    return {
        "tier": "l1",
        "title": "数值接地",
        "description": "模型写出来的数字，能不能在台账里找到出处",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "threshold": MIN_RATE,
        "artifacts": per_artifact,
        "selection": "每标的取最新一次产物（历史文件保留，不参与计分）",
        "skipped_superseded": superseded_artifacts(),
        "report_text": fixture,
        "checks_passed": passed,
        "checks_total": total_checks,
        "metrics": _aggregate(per_artifact, fixture) if per_artifact
                   else {"grounding_rate": 0.0, "report_text_rate": fixture["rate"]},
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
    print("L1 数值接地评测")
    print("=" * 64)
    for a in report["artifacts"]:
        head = "PASS" if a["status"] == "pass" else "FAIL"
        m = a["metrics"]
        print(f"\n[{head}] {a['artifact']}  ticker={a['ticker']}  "
              f"rate={m['grounding_rate']}  字段={m['prose_fields']}  "
              f"数字={m['number_tokens']}  片段接地={m['doc_grounded']}/{m['doc_facts']}")
        for c in a["checks"]:
            mark = "  ok " if c["passed"] else "  !! "
            print(f"{mark}{c['name']}  ({c['detail']})" if c["detail"]
                  else f"{mark}{c['name']}")
        for miss in a["misses"]:
            gap = miss.get("nearest_gap", miss.get("kind"))
            print(f"    未接地  {miss['raw']:<10} {gap}   {miss['path']}")
        for am in a.get("anchored_matches") or []:
            if am.get("metric"):
                print(f"    复算    {am['raw']:<10} = {am['metric']} "
                      f"{am['periods'][0]}({am['values'][0]}) − "
                      f"{am['periods'][1]}({am['values'][1]})")
            else:
                print(f"    复算    {am['raw']:<10} = 同句 "
                      f"{am['values'][0]} − {am['values'][1]}")
        samples = a.get("non_claim_samples") or []
        if samples:
            shown = ", ".join(s["raw"] for s in samples)
            print(f"    非断言  {m['non_claim']} 个不计入分母"
                  f"（台账可解释 {m['non_claim_grounded']}，无出处 {m['non_claim_unmatched']}）"
                  f"如 {shown}")

    fx = report["report_text"]
    print(f"\n[报告正文] {fx['fixture']}  rate={fx['rate']} 数字={fx['tokens']}")
    print(f"  反向控制  伪造 {fx['control_value']}% → rate={fx['control_rate']} "
          f"(detected={fx['control_detected']})")

    skipped = report.get("skipped_superseded") or []
    if skipped:
        print(f"\n[不参与计分] {len(skipped)} 份历史产物已被同标的更新的一次取代：")
        for s in skipped:
            print(f"  - {s}")

    print("\n" + "-" * 64)
    print(f"断言 {report['checks_passed']}/{report['checks_total']} 通过 → "
          f"{report['status'].upper()}")
    print(f"报告已写: {REPORT_PATH.relative_to(ROOT)}")
    print("=" * 64)
    sys.exit(0 if report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
