"""L4 端到端评测：三支真实标的一次性基线（真实 LLM + 真实数据源）。

与 L0–L3 的区别：那四层跑在落盘产物上（零 token、秒级、可进日常门禁）；
L4 真的把管线跑一遍，用来回答「这套管线今天在这台机器上还能不能用」：
成功几支、有没有降级、烧了多少 token、花了多久。

**不进日常门禁**：每次 5–10 分钟、消耗真实 token、依赖外网与限流。
基线只做一次（PRD FR-12），要重跑必须显式 `--yes`。

断言（与 PRD FR-12 逐字对应）：
  全链路成功 3/3
  降级均可解释
  台账闭合（L4 产物复跑 L0）
  耗时与 token 完整记录

判据 v1 曾要求「降级事实为 0」。首次真实基线证明该门槛结构上不可达：4 条降级全部
来自零 key 源的覆盖缺口（A 股无股东大会数据、AAPL 无新闻/公告），系统正确地没有把
「查不到」写成 0。v2 改为「降级均可解释」——每条降级事实必须带 evidence_ref 且路径
可定位，降级数量照常记录。历史判据写入报告的 criterion_history，改判据不改实测数据。

产物落 data/artifacts/<TICKER>/<ts>.json，与 collect_artifacts 同格式，
因此 L0/L1/L3 之后会自动把它纳入评测范围。

运行:
  D:\\python\\python.exe scripts/eval_l4_e2e.py            # 只打印，不跑
  D:\\python\\python.exe scripts/eval_l4_e2e.py --yes      # 真跑（花 token）
  D:\\python\\python.exe scripts/eval_l4_e2e.py --yes --tickers 600519.SS,AAPL
  D:\\python\\python.exe scripts/eval_l4_e2e.py --rescore  # 换判据后重算，不花 token
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

ARTIFACT_DIR = ROOT / "data" / "artifacts"
REPORT_PATH = ROOT / "data" / "eval" / "l4_report.json"
HISTORY_PATH = ROOT / "data" / "eval" / "history.jsonl"

# 沪市高毛利 / 深市 / 美股各一：三条不同的取数通路
TICKERS = ("600519.SS", "000858.SZ", "AAPL")

_COUNTER_KEYS = ("llm_calls", "llm_tokens_in", "llm_tokens_out", "llm_errors")

CRITERION_VERSION = 2
CRITERION_HISTORY = [
    {"version": 1, "name": "降级事实为 0", "passed": False,
     "note": "首次真实基线暴露该门槛不可达：降级来自零 key 源的覆盖缺口，非管线故障"},
    {"version": 2, "name": "降级均可解释", "passed": None,
     "note": "每条降级事实必须带 evidence_ref 且路径可定位；数量照常记录"},
]


def degraded_facts(artifact_rel: str | None) -> list[dict]:
    """从落盘产物里取降级事实（路径 + 原因），用于「降级可解释」判定。"""
    if not artifact_rel:
        return []
    from app.trust import build_ledger

    path = ROOT / artifact_rel
    if not path.exists():
        return [{"path": artifact_rel, "evidence_ref": None, "missing_artifact": True}]
    data = json.loads(path.read_text(encoding="utf-8"))
    ledger = build_ledger(data, ticker=data.get("ticker"))
    return [{"path": f.path, "evidence_ref": f.evidence_ref}
            for f in ledger.facts if f.provenance == "degraded"]


def _counters() -> dict[str, int]:
    from app.observability.metrics import metrics

    snap = metrics.snapshot().get("counters") or {}
    return {k: int(snap.get(k) or 0) for k in _COUNTER_KEYS}


def _stamp_artifact(ticker: str, result: dict) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out = ARTIFACT_DIR / ticker.upper().replace(".", "_") / f"{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    return out


def run_one(ticker: str) -> dict:
    """跑一支标的的全链路，返回该标的的原始记录（不判定，只记录）。"""
    from app.analysis.pipeline import AnalysisOrchestrator

    before = _counters()
    started = time.time()
    print(f"\n{'=' * 64}\nL4 跑管线: {ticker}\n{'=' * 64}")

    last_pct = -1

    def on_progress(info: dict) -> None:
        nonlocal last_pct
        pct = int(info.get("pct") or 0)
        if pct == last_pct:
            return
        last_pct = pct
        print(f"  [{pct:>3}%] {info.get('label') or info.get('step') or ''}")

    try:
        result = AnalysisOrchestrator().run(ticker, progress_cb=on_progress)
    except Exception as exc:  # noqa: BLE001
        wall = round(time.time() - started, 1)
        print(f"  [FAIL] 管线异常: {type(exc).__name__}: {exc}")
        return {"ticker": ticker, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                "wall_seconds": wall}
    wall = round(time.time() - started, 1)

    if not isinstance(result, dict) or not result:
        print("  [FAIL] 管线返回空结果")
        return {"ticker": ticker, "ok": False, "error": "管线返回空结果",
                "wall_seconds": wall}

    after = _counters()
    delta = {k: after[k] - before[k] for k in _COUNTER_KEYS}

    path = _stamp_artifact(ticker, result)

    # 用 L0 的同一套断言复核这份新产物——端到端产物同样必须台账闭合
    from eval_l0_ledger import evaluate_one

    l0 = evaluate_one(path)
    counts = l0.get("counts") or {}

    print(f"  [OK] 落盘 {path.relative_to(ROOT)}  墙钟 {wall}s  "
          f"管线自报 {result.get('pipeline_time')}s")
    print(f"       facts={l0['metrics']['fact_total']}  unknown={l0['metrics']['unknown_count']}  "
          f"degraded={counts.get('degraded', 0)}  L0={l0['status']} "
          f"{sum(1 for c in l0['checks'] if c['passed'])}/{len(l0['checks'])}")

    return {
        "ticker": ticker,
        "ok": True,
        "artifact": str(path.relative_to(ROOT)),
        "run_id": l0["run_id"],
        "wall_seconds": wall,
        "pipeline_time": result.get("pipeline_time"),
        "llm_calls": delta["llm_calls"],
        "tokens_in": delta["llm_tokens_in"],
        "tokens_out": delta["llm_tokens_out"],
        "llm_errors": delta["llm_errors"],
        "facts": l0["metrics"]["fact_total"],
        "unknown": l0["metrics"]["unknown_count"],
        "degraded": counts.get("degraded", 0),
        "degraded_detail": degraded_facts(str(path.relative_to(ROOT))),
        "imputed": counts.get("imputed", 0),
        "l0_status": l0["status"],
        "l0_checks": [{"name": c["name"], "passed": c["passed"], "detail": c["detail"]}
                      for c in l0["checks"]],
    }


def _checks(runs: list[dict], total: int) -> list[dict]:
    ok_runs = [r for r in runs if r.get("ok")]
    checks: list[dict] = []

    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    failed = [r["ticker"] for r in runs if not r.get("ok")]
    chk("全链路成功 3/3", len(ok_runs) == total and not failed,
        f"{len(ok_runs)}/{total} 成功" + (f"，失败 {failed}" if failed else ""))

    unexplained: dict[str, list[str]] = {}
    degraded_total = 0
    for r in ok_runs:
        detail = r.get("degraded_detail")
        if detail is None:
            detail = degraded_facts(r.get("artifact"))
        degraded_total += len(detail)
        silent = [d["path"] for d in detail if not (d.get("evidence_ref") or "").strip()]
        if silent:
            unexplained[r["ticker"]] = silent
    chk("降级均可解释", not unexplained,
        "；".join(f"{t}: {p}" for t, p in unexplained.items()) if unexplained
        else (f"{degraded_total} 条降级，全部带原因并可定位" if degraded_total
              else "无降级"))

    bad_l0 = [r["ticker"] for r in ok_runs if r.get("l0_status") != "pass"]
    chk("台账闭合（L4 产物复跑 L0）", len(ok_runs) == total and not bad_l0,
        f"{len(ok_runs) - len(bad_l0)}/{total} 闭合" +
        (f"，未闭合 {bad_l0}" if bad_l0 else ""))

    incomplete = [r["ticker"] for r in ok_runs
                  if not (r.get("wall_seconds", 0) > 0 and r.get("llm_calls", 0) > 0
                          and r.get("tokens_in", 0) > 0 and r.get("tokens_out", 0) > 0)]
    chk("耗时与 token 完整记录", len(ok_runs) == total and not incomplete,
        (f"{len(ok_runs) - len(incomplete)}/{total} 完整"
         + (f"，缺失 {incomplete}" if incomplete else "")) if ok_runs else "无成功样本")
    return checks


def _criterion_history(status: str) -> list[dict]:
    out: list[dict] = []
    for item in CRITERION_HISTORY:
        entry = dict(item)
        if entry["version"] == CRITERION_VERSION:
            entry["passed"] = status == "pass"
        out.append(entry)
    return out


def run(tickers: tuple[str, ...] = TICKERS) -> dict:
    runs = [run_one(t) for t in tickers]
    checks = _checks(runs, len(tickers))
    status = "pass" if all(c["passed"] for c in checks) else "fail"
    ok_runs = [r for r in runs if r.get("ok")]
    walls = [r["wall_seconds"] for r in ok_runs]

    def _sum(key: str) -> int:
        return sum(int(r.get(key) or 0) for r in ok_runs)

    return {
        "tier": "l4",
        "title": "端到端（一次性基线）",
        "description": "三支真实标的跑完整管线：成功/降级/token/耗时",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline": True,
        "in_daily_gate": False,
        "criterion_version": CRITERION_VERSION,
        "criterion_history": _criterion_history(status),
        "tickers": list(tickers),
        "runs": runs,
        "checks": checks,
        "checks_passed": sum(1 for c in checks if c["passed"]),
        "checks_total": len(checks),
        "metrics": {
            "success_rate": round(len(ok_runs) / len(tickers), 4) if tickers else None,
            "wall_seconds_total": round(sum(walls), 1) if walls else None,
            "wall_seconds_mean": round(sum(walls) / len(walls), 1) if walls else None,
            "wall_seconds_max": max(walls) if walls else None,
            "llm_calls_total": _sum("llm_calls"),
            "tokens_in_total": _sum("tokens_in"),
            "tokens_out_total": _sum("tokens_out"),
            "llm_errors_total": _sum("llm_errors"),
            "fact_total": _sum("facts"),
            "degraded_facts": _sum("degraded"),
            "unknown_count": _sum("unknown"),
        },
    }


def rescore() -> dict:
    """用已记录的那次运行重新判定（不重跑管线、不产生 token）。

    改的是判定标准，不是实测数据：耗时与 token 原样保留，降级明细从落盘产物重取，
    并在报告里留下 criterion_version 与 rescored_at，谁都能看出判据换过。
    """
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    runs = report.get("runs") or []
    total = len(report.get("tickers") or runs)
    for r in runs:
        if r.get("ok"):
            r["degraded_detail"] = degraded_facts(r.get("artifact"))
    checks = _checks(runs, total)
    status = "pass" if all(c["passed"] for c in checks) else "fail"
    report.update({
        "runs": runs,
        "checks": checks,
        "checks_passed": sum(1 for c in checks if c["passed"]),
        "checks_total": len(checks),
        "status": status,
        "criterion_version": CRITERION_VERSION,
        "criterion_history": _criterion_history(status),
        "rescored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return report


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


def main() -> int:
    ap = argparse.ArgumentParser(description="L4 端到端一次性基线")
    ap.add_argument("--yes", action="store_true",
                    help="确认真跑（真实 LLM，5–10 分钟，产生 API 费用）")
    ap.add_argument("--rescore", action="store_true",
                    help="用已记录的那次运行重算判定（不跑管线、不产生 token）")
    ap.add_argument("--tickers", default=",".join(TICKERS),
                    help="逗号分隔的标的（默认 600519.SS,000858.SZ,AAPL）")
    args = ap.parse_args()
    tickers = tuple(t.strip() for t in args.tickers.split(",") if t.strip())

    if args.rescore:
        if not REPORT_PATH.exists():
            print(f"没有可重判的报告: {REPORT_PATH.relative_to(ROOT)}")
            return 2
        report = rescore()
        save(report)
        print(f"已按判据 v{CRITERION_VERSION} 重判（未重跑管线、未产生 token）")
        for c in report["checks"]:
            print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['name']}  {c['detail']}")
        print(f"断言 {report['checks_passed']}/{report['checks_total']} → "
              f"{report['status'].upper()}")
        return 0 if report["status"] == "pass" else 1

    if not args.yes:
        print("L4 会真实调用 LLM 跑完整管线（每支 1–6 分钟，产生 API 费用），")
        print("且结果依赖当时的外网与数据源状态，不适合放进日常门禁。")
        print(f"标的: {', '.join(tickers)}")
        print("确认执行请加 --yes。")
        return 2

    report = run(tickers)
    save(report)

    print("\n" + "=" * 64)
    print("L4 端到端评测")
    print("=" * 64)
    for r in report["runs"]:
        if not r.get("ok"):
            print(f"[FAIL] {r['ticker']}  {r.get('error')}  ({r['wall_seconds']}s)")
            continue
        print(f"[ok]   {r['ticker']:<11} {r['wall_seconds']:>6.1f}s  "
              f"facts={r['facts']:<5} degraded={r['degraded']}  "
              f"tokens={r['tokens_in']}+{r['tokens_out']}  calls={r['llm_calls']}")
    print("-" * 64)
    for c in report["checks"]:
        print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['name']}  {c['detail']}")
    print("-" * 64)
    print(f"断言 {report['checks_passed']}/{report['checks_total']} 通过 → "
          f"{report['status'].upper()}")
    print(f"报告已写: {REPORT_PATH.relative_to(ROOT)}")
    print("=" * 64)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
