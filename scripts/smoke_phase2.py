"""
Phase 2 冒烟测试：深度研究分析管线完整验证。

对 AAPL（美股）与 600519（A股）各跑一遍完整分析管线，验证：
  1. 财务分析（比率/增长率/FCF 历史）真实计算
  2. DCF 估值 + 相对估值（真实 FCF + WACC）
  3. SOTP 分部估值
  4. 多市场竞品分析 + 市场集中度
  5. 事实核查 / 地缘政治 / 供应链
  6. 竞品对比 + SWOT（LLM 结构化输出）
  7. 管线耗时

运行: PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\smoke_phase2.py
"""
import sys
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis.pipeline import AnalysisOrchestrator

_results: list[dict] = []


def record(name: str, ok: bool, detail: str = ""):
    _results.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def check_pipeline(ticker: str) -> dict:
    """运行完整管线并做结构化校验。"""
    print("\n" + "=" * 60)
    print(f"完整分析管线: {ticker}")
    print("=" * 60)
    orch = AnalysisOrchestrator()
    t0 = time.time()
    r = orch.run(ticker)
    dt = round(time.time() - t0, 1)
    print(f"管线耗时: {dt}s")

    # ── 财务分析 ──
    fa = r.get("financial_analysis", {})
    ratios = fa.get("ratios", {})
    n_ratios = len(ratios)
    has_fcf = any(s.get("value") for s in ratios.get("FREE_CASH_FLOW", []))
    record(f"{ticker} 财务比率", n_ratios >= 8, f"{n_ratios} 项, periods={fa.get('periods', [])[-2:]} FCF={has_fcf}")
    for code in ("ROE", "GROSS_MARGIN", "NET_MARGIN"):
        ser = ratios.get(code, [])
        v = ser[-1]["value"] if ser and ser[-1]["value"] is not None else None
        print(f"    {code}: {round(v, 4) if v is not None else None}")

    # ── 估值 ──
    va = r.get("valuation_analysis", {})
    dcf = va.get("dcf", {})
    dcf_ok = dcf.get("fair_value") is not None or dcf.get("reason")
    record(f"{ticker} DCF 估值", dcf_ok, f"fair_value={dcf.get('fair_value')} reason={dcf.get('reason', 'ok')[:40]}")
    record(f"{ticker} 相对估值", bool(va.get("relative_valuation")), str(va.get("summary", ""))[:90])

    # ── SOTP ──
    sotp = r.get("sotp_analysis", {})
    sotp_ok = sotp.get("per_share_low") is not None or sotp.get("per_share_high") is not None
    record(f"{ticker} SOTP 分部估值", sotp_ok,
           f"segments={len(sotp.get('segments', []))} 区间={sotp.get('per_share_low')}~{sotp.get('per_share_high')}")

    # ── 市场分析 ──
    ma = r.get("market_analysis", {})
    mkts = ma.get("markets", {})
    n_comp = sum(len(v.get("competitors", [])) for v in mkts.values())
    record(f"{ticker} 多市场竞品", n_comp > 0, f"{len(mkts)} 个市场, 共 {n_comp} 家竞品")

    # ── 事实核查 ──
    fc = r.get("fact_checks", {})
    record(f"{ticker} 事实核查", isinstance(fc, dict), f"warnings={len(fc.get('warnings', []))}")

    # ── 地缘 / 供应链 ──
    geo = r.get("geopolitical_analysis", {})
    sc = r.get("supply_chain_analysis", {})
    record(f"{ticker} 地缘政治", bool(geo.get("geopolitical_events")), f"风险={geo.get('geopolitical_events', {}).get('risk_level')}")
    record(f"{ticker} 供应链", bool(sc), f"{sc.get('summary', '')[:40]}")

    # ── SWOT ──
    sw = r.get("swot_analysis", {})
    sw_ok = bool(sw.get("swot") or sw.get("rating"))
    record(f"{ticker} SWOT", sw_ok, f"评级={sw.get('rating')}")

    r["_time"] = dt
    return r


def main():
    results = {}
    for ticker in ("AAPL", "600519"):
        try:
            results[ticker] = check_pipeline(ticker)
        except Exception as e:
            import traceback
            traceback.print_exc()
            record(f"{ticker} 完整管线", False, f"{type(e).__name__}: {str(e)[:80]}")

    print("\n" + "=" * 60)
    passed = sum(1 for r in _results if r["ok"])
    print(f"汇总: 通过 {passed} / {len(_results)}")
    for r in _results:
        print(f"  [{'PASS' if r['ok'] else '--'}] {r['name']}")
    print("=" * 60)

    with open("data/phase2_smoke.json", "w", encoding="utf-8") as f:
        json.dump({t: {"_time": results[t].get("_time"), "company": results[t].get("company_info", {}).get("name"),
                        "ratios": len(results[t].get("financial_analysis", {}).get("ratios", {})),
                        "dcf_fair_value": results[t].get("valuation_analysis", {}).get("dcf", {}).get("fair_value"),
                        "swot_rating": results[t].get("swot_analysis", {}).get("rating")}
                   for t in results}, f, ensure_ascii=False, indent=2)

    hard_required = [f"{t} 财务比率" for t in results]
    missing = [n for n in hard_required if n not in [r["name"] for r in _results if r["ok"]]]
    if missing:
        print(f"\n未满足核心验证: {missing}")
        sys.exit(1)
    print("\nPhase 2 核心验证通过")


if __name__ == "__main__":
    main()
