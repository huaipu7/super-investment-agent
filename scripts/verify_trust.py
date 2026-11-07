"""P3 验收：降级显式化与插补可追溯（离线，零 LLM 调用）。

治理的不是「算得对不对」，而是「算不出来时说了什么」。每一条都在防同一件事：
失败被伪装成正常结果。

  1. 开关    TRUST_STRICT_MODE 默认开启，strict_mode() 读得到
  2. SWOT    LLM 失败 → 评级留空 + degraded + reason（不再冒充「中性」）
  3. 分部    LLM 失败 → 占位分部带 degraded；SOTP 不出数并说明原因
  4. 供应链  输入缺失 → 逐项登记假设 + degraded；输入齐全 → 不得误标降级
  5. 新闻    文章有、舆情失败 → 只标记分析降级，真实计数不被污染
  6. 解析    结构化输出失败可计数（llm_error_rate 的数据源）
  7. 插补    清洗阶段的填充记录进入产物，且逐条带 imputed 标记
  8. 台账    degraded 向下传播；news 的真实计数保持 computed 不被传染
  9. 报告    评级未生成时写明「未生成」，不静默略过

运行: D:\\python\\python.exe scripts/verify_trust.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.trust import IMPUTED, build_ledger, strict_mode  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


class _BadClient:
    """返回非 JSON 文本，模拟模型跑飞。"""

    def chat(self, *args, **kwargs):
        return "模型跑飞了，这不是 JSON"


def main() -> None:
    print("=" * 62)
    print("P3 降级显式化验收")
    print("=" * 62)

    # ── 1. 开关 ──
    print("\n[1] 严格模式开关")
    check("strict_mode() 默认开启", strict_mode() is True, str(strict_mode()))

    # ── 2. SWOT ──
    print("\n[2] SWOT 不再冒充「中性」")
    from app.analysis.swot import SwotAnalyzer

    swot = SwotAnalyzer()
    swot._client = _BadClient()
    out = swot.analyze("测试公司")
    check("评级留空", out.get("rating") is None, str(out.get("rating")))
    check("标记 degraded", out.get("degraded") is True)
    check("写明原因", bool(out.get("reason")), str(out.get("reason")))
    check("SWOT 结构仍在（不抛错）", out.get("swot", {}).get("strengths") == [])

    # ── 3. 分部与 SOTP ──
    print("\n[3] 分部识别失败不产出假 SOTP")
    from app.analysis.segment import SegmentAnalyzer

    seg_an = SegmentAnalyzer()
    seg_an._client = _BadClient()
    segs = seg_an.identify_segments({"name": "测试公司", "industry": "白酒",
                                     "summary": "卖酒"})
    check("退回单分部占位", len(segs) == 1 and segs[0]["name"] == "综合业务")
    check("占位分部带 degraded", segs[0].get("degraded") is True)
    check("写明是占位而非识别结果", bool(segs[0].get("reason")))

    sotp = seg_an.compute_sotp(segs, total_net_income=1000.0,
                               shares_outstanding=100.0, current_price=50.0)
    check("SOTP 数值为空", sotp.get("sotp_value_low") is None
          and sotp.get("per_share_low") is None)
    check("SOTP 标记降级并说明单分部不构成加总",
          sotp.get("degraded") is True and "加总" in (sotp.get("reason") or ""),
          str(sotp.get("reason")))
    check("分部明细保留（可回溯为何失败）", len(sotp.get("segments", [])) == 1)

    # 正常分部不受影响
    good = seg_an.compute_sotp(
        [{"name": "高端酒", "revenue_share": 0.7, "net_income_share": 0.8,
          "segment_type": "消费"}],
        total_net_income=1000.0, shares_outstanding=100.0, current_price=50.0)
    check("正常分部仍能算出 SOTP", good.get("sotp_value_low") is not None
          and not good.get("degraded"))

    # ── 4. 供应链假设登记 ──
    print("\n[4] 供应链：输入缺失要把假设摆到台面上")
    from app.analysis.supply_chain import SupplyChainAnalyzer

    sc = SupplyChainAnalyzer()
    empty = sc.analyze_disruption_scenarios(
        company_info={}, financial_analysis={}, geopolitical_analysis={},
        industry_cls=None, sc_cfg={"has_global_supply_chain": True})
    check("缺失输入时标记 degraded", empty.get("degraded") is True)
    fields = {a["field"] for a in empty.get("assumptions", [])}
    check("三处假设全部登记", fields == {"china_exposure", "gross_margin", "net_margin"},
          str(sorted(fields)))
    check("每条假设都写了理由",
          all(a.get("reason") for a in empty.get("assumptions", [])))
    check("说明文字点出哪些输入是假设值", "假设" in (empty.get("reason") or ""))

    full = sc.analyze_disruption_scenarios(
        company_info={},
        financial_analysis={
            "raw_metrics": {"NET_INCOME": [{"value": 200.0}],
                            "REVENUE": [{"value": 1000.0}]},
            "ratios": {"GROSS_MARGIN": [{"value": 0.45}]},
        },
        geopolitical_analysis={"tariff_sensitivity": {"china_exposure": 0.3}},
        industry_cls=None, sc_cfg={"has_global_supply_chain": True})
    check("输入齐全时不得误标降级",
          not full.get("degraded") and not full.get("assumptions"))
    check("输入齐全时数值按真实输入计算",
          full.get("china_exposure") == 0.3 and full.get("base_gross_margin") == 0.45,
          f"{full.get('china_exposure')} / {full.get('base_gross_margin')}")

    # ── 5. 新闻：只标分析降级，不污染真实计数 ──
    print("\n[5] 新闻：数据可用但分析失败要说清")
    from app.analysis.news import NewsAnalyzer

    news = NewsAnalyzer()
    news._client = _BadClient()
    out = news.analyze("测试公司",
                       articles=[{"title": "公告一", "published": "2026-01-01"}])
    check("真实文章数照常记录", out.get("article_count") == 1)
    check("分析失败被标记", out.get("analysis_degraded") is True)
    check("不误标整段降级（避免污染真实计数）", "degraded" not in out)
    check("写明原因", bool(out.get("reason")), str(out.get("reason")))

    # ── 6. 解析失败可计数 ──
    print("\n[6] 结构化输出失败可计数")
    from app.llm.structured import failure_stats, parse_llm_json, reset_failures

    reset_failures()
    parse_llm_json("不是 JSON", default={}, context="P3验收")
    check("失败被计数", failure_stats().get("P3验收") == 1, str(failure_stats()))
    parse_llm_json("不是 JSON", default={}, context="P3验收")
    check("同一 context 累计", failure_stats().get("P3验收") == 2)
    parse_llm_json('{"ok": 1}', default={}, context="P3验收")
    check("成功不计数", failure_stats().get("P3验收") == 2)
    reset_failures()

    # ── 7. 插补可追溯 ──
    print("\n[7] 插补痕迹进入产物")
    import pandas as pd

    from app.data.cleaning.missing_handler import MissingHandler

    df = pd.DataFrame({"REVENUE": [100.0, None, 300.0]},
                      index=pd.to_datetime(["2023-12-31", "2024-12-31", "2025-12-31"]))
    handler = MissingHandler()
    handler.forward_fill(df, "income")
    entries = handler.record.entries
    check("插补被记录", len(entries) == 1, str(entries))
    check("记录字段齐全",
          set(entries[0]) == {"statement", "period", "metric", "method", "value"},
          str(entries[0]))

    from app.analysis.pipeline import AnalysisOrchestrator

    orch = AnalysisOrchestrator.__new__(AnalysisOrchestrator)  # 不初始化数据源
    orch.cleaning_pipeline = type("P", (), {})()
    orch.cleaning_pipeline.missing_handler = handler
    rec = orch._cleaning_record()
    check("产物记录含插补条数", rec.get("imputed_count") == 1, str(rec.get("imputed_count")))
    check("明细逐条带 imputed 标记",
          rec["imputed_metrics"][0].get("imputed") is True)
    check("明细带插补方法说明",
          "forward_fill" in rec["imputed_metrics"][0].get("imputed_reason", ""))
    # pandas Timestamp 会把期间写成「2024-12-31 00:00:00」，台账 as_of 要保持纯日期
    check("期间只保留日期（不带时间戳）",
          rec["imputed_metrics"][0].get("period") == "2024-12-31",
          str(rec["imputed_metrics"][0].get("period")))

    # ── 8. 台账：标记传播与不传染 ──
    print("\n[8] 台账传播边界")
    degraded_res = {
        "swot_analysis": out if False else {
            "degraded": True, "reason": "LLM 未返回有效 SWOT 结构",
            "rating": None, "swot": {"strengths": [], "weaknesses": []}},
        "news_analysis": out,
    }
    led = build_ledger(degraded_res)
    article = led.find("news_analysis.article_count")
    check("新闻真实计数保持 computed（不被污染）",
          article is not None and article.provenance == "computed",
          str(article.provenance) if article else "缺失")

    led_imp = build_ledger({"financial_analysis": {"cleaning": {
        "imputed_count": 1,
        "imputed_metrics": [{"statement": "income", "period": "2024",
                             "metric": "REVENUE", "method": "forward_fill",
                             "value": 100.0, "imputed": True,
                             "imputed_reason": "forward_fill 插补"}]}}})
    val = led_imp.find("financial_analysis.cleaning.imputed_metrics[0].value")
    check("插补单元格在台账里标为 imputed",
          val is not None and val.provenance == IMPUTED,
          str(val.provenance) if val else "缺失")
    check("插补出处写明方法",
          val is not None and "forward_fill" in (val.evidence_ref or ""),
          str(val.evidence_ref) if val else "缺失")
    check("插补台账仍无未归类路径", not led_imp.unknown(),
          str([f.path for f in led_imp.unknown()]))

    # ── 9. 报告渲染 ──
    print("\n[9] 报告如实写明「未生成」")
    from app.report.markdown import _section_summary

    md = "\n".join(_section_summary(
        {"swot_analysis": {"degraded": True, "rating": None,
                           "reason": "LLM 未返回有效 SWOT 结构"}},
        {}, {},
        {"degraded": True, "rating": None,
         "reason": "LLM 未返回有效 SWOT 结构"}))
    check("渲染出「未生成」而非省略", "未生成" in md, md[:80])
    check("渲染里带上真实原因", "LLM 未返回有效 SWOT 结构" in md)

    # ── 汇总 ──
    print("\n" + "=" * 62)
    if _failed:
        print(f"结果: {_passed} 通过, {len(_failed)} 失败")
        for f in _failed:
            print(f"  ✗ {f}")
        sys.exit(1)
    print(f"结果: {_passed}/{_passed} 全部通过")
    print("=" * 62)


if __name__ == "__main__":
    main()
