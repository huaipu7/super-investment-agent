"""B2 验证：planner 的 LLM 兜底与缓存。

Part A —— 触发条件：只在「没标的」或「规则兜底意图」时问模型，其余走模板。
Part B —— 白名单：动作不在 ACTION_MAP、标的不合法的步骤整条丢弃。
Part C —— 缓存：同一句问第二次不再花 token，且命中可从 meta 看出来。
Part D —— 退化：LLM 不可用 / 排不出合法步骤 → 与改动前的模板输出逐字相同。
Part E —— 一致性：工具 schema 的动作集与 runner.ACTION_MAP 严格对齐。

运行: D:\\python\\python.exe scripts/verify_planner_fallback.py
（全离线：chat_tool_calls 全部用桩替换，不联网、不调 LLM）
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from unittest import mock

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


def _tool_call(steps: list[dict]) -> list[dict]:
    import json

    return [{"name": "build_execution_plan", "arguments": json.dumps({"steps": steps})}]


def main():
    from app.agent import planner
    from app.agent.runner import ACTION_MAP

    planner.clear_plan_cache()

    # ── Part A：触发条件 ─────────────────────────────────
    print("\n=== Part A: 何时才问模型 ===")
    check("没标的 → 需要兜底", planner._needs_llm_plan({"intent": "analysis", "tickers": []}))
    check("规则兜底意图 → 需要兜底",
          planner._needs_llm_plan({"intent": "analysis", "tickers": ["AAPL"],
                                   "confidence": "low"}))
    check("高置信 + 有标的 → 不兜底",
          not planner._needs_llm_plan({"intent": "analysis", "tickers": ["AAPL"],
                                       "confidence": "high"}))
    check("没有 confidence 字段 → 不兜底",
          not planner._needs_llm_plan({"intent": "analysis", "tickers": ["AAPL"]}))

    calls: list = []

    def counting_calls(**kw):
        calls.append(kw)
        return _tool_call([{"action": "get_quote", "ticker": "AAPL"}])

    intent_ok = {"intent": "query_price", "tickers": ["AAPL"], "timeframe": None,
                 "analysis_type": None, "raw_input": "AAPL 多少钱", "confidence": "high"}
    with mock.patch("app.agent.llm.chat_tool_calls", side_effect=counting_calls):
        plan_ok = planner.build_plan(intent_ok)
    check("高置信请求一次都不问模型", calls == [], str(len(calls)))
    check("模板计划原样产出",
          plan_ok["steps"] == [{"action": "get_quote", "params": {"ticker": "AAPL"},
                                "description": "获取 AAPL 实时报价"}], str(plan_ok["steps"]))

    # ── Part B：白名单与合法性 ───────────────────────────
    print("\n=== Part B: 白名单过滤 ===")
    planner.clear_plan_cache()
    intent_none = {"intent": "analysis", "tickers": [], "timeframe": None,
                   "analysis_type": None, "raw_input": "帮我看看苹果最近怎么样",
                   "confidence": "low"}
    good = [
        {"action": "get_quote", "ticker": "aapl", "description": "报价"},
        {"action": "get_history", "ticker": "AAPL", "period": "6mo"},
        {"action": "drop_database", "ticker": "AAPL"},        # 不在白名单
        {"action": "get_quote", "ticker": "苹果 公司"},        # 标的不合法
        {"action": "get_financials"},                         # 缺标的
        "not-a-dict",
    ]
    with mock.patch("app.agent.llm.chat_tool_calls", return_value=_tool_call(good)):
        plan = planner.build_plan(intent_none)
    check("只留下白名单内的两步", len(plan["steps"]) == 2, str(plan["steps"]))
    check("标的统一大写", [s["params"]["ticker"] for s in plan["steps"]] == ["AAPL", "AAPL"])
    check("period 只留给 get_history",
          "period" not in plan["steps"][0]["params"] and plan["steps"][1]["params"]["period"] == "6mo")
    check("meta 标出来自 LLM 兜底", plan["meta"].get("planner") == "llm-fallback",
          str(plan["meta"]))
    check("meta 汇总真实标的", plan["meta"]["tickers"] == ["AAPL"])

    planner.clear_plan_cache()
    all_bad = [{"action": "rm_rf", "ticker": "AAPL"}, {"action": "get_quote", "ticker": ""}]
    with mock.patch("app.agent.llm.chat_tool_calls", return_value=_tool_call(all_bad)):
        plan = planner.build_plan(intent_none)
    check("全被丢弃 → 退回模板（没标的仍是空计划）",
          plan["steps"] == [] and plan["meta"].get("error") == "未能识别股票代码", str(plan["meta"]))

    # ── Part C：缓存 ─────────────────────────────────────
    print("\n=== Part C: 同一句只问一次 ===")
    planner.clear_plan_cache()
    hits: list = []

    def once(**kw):
        hits.append(kw)
        return _tool_call([{"action": "get_quote", "ticker": "AAPL"}])

    with mock.patch("app.agent.llm.chat_tool_calls", side_effect=once):
        first = planner.build_plan(intent_none)
        second = planner.build_plan(copy.deepcopy(intent_none))
    check("两次调用只打一次模型", len(hits) == 1, f"{len(hits)} 次")
    check("首次标 cache=miss", first["meta"].get("cache") == "miss", str(first["meta"]))
    check("第二次标 cache=hit", second["meta"].get("cache") == "hit", str(second["meta"]))
    check("两次步骤一致", first["steps"] == second["steps"])
    check("缓存按 (意图, 原文) 建键",
          planner.plan_cache_stats()["size"] == 1, str(planner.plan_cache_stats()))

    third_intent = {**intent_none, "raw_input": "另一句话"}
    with mock.patch("app.agent.llm.chat_tool_calls", side_effect=once):
        planner.build_plan(third_intent)
    check("原文不同则另算一次", len(hits) == 2, f"{len(hits)} 次")

    # ── Part D：退化 ─────────────────────────────────────
    print("\n=== Part D: LLM 不可用时与改动前一致 ===")
    planner.clear_plan_cache()
    base = {"intent": "query_price", "tickers": ["AAPL"], "timeframe": None,
            "analysis_type": None, "raw_input": "AAPL 多少钱"}
    expected = planner._build_deterministic(base)
    with mock.patch("app.agent.llm.chat_tool_calls", side_effect=RuntimeError("离线")):
        degraded = planner.build_plan({**base, "confidence": "low"})
    check("异常 → 模板输出逐字相同", degraded == expected, str(degraded["steps"][:1]))
    check("失败不进缓存", planner.plan_cache_stats()["size"] == 0,
          str(planner.plan_cache_stats()))

    with mock.patch("app.agent.llm.chat_tool_calls", return_value=[]):
        no_call = planner.build_plan({**base, "confidence": "low"})
    check("模型不返回工具调用 → 模板", no_call == expected)

    empty_text = {**base, "raw_input": "   ", "confidence": "low"}
    with mock.patch("app.agent.llm.chat_tool_calls", side_effect=AssertionError("不该被调用")):
        blank = planner.build_plan(empty_text)
    check("空原文不浪费一次调用", blank == planner._build_deterministic(empty_text))

    # ── Part E：与执行层对齐 ─────────────────────────────
    print("\n=== Part E: 工具 schema 与 ACTION_MAP 对齐 ===")
    schema = planner.PLAN_REQUEST_TOOL["function"]["parameters"]["properties"]["steps"]["items"]
    check("schema 动作枚举 == ACTION_MAP 键",
          set(schema["properties"]["action"]["enum"]) == set(ACTION_MAP),
          str(set(schema["properties"]["action"]["enum"]) ^ set(ACTION_MAP)))
    check("_KNOWN_ACTIONS 与 ACTION_MAP 一致", set(planner._KNOWN_ACTIONS) == set(ACTION_MAP))

    # 模板分支未被兜底改写
    for intent, tickers in (("query_price", ["AAPL"]), ("financials", ["AAPL"]),
                            ("analysis", ["AAPL"]), ("compare", ["AAPL", "MSFT"])):
        stub = {"intent": intent, "tickers": tickers, "timeframe": "6mo",
                "analysis_type": None, "raw_input": f"{intent} 测试", "confidence": "high"}
        got = planner.build_plan(stub)
        want = planner._build_deterministic(stub)
        check(f"模板分支未变：{intent}", got == want)

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
