"""Phase C4 验证：多跳查询（deep_compare 意图 / LLM 排步骤 / 确定性执行 / 顺序依赖）。

覆盖：
  (a) intent.parse 离线识别 deep_compare + 双标的（茅台/五粮液）
  (b) build_plan(deep_compare) 步骤 ≥3 且全部可映射 ACTION_MAP、覆盖两标的
  (c) execute_plan 确定性执行 → 结果含两标的字段、全部成功
  (d) runner 简单顺序依赖：{"$ref": "step.<idx>.<field>"} 解析
  (e) LLM 排步骤路径：patch chat_tool_calls → planner=llm，步骤来自 LLM 且合法

运行: D:\\python\\python.exe scripts\\verify_multihop.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def main():
    import app.agent.runner as runner

    # ── mock 数据层（离线）──
    def fake_quote(ticker, **kw):
        return {"ticker": ticker, "current_price": 100.0, "company_name": f"公司{ticker}"}

    def fake_history(ticker, period="1mo", **kw):
        return {"ticker": ticker, "period": period, "bars": 120,
                "end_close": 90.0}

    def fake_financials(ticker, **kw):
        return {"ticker": ticker, "roe": 0.15, "profit_margin": 0.30,
                "revenue_growth": 0.12}

    def fake_info(ticker, **kw):
        return {"ticker": ticker, "sector": "食品饮料", "industry": "白酒"}

    def fake_sentiment(ticker, **kw):
        return {"ticker": ticker, "score": 0.6}

    # ACTION_MAP 在模块加载时捕获了原始函数引用，须直接 patch dict
    # 让 execute_plan 走 mock；同时强制 LLM 规划失败以测确定性兜底。
    with mock.patch.dict(runner.ACTION_MAP, {
        "get_quote": fake_quote,
        "get_history": fake_history,
        "get_financials": fake_financials,
        "get_ticker_info": fake_info,
        "get_news_sentiment": fake_sentiment,
    }), mock.patch("app.agent.llm.chat_tool_calls",
                   side_effect=RuntimeError("LLM 不可用(离线测试)")):
        # ── (a) intent.parse 离线识别 deep_compare ──
        from app.agent.intent import parse

        with mock.patch("app.agent.intent._parse_with_function_calling",
                        return_value=None), \
                mock.patch("app.agent.intent._parse_with_llm", return_value=None):
            intent = parse("对比茅台和五粮液近三年盈利能力，并给出估值")
        check("(a) 识别 deep_compare", intent.get("intent") == "deep_compare",
              intent.get("intent"))
        tickers = intent.get("tickers") or []
        check("(a) 双标的识别",
              "600519.SS" in tickers and "000858.SZ" in tickers, str(tickers))

        # ── (b) build_plan → 步骤可映射且覆盖两标的（确定性兜底）──
        from app.agent.planner import build_plan

        plan = build_plan(intent)
        steps = plan.get("steps", [])
        check("(b) 步骤 ≥3", len(steps) >= 3, f"{len(steps)} 步")
        check("(b) 全部可映射 ACTION_MAP",
              all(s["action"] in runner.ACTION_MAP for s in steps))
        step_tickers = {s["params"].get("ticker") for s in steps}
        check("(b) 覆盖两标的",
              "600519.SS" in step_tickers and "000858.SZ" in step_tickers,
              str(step_tickers))

        # ── (c) 确定性执行 ──
        execution = runner.execute_plan(plan)
        check("(c) 全部成功", execution["summary"]["success"] == len(steps)
              and execution["summary"]["failed"] == 0,
              str(execution["summary"]))
        fin = [r for r in execution["results"]
               if r.get("step", {}).get("action") == "get_financials"]
        check("(c) 结果含两标的财务字段",
              len(fin) == 2 and all(r.get("data", {}).get("roe") == 0.15 for r in fin),
              f"{len(fin)} 条财务")

        # ── (d) 简单顺序依赖（$ref）──
        dep_plan = {"steps": [
            {"action": "get_quote", "params": {"ticker": "AAPL"}, "description": "q"},
            {"action": "get_history",
             "params": {"ticker": {"$ref": "step.0.data.ticker"},
                        "period": "1mo"},
             "description": "h"},
        ]}
        dep_exec = runner.execute_plan(dep_plan)
        check("(d) $ref 解析",
              dep_exec["summary"]["success"] == 2
              and dep_exec["results"][1]["data"]["ticker"] == "AAPL",
              str(dep_exec["results"][1].get("data")))

    # ── (e) LLM 排步骤路径（单独 patch chat_tool_calls 返回合法步骤）──
    from app.agent.planner import _try_llm_steps

    fake_calls = [{
        "name": "build_deep_compare_plan",
        "arguments": json.dumps({"steps": [
            {"action": "get_financials", "ticker": "600519.SS"},
            {"action": "get_quote", "ticker": "000858.SZ"},
            {"action": "get_history", "ticker": "600519.SS", "period": "5y"},
            {"action": "bad_action", "ticker": "X"},
        ]}),
    }]
    with mock.patch("app.agent.llm.chat_tool_calls", return_value=fake_calls):
        llm_plan = _try_llm_steps(["600519.SS", "000858.SZ"], "5y")
    check("(e) LLM 步骤生效", llm_plan is not None
          and llm_plan["meta"].get("planner") == "llm", str(llm_plan))
    check("(e) LLM 步骤过滤非法动作",
          llm_plan is not None
          and all(s["action"] in runner.ACTION_MAP for s in llm_plan["steps"])
          and len(llm_plan["steps"]) == 3, str(llm_plan))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
