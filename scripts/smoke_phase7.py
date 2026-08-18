"""Phase 7 冒烟测试：Streamlit 看板 + 完整用户流程。

Part A —— AppTest 无头运行看板，验证各模块渲染无异常（默认页 + 切到自选股页）。
Part B —— 通过看板同一套 API 客户端走通完整用户流程：
   查贵州茅台 → 评分（Violetz + 四大师）→ 深度研究（后台管线）→ 对话追问（SSE）→ 基金净值

依赖后端已启动: D:\\python\\python.exe api/main.py
运行: PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\smoke_phase7.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui import api_client as api

_results: list[dict] = []


def record(name: str, ok: bool, detail: str = ""):
    _results.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


# ── Part A：看板无头渲染 ───────────────────────────────

def check_dashboard():
    print("\n=== Part A: 看板 AppTest 渲染 ===")
    from streamlit.testing.v1 import AppTest

    app_path = str(Path(__file__).resolve().parent.parent / "ui" / "dashboard.py")
    mods = ["📊 行情与评分", "📈 深度研究", "💬 AI 对话", "📦 基金分析", "⭐ 自选股"]

    at = AppTest.from_file(app_path, default_timeout=45)
    try:
        at.run(timeout=60)
    except Exception as e:
        record("看板默认页渲染", False, str(e))
        return

    ok_all = True
    for mod in mods:
        try:
            at.radio[0].set_value(mod)
            at.run(timeout=90)
            if at.exception:
                ok_all = False
            record(f"看板渲染: {mod}", not at.exception,
                   f"exception={at.exception}" if at.exception else "无异常")
        except Exception as e:
            ok_all = False
            record(f"看板渲染: {mod}", False, str(e))
    return ok_all


# ── Part B：完整用户流程 ───────────────────────────────

def check_flow():
    print("\n=== Part B: 完整用户流程 ===")

    # 1. 查贵州茅台
    t0 = time.time()
    r = api.search("贵州茅台")
    ticker = next((x["ticker"] for x in r if "600519" in x["ticker"]), None)
    record("① 搜索贵州茅台→600519.SS", ticker is not None, f"results={r[:3]}")

    q = api.quote(ticker)
    record("② 实时报价", q.get("current_price") is not None,
           f"{q.get('company_name')} {q.get('currency')} {q.get('current_price')} "
           f"{q.get('change_percent')}%")

    hist = api.history(ticker, "1mo")
    record("③ 历史K线", hist.get("count", 0) > 10, f"{hist.get('count')} 根")

    # 2. 评分（Violetz + 四大师）
    sc = api.score(ticker)
    vz = sc.get("violetz_score") or {}
    expert = sc.get("expert_score") or {}
    record("④ 评分 Violetz", vz.get("total_score") is not None,
           f"VZ={vz.get('total_score')} {vz.get('rating')}")
    n_exp = len(expert.get("experts") or [])
    record("⑤ 四大师专家评分", n_exp >= 4,
           f"{n_exp} 位专家, industry={expert.get('industry_name')}")

    # 3. 深度研究（后台管线）
    record("⑥ 深度研究启动", True, "开始后台管线（约1-2分钟）…")
    r0 = api.analysis_run(ticker)
    statuses = [r0.get("status")]  # POST 返回 pending
    deadline = time.time() + 300
    while time.time() < deadline:
        st = api.analysis_status(ticker).get("status")
        statuses.append(st)
        if st in ("completed", "failed"):
            break
        time.sleep(10)
    ok = "pending" in statuses and "running" in statuses and "completed" in statuses
    record("⑦ 状态流转 pending→running→completed", ok, f"观测到: {statuses}")

    result = api.analysis_result(ticker)
    fin = result.get("financial_analysis") or {}
    val = result.get("valuation_analysis") or {}
    dcf = val.get("dcf") or {}
    record("⑧ 分析结果", bool(fin.get("ratios")),
           f"ratios={len(fin.get('ratios', {}))} 项, DCF={dcf.get('fair_value')}, "
           f"pipeline_time=" + (f"{result.get('pipeline_time'):.1f}s"
                                if result.get("pipeline_time") is not None else "—"))

    rep = api.analysis_report(ticker)
    record("⑨ 报告生成", rep.get("charts_count", 0) >= 5 and bool(rep.get("report")),
           f"{rep.get('charts_count')} 图, 格式={list(rep.get('report', {}).keys())}")

    # 4. 对话追问（SSE）
    import json
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
    record("⑩ 对话追问 SSE", "token" in names and "done" in names,
           f"事件={names}, token字符={tokens}")

    # 5. 基金净值（新端点）
    nav = api.fund_quote("005827")
    record("⑪ 基金净值", nav.get("单位净值") is not None,
           f"005827 {nav.get('基金简称')} 净值={nav.get('单位净值')} "
           f"{nav.get('净值日期')}")

    print(f"    Part B 总耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    check_dashboard()
    check_flow()

    passed = sum(1 for r in _results if r["ok"])
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"Phase 7 结果: {passed}/{total} 通过")
    for r in _results:
        if not r["ok"]:
            print(f"  FAIL: {r['name']} {r['detail']}")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)
