"""Phase 8 端到端集成测试：完整系统全链路（真实运行的后端 HTTP 服务）。

覆盖：
  1. 健康检查（后端在线 + LLM 就绪）
  2. 行情：搜索→报价→历史K线→财务→公司信息
  3. 专家评分：Violetz + 四大师
  4. 深度研究：后台管线 run→状态流转→结果→报告(md/html/pdf+图表)
  5. AI 对话：SSE 逐 token 追问（真实数据）
  6. 基金：搜索→实时净值
  7. 自选股：股票+基金 增/查/实时数据/删
  8. 报告列表 / 下载

前置：后端已启动  D:\\python\\python.exe api\\main.py
运行：PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\test_e2e.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# 强制 UTF-8，避免检查名中的 ①-⑳ 在 GBK 控制台下打印崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui import api_client as api

_results: list[dict] = []


def record(name: str, ok: bool, detail: str = ""):
    _results.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


# ── 1. 健康检查 ──────────────────────────────────────────

def check_health():
    print("\n=== 1. 健康检查 ===")
    h = api.health()
    record("后端在线 + LLM 就绪", h and h.get("status") == "ok" and h.get("llm_ready"),
           f"version={h and h.get('version')} model={h and h.get('model')}")


# ── 2. 行情 ──────────────────────────────────────────────

def check_quotes():
    print("\n=== 2. 行情 ===")
    r = api.search("贵州茅台")
    ticker = next((x["ticker"] for x in r if "600519" in x["ticker"]), None)
    record("① 搜索 贵州茅台→600519.SS", ticker is not None, f"results={r[:2]}")

    q = api.quote(ticker)
    record("② 实时报价", q.get("current_price") is not None,
           f"{q.get('company_name')} {q.get('currency')} {q.get('current_price')} "
           f"({q.get('change_percent')}%)")

    hist = api.history(ticker, "1mo")
    record("③ 历史K线", hist.get("count", 0) > 10, f"{hist.get('count')} 根")

    fin = api.financials(ticker)
    record("④ 财务概览", fin.get("pe_ratio") is not None or fin.get("return_on_equity") is not None,
           f"PE={fin.get('pe_ratio')} ROE={fin.get('return_on_equity')}")

    info = api.info(ticker)
    record("⑤ 公司信息", bool(info), f"name={info.get('long_name')}")


# ── 3. 专家评分 ──────────────────────────────────────────

def check_score(ticker: str):
    print("\n=== 3. 专家评分 ===")
    t0 = time.time()
    sc = api.score(ticker)
    vz = sc.get("violetz_score") or {}
    exp = sc.get("expert_score") or {}
    record("⑥ Violetz 推荐评分", vz.get("total_score") is not None,
           f"VZ={vz.get('total_score')} {vz.get('rating')} "
           f"指标={vz.get('available_metrics')}/{vz.get('total_metrics')} "
           f"({time.time()-t0:.0f}s)")
    n_exp = len(exp.get("experts") or [])
    record("⑦ 四大师专家评分", n_exp >= 4, f"{n_exp} 位, 行业={exp.get('industry_name')}")


# ── 4. 深度研究 ──────────────────────────────────────────

def check_analysis(ticker: str):
    print("\n=== 4. 深度研究（后台管线） ===")
    record("⑧ 启动分析", True, "开始后台管线（约 1-3 分钟）…")
    r0 = api.analysis_run(ticker)
    statuses = [r0.get("status")]
    deadline = time.time() + 320
    while time.time() < deadline:
        st = api.analysis_status(ticker).get("status")
        statuses.append(st)
        if st in ("completed", "failed"):
            break
        time.sleep(10)
    ok = "pending" in statuses and "running" in statuses and "completed" in statuses
    record("⑨ 状态流转 pending→running→completed", ok, f"观测到: {statuses}")

    result = api.analysis_result(ticker)
    fin = result.get("financial_analysis") or {}
    val = result.get("valuation_analysis") or {}
    dcf = val.get("dcf") or {}
    record("⑩ 分析结果（财务+估值）", bool(fin.get("ratios")),
           f"ratios={len(fin.get('ratios', {}))} 项, DCF={dcf.get('fair_value')}, "
           f"耗时={result.get('pipeline_time'):.1f}s")

    rep = api.analysis_report(ticker)
    record("⑪ 报告生成 md/html/pdf", rep.get("charts_count", 0) >= 5 and bool(rep.get("report")),
           f"{rep.get('charts_count')} 图, 格式={list(rep.get('report', {}).keys())}")


# ── 5. AI 对话（SSE）─────────────────────────────────────

def check_chat(ticker: str):
    print("\n=== 5. AI 对话（SSE 逐 token） ===")
    evts = []
    tokens = 0
    with api.chat_stream(f"{ticker} 今天股价怎么样") as resp:
        resp.encoding = "utf-8"
        evt = None
        for raw in resp.iter_lines(decode_unicode=True):
            line = (raw or "").strip()
            if line.startswith("event:"):
                evt = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line.split(":", 1)[1].strip())
                except Exception:
                    continue
                if evt == "token":
                    tokens += len(data.get("text", ""))
                evts.append(evt)
    names = list(dict.fromkeys(evts))
    record("⑫ 对话追问 SSE", "token" in names and "done" in names,
           f"事件={names}, token字符={tokens}")


# ── 6. 基金 ──────────────────────────────────────────────

def check_fund():
    print("\n=== 6. 基金 ===")
    sug = api.fund_suggest("005827")
    rec = next((x for x in sug if "005827" in str(x.get("基金代码"))), None)
    record("⑬ 基金搜索 005827", rec is not None, f"{rec}")
    nav = api.fund_quote("005827")
    record("⑭ 基金实时净值", nav.get("单位净值") is not None,
           f"{nav.get('基金简称')} 净值={nav.get('单位净值')} {nav.get('净值日期')}")


# ── 7. 自选股 ────────────────────────────────────────────

def check_watchlist():
    print("\n=== 7. 自选股（股票+基金） ===")
    # 清理可能的残留
    api.watchlist_remove("AAPL")
    api.fund_watchlist_remove("005827")

    ok_add = api.watchlist_add("AAPL")
    record("⑮ 自选加 AAPL", ok_add, "")
    items = api.watchlist_data()
    aapl = next((i for i in items if i["ticker"] == "AAPL"), None)
    record("⑯ 自选实时数据", aapl is not None and aapl.get("price") is not None,
           f"AAPL price={aapl and aapl.get('price')}")

    ok_fadd = api.fund_watchlist_add("005827", "易方达蓝筹精选")
    fdata = api.fund_watchlist_data()
    f005 = next((i for i in fdata if i["fund_code"] == "005827"), None)
    record("⑰ 基金自选加 + 净值", ok_fadd and f005 is not None and f005.get("unit_nav") is not None,
           f"005827 nav={f005 and f005.get('unit_nav')}")

    # 清理
    api.watchlist_remove("AAPL")
    api.fund_watchlist_remove("005827")
    record("⑱ 自选删除", "AAPL" not in api.watchlist(), "")


# ── 8. 报告列表 / 下载 ───────────────────────────────────

def check_reports():
    print("\n=== 8. 报告列表 / 下载 ===")
    pdfs = api.reports_list("pdf")
    record("⑲ 报告列表(PDF)", any("600519" in p["name"] or "AAPL" in p["name"] for p in pdfs),
           f"{len(pdfs)} 个 PDF")
    if pdfs:
        target = next((p for p in pdfs if "600519" in p["name"] or "AAPL" in p["name"]), pdfs[0])
        data = api.reports_download(target["name"])
        record("⑳ 报告下载", data[:5] == b"%PDF-", f"{target['name']} {len(data)} bytes")


if __name__ == "__main__":
    t0 = time.time()
    check_health()
    check_quotes()
    check_score("600519.SS")
    check_analysis("600519.SS")
    check_chat("600519.SS")
    check_fund()
    check_watchlist()
    check_reports()

    passed = sum(1 for r in _results if r["ok"])
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"端到端集成测试: {passed}/{total} 通过  ({time.time()-t0:.0f}s)")
    for r in _results:
        if not r["ok"]:
            print(f"  FAIL: {r['name']} {r['detail']}")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)
