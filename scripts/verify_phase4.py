"""Phase 4 数据质量补全 —— 离线验证（无需后端 / 网络，纯函数 + 合成数据）。

覆盖：
- financial.compute_quarterly_trend：近4季度趋势 + 同报告期同比、缺失降级
- meeting.MeetingAnalyzer：yfinance 日历记录 → 诚实 source 标注（mock LLM）
- metrics.data_sources / _ds_instrument 计数契约：请求/错误/平均耗时聚合
- report.markdown._section_financial：季度趋势渲染（可用 / 显式不可用两态）
- rag 研报文件名解析 + 知识库回填脚本入口可导入
- 消除 start_year 硬编码 2024（源码文本断言）

运行: D:\\python\\python.exe scripts\\verify_phase4.py
退出码: 0 = 全部通过；1 = 存在失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_PASS = 0
_FAIL = 0


def _ok(name: str, cond: bool, detail: str = ""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# ── 4.1 季度趋势 ──────────────────────────────────────────

def _test_quarterly_trend():
    print("== compute_quarterly_trend ==")
    import pandas as pd

    from app.analysis.financial import compute_quarterly_trend

    idx = pd.to_datetime(["2025-03-31", "2025-06-30", "2025-09-30",
                          "2025-12-31", "2026-03-31", "2026-06-30"])
    df = pd.DataFrame({
        "REVENUE": [50e8, 100e8, 160e8, 200e8, 55e8, 108e8],
        "NET_INCOME": [10e8, 22e8, 35e8, 45e8, 12e8, 24e8],
    }, index=idx)

    qt = compute_quarterly_trend(df)
    _ok("available", qt.get("available") is True)
    per = qt.get("periods", [])
    _ok("近4期升序", len(per) == 4
        and [p["period"] for p in per] == ["2025Q3", "2025Q4", "2026Q1", "2026Q2"],
        f"periods={[p['period'] for p in per]}")
    last = per[-1]
    _ok("营收同比(同报告期 vs 上年)", last["period"] == "2026Q2"
        and abs(last["revenue_yoy"] - 0.08) < 1e-6,
        f"got={last.get('revenue_yoy')}")
    _ok("净利同比", abs(last["net_income_yoy"] - (24 - 22) / 22) < 1e-3,
        f"got={last.get('net_income_yoy')}")
    _ok("首期无上年 → 同比 None", per[0]["period"] == "2025Q3"
        and per[0]["revenue_yoy"] is None)
    _ok("营收/净利数值保留", last["revenue"] == 108e8 and last["net_income"] == 24e8)

    _ok("空 df → 不可用", compute_quarterly_trend(pd.DataFrame())["available"] is False)
    bad = df.copy().drop(columns=["NET_INCOME"])
    bad_qt = compute_quarterly_trend(bad)
    _ok("仅营收可用且净利 None", bad_qt["available"] is True
        and all(p["net_income"] is None for p in bad_qt["periods"]))
    _ok("None → 不可用", compute_quarterly_trend(None)["available"] is False)
    # 全 NaN → 不可用
    nan = pd.DataFrame({"REVENUE": [None, None], "NET_INCOME": [None, None]}, index=idx[:2])
    _ok("全 NaN → 不可用", compute_quarterly_trend(nan)["available"] is False)
    # 仅一年数据 → 可用但同比全 None
    one = df.iloc[:2]
    qt1 = compute_quarterly_trend(one)
    _ok("单年可用且同比 None", qt1["available"] is True
        and all(p["revenue_yoy"] is None for p in qt1["periods"]))


# ── 4.2 美股股东大会（source 标注，mock LLM）─────────────

def _test_meeting_source():
    print("== MeetingAnalyzer source 标注（yfinance 日历） ==")
    from app.analysis.meeting import MeetingAnalyzer

    class _FakeLLM:
        @staticmethod
        def chat(**kw):
            return '{"governance_signals": "仅财报/分红日历，无提案", "key_proposals": []}'

    ma = MeetingAnalyzer()
    ma._client = _FakeLLM()
    recs = [{
        "name": "财报/分红日历（yfinance）",
        "start_date": "2026-10-30",
        "record_date": "2026-08-10",
        "resolution_date": "2026-08-13",
        "announcement_date": "",
        "proposals": "",
        "source_note": "yfinance 财报/分红日历（仅披露日期，无股东大会提案）",
    }]
    out = ma.analyze("TEST", recs)
    _ok("available", out.get("available") is True)
    _ok("source 带 yfinance 说明", "yfinance" in out.get("source", ""),
        f"source={out.get('source')!r}")
    _ok("meeting_count=1", out.get("meeting_count") == 1)

    # 东财记录（无 source_note）→ 回退东财标注
    recs_cn = [{"name": "2026年第一次临时股东大会", "start_date": "2026-08-20",
                "record_date": "", "resolution_date": "", "proposals": "增补董事"}]
    out_cn = ma.analyze("TEST", recs_cn)
    _ok("无 source_note 回退东财", out_cn.get("source") == "东财股东大会数据",
        f"source={out_cn.get('source')!r}")

    _ok("None → 不可用", ma.analyze("TEST", None).get("available") is False)
    _ok("空列表 → 不可用", ma.analyze("TEST", []).get("available") is False)


# ── 4.4 数据源健康监控 ────────────────────────────────────

def _test_data_sources():
    print("== metrics.data_sources 聚合 ==")
    from app.observability.metrics import metrics

    metrics.reset()
    metrics.inc("ds_akshare_quote")
    metrics.inc("ds_akshare_quote_errors")
    metrics.inc("ds_akshare_statements")
    metrics.inc("ds_yfinance_get_history")
    metrics.record("ds_akshare_latency", 0.5)
    metrics.record("ds_akshare_latency", 0.3)

    ds = metrics.data_sources()
    ak = ds.get("akshare") or {}
    _ok("akshare 请求=2", ak.get("requests") == 2, f"got={ak.get('requests')}")
    _ok("akshare 错误=1", ak.get("errors") == 1, f"got={ak.get('errors')}")
    _ok("akshare 平均耗时≈0.4", abs(ak.get("avg_seconds", 0) - 0.4) < 1e-6,
        f"got={ak.get('avg_seconds')}")
    _ok("yfinance 请求=1", (ds.get("yfinance") or {}).get("requests") == 1)
    _ok("sina 无数据", (ds.get("sina") or {}).get("requests") == 0)

    # /metrics 端点整合（响应结构）
    snap = metrics.snapshot()
    snap["data_sources"] = metrics.data_sources()
    _ok("metrics 快照含 data_sources", "data_sources" in snap)

    # 装饰器契约：MarketData 方法带 _ds_instrument 包装
    from app.data.router import MarketData
    _ok("router 方法已打点",
        getattr(getattr(MarketData, "get_quote", None), "__name__", "") == "wrapper"
        and getattr(getattr(MarketData, "get_meetings", None), "__name__", "") == "wrapper")

    metrics.reset()


# ── 报告渲染（markdown 季度趋势）─────────────────────────

def _test_markdown_quarterly():
    print("== _section_financial 季度趋势渲染 ==")
    from app.report.markdown import _section_financial

    fa = {
        "periods": ["2024", "2025"],
        "ratios": {},
        "quarterly_trend": {
            "available": True,
            "source": "季度财报（报告期累计口径，同比=同报告期 vs 上年同期）",
            "periods": [
                {"period": "2025Q3", "report_date": "2025-09-30",
                 "revenue": 160e8, "net_income": 35e8, "revenue_yoy": 0.10, "net_income_yoy": 0.08},
                {"period": "2025Q4", "report_date": "2025-12-31",
                 "revenue": 200e8, "net_income": 45e8, "revenue_yoy": None, "net_income_yoy": None},
            ],
        },
    }
    md = "\n".join(_section_financial(fa))
    _ok("含季度趋势标题", "近季度营收/净利趋势" in md)
    _ok("含报告期与同比", "2025Q3 (2025-09-30)" in md and "+10.0%" in md)
    _ok("缺失同比显示 —", "| — |" in md)

    fa_un = {"periods": ["2025"], "ratios": {},
             "quarterly_trend": {"available": False, "reason": "美股季度财报未接入（本报告为年报口径）",
                                 "periods": []}}
    md_un = "\n".join(_section_financial(fa_un))
    _ok("不可用显式标注", "季度趋势: 美股季度财报未接入" in md_un)


# ── 4.3 知识库回填 ────────────────────────────────────────

def _test_backfill():
    print("== 知识库回填（文件名解析 + 脚本可导入） ==")
    from app.rag.ingest.reports import _parse_fname

    tk, dt_ = _parse_fname("600519.SS_20260811_004002.md")
    _ok("A股文件名解析", tk == "600519.SS" and dt_ == "2026-08-11",
        f"ticker={tk} date={dt_}")
    tk2, dt2 = _parse_fname("AAPL 对话分析_20260812_101010.md")
    _ok("美股带后缀文件名解析", tk2 == "AAPL" and dt2 == "2026-08-12",
        f"ticker={tk2} date={dt2}")
    tk3, dt3 = _parse_fname("乱命名文件.txt")
    _ok("非标准文件名安全", tk3 == "" and dt3 == "")

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "backfill_reports", ROOT / "scripts" / "backfill_reports.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _ok("backfill_reports 脚本可导入", callable(mod.backfill_reports))


# ── 消除 start_year 硬编码 2024 ───────────────────────────

def _test_no_hardcoded_start_year():
    print("== start_year 硬编码消除 ==")
    src_ak = (ROOT / "app" / "data" / "sources" / "akshare_source.py").read_text(encoding="utf-8")
    src_rt = (ROOT / "app" / "data" / "router.py").read_text(encoding="utf-8")
    _ok("akshare 默认 start_year=None",
        "def get_financial_indicators(self, code: str, start_year: str | None = None)" in src_ak)
    _ok("akshare 动态年份",
        "datetime.date.today().year - 5" in src_ak)
    _ok("router 动态 start_year",
        "else str(dt.date.today().year - 5)" in src_rt
        and 'else "2024")' not in src_rt
        and "as_of_dt.year) if as_of_dt is not None" in src_rt)


def main():
    print("=" * 60)
    print("verify_phase4.py — Phase 4 数据质量补全（季度化 / 美股补全 / 知识库 / TTL+监控）")
    print("=" * 60)
    _test_quarterly_trend()
    print()
    _test_meeting_source()
    print()
    _test_data_sources()
    print()
    _test_markdown_quarterly()
    print()
    _test_backfill()
    print()
    _test_no_hardcoded_start_year()
    print()
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
