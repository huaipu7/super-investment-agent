"""
Phase 6 冒烟测试：FastAPI 后端全端点验证。

覆盖：
  - health / 行情 / 历史 / 财务 / 搜索 / 评分
  - 对话（非流式）+ SSE 流式（股票、基金）逐 token 输出
  - 后台分析任务状态流转（pending→running→completed）
  - 报告生成 / 列表 / 下载 / 导出
  - 自选股（股票 + 基金）CRUD

运行: PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\smoke_phase6.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from api.main import app

_results: list[dict] = []


def record(name: str, ok: bool, detail: str = ""):
    _results.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def parse_sse(lines) -> list[dict]:
    """解析 SSE 行流为 [{event, data}]（事件间以空行分隔）"""
    blocks = []
    buf = None
    for line in lines:
        line = line.strip()
        if line.startswith("event:"):
            buf = {"event": line.split(":", 1)[1].strip()}
        elif line.startswith("data:") and buf is not None:
            try:
                buf["data"] = json.loads(line.split(":", 1)[1].strip())
            except Exception:
                buf["data"] = line.split(":", 1)[1].strip()
        elif not line and buf is not None:
            blocks.append(buf)
            buf = None
    if buf is not None:
        blocks.append(buf)
    return blocks


client = TestClient(app)


# ── 基础 ─────────────────────────────────────────────────────

def check_health():
    print("\n=== 基础 ===")
    r = client.get("/api/v1/health")
    record("health", r.status_code == 200 and r.json().get("status") == "ok",
           str(r.json()))


# ── 行情 ─────────────────────────────────────────────────────

def check_stock():
    print("\n=== 行情/财务/搜索/评分 ===")
    r = client.get("/api/v1/stock/AAPL")
    ok = r.status_code == 200 and r.json().get("current_price") is not None
    record("报价 AAPL", ok, f"{r.json().get('currency')} {r.json().get('current_price')}")

    r = client.get("/api/v1/stock/AAPL/history", params={"period": "1mo"})
    j = r.json()
    record("历史 AAPL 1mo", r.status_code == 200 and j.get("count", 0) > 10,
           f"{j.get('count')} 条, change {j.get('summary', {}).get('change_pct')}%")

    r = client.get("/api/v1/stock/AAPL/financials")
    j = r.json()
    record("财务 AAPL", r.status_code == 200 and j.get("pe_ratio") is not None,
           f"PE={j.get('pe_ratio')}, ROE={j.get('return_on_equity')}")

    r = client.get("/api/v1/stock/search", params={"q": "苹果"})
    record("搜索 苹果", r.status_code == 200 and len(r.json().get("results", [])) > 0,
           str(r.json().get("results", [])[:2]))

    r = client.get("/api/v1/stock/600519/score")
    j = r.json()
    vz_ok = j.get("violetz_score") is not None and j["violetz_score"].get("total_score") is not None
    expert_ok = j.get("expert_score") is not None
    record("评分 600519", r.status_code == 200 and vz_ok and expert_ok,
           f"VZ={j['violetz_score']['total_score'] if vz_ok else 'NA'}, "
           f"专家={'ok' if expert_ok else 'NA'}")


# ── 对话 ─────────────────────────────────────────────────────

def check_chat():
    print("\n=== 对话（非流式 + SSE） ===")
    r = client.post("/api/v1/chat", data={"user_input": "查询苹果股价"})
    j = r.json()
    reply = j.get("reply", "")
    record("对话 非流式", r.status_code == 200 and "AAPL" in reply and "当前价格" in reply,
           reply[:80].replace("\n", " / "))

    # SSE 股票流式（query_price，无 LLM）
    with client.stream("POST", "/api/v1/chat/stream",
                       data={"user_input": "苹果股价"}) as resp:
        events = parse_sse(resp.iter_lines())
    ev_names = [e["event"] for e in events]
    ok = ("status" in ev_names and "intent" in ev_names and
          "token" in ev_names and "done" in ev_names and events[-1]["event"] == "done")
    tokens = sum(len(e["data"].get("text", "")) for e in events if e["event"] == "token")
    record("SSE 股票流式", ok, f"事件={ev_names}, 累计token字符={tokens}")
    print("   样例 token:", next((e["data"].get("text", "")[:60] for e in events
                                  if e["event"] == "token" and e["data"].get("text")), ""))

    # SSE 基金流式
    with client.stream("POST", "/api/v1/chat/fund/stream",
                       data={"user_input": "查询 005827 净值"}) as resp:
        ev_fund = parse_sse(resp.iter_lines())
    fnames = [e["event"] for e in ev_fund]
    ok = ("status" in fnames and "intent" in fnames and
          "token" in fnames and "done" in fnames)
    record("SSE 基金流式", ok, f"事件={fnames}")
    ftok = next((e["data"].get("text", "")[:60] for e in ev_fund
                 if e["event"] == "token" and e["data"].get("text")), "")
    print("   基金样例 token:", ftok)


# ── 自选股 ───────────────────────────────────────────────────

def check_watchlist():
    print("\n=== 自选股（股票+基金） ===")
    # 幂等：先清掉可能残留的测试条目
    client.request("DELETE", "/api/v1/watchlist/remove", json={"ticker": "AAPL"})
    client.request("DELETE", "/api/v1/watchlist/fund/remove", json={"fund_code": "005827"})

    r = client.post("/api/v1/watchlist/add", json={"ticker": "AAPL"})
    ok = r.status_code == 200
    record("自选加 AAPL", ok, str(r.json()))
    client.post("/api/v1/watchlist/add", json={"ticker": "AAPL"})  # 重复加 → 409

    r = client.get("/api/v1/watchlist")
    ok2 = r.status_code == 200 and "AAPL" in r.json().get("tickers", [])
    record("自选列表", ok2, str(r.json()))

    r = client.get("/api/v1/watchlist/data")
    items = r.json().get("items", [])
    ok3 = r.status_code == 200 and any(i["ticker"] == "AAPL" and i["price"] for i in items)
    record("自选实时数据", ok3, f"{len(items)} 项, AAPL price={next((i['price'] for i in items if i['ticker']=='AAPL'), None)}")

    r = client.post("/api/v1/watchlist/fund/add",
                    json={"fund_code": "005827", "fund_name": "易方达蓝筹"})
    record("基金自选加 005827", r.status_code == 200, str(r.json()))

    r = client.get("/api/v1/watchlist/fund/data")
    fitems = r.json().get("items", [])
    ok4 = any(i["fund_code"] == "005827" and i["unit_nav"] is not None for i in fitems)
    record("基金自选净值", ok4, f"{len(fitems)} 项, 005827 nav={next((i['unit_nav'] for i in fitems if i['fund_code']=='005827'), None)}")

    r = client.request("DELETE", "/api/v1/watchlist/remove", json={"ticker": "AAPL"})
    record("自选删 AAPL", r.status_code == 200, str(r.json()))
    client.request("DELETE", "/api/v1/watchlist/fund/remove", json={"fund_code": "005827"})


# ── 后台分析 + 报告 ──────────────────────────────────────────

def check_analysis():
    print("\n=== 后台分析任务（AAPL，约 1-2 分钟） ===")
    r = client.post("/api/v1/analysis/run/AAPL")
    ok = r.status_code == 200 and r.json().get("status") == "pending"
    record("分析启动 pending", ok, str(r.json()))

    statuses = [r.json().get("status")]
    deadline = time.time() + 240
    while time.time() < deadline:
        r = client.get("/api/v1/analysis/status/AAPL")
        st = r.json().get("status")
        statuses.append(st)
        if st in ("completed", "failed"):
            break
        time.sleep(10)
    ok = "pending" in statuses and "running" in statuses and "completed" in statuses
    record("状态流转 pending→running→completed", ok, f"观测到: {statuses}")

    r = client.get("/api/v1/analysis/result/AAPL")
    j = r.json()
    ok = r.status_code == 200 and j.get("financial_analysis", {}).get("ratios")
    record("分析结果完整", ok,
           f"ratios={len(j.get('financial_analysis', {}).get('ratios', {}))} 项, "
           f"pipeline_time={j.get('pipeline_time')}s")

    r = client.post("/api/v1/analysis/report/AAPL")
    j = r.json()
    ok = r.status_code == 200 and j.get("report", {}).get("pdf") and j.get("charts_count", 0) >= 5
    record("报告生成 (md/html/pdf+图)", ok,
           f"{j.get('charts_count')} 图, {list(j.get('report', {}).keys())}")

    r = client.get("/api/v1/reports/list", params={"kind": "pdf"})
    pdfs = r.json()
    record("报告列表", r.status_code == 200 and any("AAPL" in p["name"] for p in pdfs),
           f"{len(pdfs)} 个 PDF")

    r = client.get("/api/v1/reports/download/" + pdfs[0]["name"]) if pdfs else None
    if r is not None:
        record("报告下载", r.status_code == 200 and r.content[:5] == b"%PDF-",
               f"{r.headers.get('content-type')}, {len(r.content)} bytes")

    # 导出：对话 markdown → PDF
    r = client.post("/api/v1/report/export",
                    json={"text": "## 测试报告\n\n**当前价格**: USD 306.55\n\n| 指标 | 值 |\n| --- | --- |\n| PE | 35.2 |",
                          "title": "测试导出"})
    record("对话报告导出 PDF", r.status_code == 200 and r.content[:5] == b"%PDF-",
           f"{len(r.content)} bytes")


if __name__ == "__main__":
    check_health()
    check_stock()
    check_chat()
    check_watchlist()
    check_analysis()

    passed = sum(1 for r in _results if r["ok"])
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"Phase 6 结果: {passed}/{total} 通过")
    for r in _results:
        if not r["ok"]:
            print(f"  FAIL: {r['name']} {r['detail']}")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)
