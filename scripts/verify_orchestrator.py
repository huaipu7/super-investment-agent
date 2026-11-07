"""B1 验证：统一编排入口（档位推断 / 路由决定 / 分派透传 / 逐字不变）。

Part A —— Depth 与 resolve_depth：显式 > 意图 > 默认，非法值不静默吞。
Part B —— plan_route：三档到三条通路的映射，形状不匹配时显式降级。
Part C —— dispatch：只调应调的那条，参数原样透传。
Part D —— 逐字不变：引用的是**同一批函数对象**，调用顺序与路由代码一致。
Part E —— 懒加载：只走 quick 时不把 analysis / investigation 拖进进程。
Part F —— 活体（可选，SIA_LIVE=1 才跑，真实取数）。

运行: D:\\python\\python.exe scripts/verify_orchestrator.py
（A–E 全离线：不联网、不调 LLM、不写 data/）
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name: str, ok, detail: str = ""):
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def main():
    from app.agent import orchestrator as orch
    from app.agent import planner, runner

    # ── Part A：档位 ──────────────────────────────────────
    print("\n=== Part A: Depth / resolve_depth ===")
    check("coerce 三档", [orch.Depth.coerce(s).value
                          for s in ("quick", "STANDARD", " Deep ")]
          == ["quick", "standard", "deep"])
    check("coerce 认枚举实例", orch.Depth.coerce(orch.Depth.DEEP) is orch.Depth.DEEP,
          "str(Depth.DEEP) 在 3.11+ 会是 'Depth.DEEP'，必须先判 isinstance")
    check("coerce 非法值落 standard", orch.Depth.coerce("quikc") is orch.Depth.STANDARD)
    check("coerce(None) 落 standard", orch.Depth.coerce(None) is orch.Depth.STANDARD)

    d, warn = orch.coerce_depth("quikc")
    check("非法档位不静默：给警告", d is orch.Depth.STANDARD and "quikc" in (warn or ""), str(warn))
    check("合法档位无警告", orch.coerce_depth("deep") == (orch.Depth.DEEP, None))

    check("显式 depth 优先于意图",
          orch.resolve_depth({"intent": "analysis"}, depth="quick")[:2] == (orch.Depth.QUICK, "explicit"))
    check("无显式时按意图", orch.resolve_depth({"intent": "analysis"})[:2]
          == (orch.Depth.STANDARD, "intent:analysis"))
    check("未知意图落默认", orch.resolve_depth({"intent": "闲聊"})[:2]
          == (orch.DEFAULT_DEPTH, "default"))
    check("空意图落默认", orch.resolve_depth(None)[:2] == (orch.DEFAULT_DEPTH, "default"))

    expect_intents = {"query_price": "quick", "query_history": "quick",
                      "financials": "quick", "compare": "quick",
                      "analysis": "standard", "deep_compare": "deep"}
    bad = {k: orch.resolve_depth({"intent": k})[0].value
           for k, v in expect_intents.items()
           if orch.resolve_depth({"intent": k})[0].value != v}
    check("六个意图的默认档位", not bad, str(bad))

    # ── Part B：路由决定 ─────────────────────────────────
    print("\n=== Part B: plan_route ===")

    def route(intent, **kw):
        return orch.plan_route(intent, **kw)

    r = route({"intent": "query_price", "tickers": ["AAPL"]})
    check("查价 → 单遍管线", (r.pipeline, r.depth.value) == ("chat", "quick"), str(r.as_dict()))
    check("查价不降级", not r.downgraded)

    r = route({"intent": "analysis", "tickers": ["AAPL"]})
    check("分析 → 固定分析管线", (r.pipeline, r.depth.value, r.ticker) == ("analysis", "standard", "AAPL"))

    r = route({"intent": "deep_compare", "tickers": ["AAPL"]})
    check("多跳单标的 → 调查循环", (r.pipeline, r.depth.value) == ("investigation", "deep"))

    r = route({"intent": "deep_compare", "tickers": ["AAPL", "MSFT"]})
    check("多跳多标的 → 降级为单遍", (r.pipeline, r.depth.value) == ("chat", "quick"), str(r.as_dict()))
    check("降级可见：downgraded + 原因写清标的数",
          r.downgraded and r.requested.value == "deep" and "2 个标的" in r.reason, r.reason)

    r = route({"intent": "analysis", "tickers": []})
    check("分析但没标的 → 降级为单遍并说明",
          r.pipeline == "chat" and r.downgraded and "没有标的" in r.reason, r.reason)

    r = route({"intent": "analysis", "tickers": ["AAPL"]}, depth="quick")
    check("显式 quick 压过多标的意图", (r.pipeline, r.depth.value) == ("chat", "quick"))

    r = route({"intent": "query_price", "tickers": ["AAPL"]}, depth="deep")
    check("显式 deep 抬高档位", (r.pipeline, r.depth.value) == ("investigation", "deep"))

    r = route({"intent": "analysis", "tickers": ["AAPL"]}, depth="turbo")
    check("非法 depth 落 standard 且原因里点名",
          r.depth.value == "standard" and "turbo" in r.reason, r.reason)

    d = r.as_dict()
    check("route 可序列化且字段齐全",
          set(d) == {"pipeline", "depth", "requested", "source", "reason", "ticker",
                     "downgraded"}, str(sorted(d)))

    # ── Part C：分派与透传 ───────────────────────────────
    print("\n=== Part C: dispatch 透传 ===")
    calls: list[tuple] = []

    def fake_chat(text, intent, history):
        calls.append(("chat", text, intent, history))
        return {"intent": intent, "plan": {"steps": []}, "execution": {}, "report": "R"}

    def fake_analysis(ticker, *, period, retriever, progress_cb):
        calls.append(("analysis", ticker, period, retriever, progress_cb))
        return {"ticker": ticker}

    def fake_inv(ticker, *, period, progress_cb):
        calls.append(("investigation", ticker, period, progress_cb))
        return {"ticker": ticker, "verdict": "ok"}

    def fake_redteam(result, *, llm):
        calls.append(("redteam", result.get("ticker"), llm))
        return {"status": "llm_unavailable", "counter_arguments": [], "dropped": [],
                "fragile_assumption": None, "open_questions": [], "note": "stub"}

    runners = dict(chat_runner=fake_chat, analysis_runner=fake_analysis,
                   investigation_runner=fake_inv, redteam_runner=fake_redteam)

    out = orch.dispatch("x", intent={"intent": "query_price", "tickers": ["AAPL"]}, **runners)
    check("quick 只调 chat", [c[0] for c in calls] == ["chat"], str([c[0] for c in calls]))
    check("quick 返回 report", out["report"] == "R" and out["route"]["pipeline"] == "chat")

    calls.clear()
    out = orch.dispatch("x", intent={"intent": "analysis", "tickers": ["AAPL"]},
                        period="quarterly", retriever="RET", progress_cb="CB", **runners)
    check("standard 只调 analysis", [c[0] for c in calls] == ["analysis"])
    check("analysis 参数原样透传",
          calls[0][1:] == ("AAPL", "quarterly", "RET", "CB"), str(calls[0]))
    check("analysis 结果挂在 artifact 下", out["artifact"] == {"ticker": "AAPL"})

    calls.clear()
    out = orch.dispatch("x", intent={"intent": "deep_compare", "tickers": ["AAPL"]},
                        period="annual", progress_cb="CB", **runners)
    check("deep 调 investigation 后再调红队", [c[0] for c in calls] == ["investigation",
                                                                     "redteam"],
          str([c[0] for c in calls]))
    check("investigation 参数原样透传", calls[0][1:] == ("AAPL", "annual", "CB"), str(calls[0]))
    check("investigation 结果挂在 investigation 下", out["investigation"]["verdict"] == "ok")
    check("红队结果写进产物 redteam 字段",
          out["investigation"]["redteam"]["note"] == "stub",
          str(out["investigation"].get("redteam")))

    calls.clear()
    out = orch.dispatch("x", intent={"intent": "deep_compare", "tickers": ["AAPL", "MSFT"]},
                        **runners)
    check("降级的 deep 走 chat 而不是 investigation", [c[0] for c in calls] == ["chat"])
    check("降级后不跑红队", "redteam" not in (out.get("route") or {}))

    calls.clear()
    orch.dispatch("x", intent={"intent": "analysis", "tickers": ["AAPL"]}, **runners)
    check("standard 档不跑红队", [c[0] for c in calls] == ["analysis"])

    # 直接给 ticker：不解析意图、不花 token
    calls.clear()
    parsed: list = []
    real_parse = orch.parse_intent
    orch.parse_intent = lambda *a, **k: parsed.append(1) or {
        "intent": "analysis", "tickers": ["SHOULD_NOT_HAPPEN"]}
    try:
        orch.dispatch("看看茅台", ticker="600519.SS", **runners)
    finally:
        orch.parse_intent = real_parse
    check("给 ticker 时不调 intent.parse",
          not parsed and [c[0] for c in calls] == ["analysis"])
    check("给 ticker 时标的直达分析管线", calls[0][1] == "600519.SS", str(calls[0][1]))

    calls.clear()
    orch.dispatch("看看茅台", ticker="600519.SS", depth="quick", **runners)
    sint = calls[0][2]
    check("合成的 intent 标正确且标注来源",
          sint["tickers"] == ["600519.SS"] and sint.get("tickers_source") == "caller",
          str(sint))

    # ── Part D：逐字不变 ─────────────────────────────────
    print("\n=== Part D: 与既有路由逐字一致 ===")
    from api import routes_chat

    check("chat 通路的四个函数与路由是同一对象",
          orch.parse_intent is routes_chat.parse_intent
          and orch.build_plan is routes_chat.build_plan
          and orch.execute_plan is routes_chat.execute_plan
          and orch.generate_report is routes_chat.generate_report,
          "任何一处不是 is 就说明复制了一份逻辑")

    # 用同一批桩分别跑「编排」与「路由里的那串调用」，比对结果与调用日志
    log_a: list[tuple] = []
    log_b: list[tuple] = []

    def mk(tag, log):
        def _build(intent):
            log.append((tag, "build_plan", intent["intent"]))
            return {"steps": [{"action": "get_quote", "params": {"ticker": "AAPL"}}]}

        def _exec(plan):
            log.append((tag, "execute_plan", len(plan["steps"])))
            return {"results": [], "summary": {"total": 1, "success": 1, "failed": 0}}

        def _report(execution, intent, history=None):
            log.append((tag, "generate_report", history))
            return f"报告::{intent['intent']}::{execution['summary']['success']}"

        return _build, _exec, _report

    stub_intent = {"intent": "query_price", "tickers": ["AAPL"], "timeframe": None,
                   "analysis_type": None, "raw_input": "x"}

    for mod, log, tag in ((orch, log_a, "orch"), (routes_chat, log_b, "route")):
        b, e, g = mk(tag, log)
        saved = mod.build_plan, mod.execute_plan, mod.generate_report
        mod.build_plan, mod.execute_plan, mod.generate_report = b, e, g
        try:
            if tag == "orch":
                res_a = orch.dispatch("x", intent=stub_intent,
                                      history=["h"], chat_runner=None)
            else:
                plan = mod.build_plan(stub_intent)
                execution = mod.execute_plan(plan)
                res_b = mod.generate_report(execution, stub_intent, history=["h"])
        finally:
            mod.build_plan, mod.execute_plan, mod.generate_report = saved

    # 归一化后比对：去掉 tag 只比顺序与参数
    check("调用顺序一致",
          [c[1:] for c in log_a] == [c[1:] for c in log_b], f"{log_a} vs {log_b}")
    check("报告逐字相同", res_a["report"] == res_b, f"{res_a['report']!r} vs {res_b!r}")
    check("quick 通路只碰这三个函数（不额外跑执行器）",
          len(log_a) == 3, str(log_a))

    check("planner/runner 未被本模块改写",
          planner.build_plan.__module__ == "app.agent.planner"
          and runner.execute_plan.__module__ == "app.agent.runner")

    # ── Part E：懒加载 ───────────────────────────────────
    print("\n=== Part E: 只走 quick 时不拖重依赖 ===")
    code = (
        "import sys;"
        "import app.agent.orchestrator;"
        "print('analysis_dragged=' + str('app.analysis.pipeline' in sys.modules));"
        "print('investigation_dragged=' + str('app.investigation.loop' in sys.modules));"
    )
    p = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                       capture_output=True, timeout=180)
    out_s = (p.stdout or b"").decode("utf-8", "replace")
    check("import orchestrator 拉不起 app.analysis.pipeline",
          "analysis_dragged=False" in out_s, out_s.strip() or (p.stderr or b"").decode()[:200])
    check("import orchestrator 拉不起 app.investigation.loop",
          "investigation_dragged=False" in out_s, out_s.strip())

    # ── Part F：活体（可选）──────────────────────────────
    print("\n=== Part F: 活体（SIA_LIVE=1 才跑）===")
    if os.environ.get("SIA_LIVE") != "1":
        print("[SKIP] 未设置 SIA_LIVE=1，跳过真实取数")
    else:
        out = orch.dispatch("AAPL 现在多少钱", depth="quick")
        check("活体 quick 出报告", isinstance(out.get("report"), str) and len(out["report"]) > 50,
              str(out.get("report"))[:80])
        check("活体 route 是 chat/quick",
              (out["route"]["pipeline"], out["route"]["depth"]) == ("chat", "quick"))
        check("活体报告不是错误页", "数据获取失败" not in out["report"])

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
