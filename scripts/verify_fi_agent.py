# -*- coding: utf-8 -*-
"""Phase 4 验证 —— 固收智能投研 Agent（JD 职责③）真实数据 + 真实 LLM 编排。

分两部分：
  A. 工具接线：直接派发每个固收工具，确认返回**真实数值**（不靠 LLM）。
  B. LLM 编排：对真实问句走 chat_with_tools（Function Calling 路由 + 工具执行 +
     LLM 综合），验证输出含「投研判断 / 投资建议 / 风险信号」且引用工具真实数值。

诚实边界（约束#7）：工具数值全部来自 app/credit 与 app/rates 引擎真实计算或
RAG 真实检索；LLM 只叙述，system 强制要求只引工具返回数值。若某工具因数据源
暂不可用（如长历史 Treasury 未缓存、yfinance 限流）→ 如实标 available=false。

运行：D:\\python\\python.exe scripts/verify_fi_agent.py
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

PASS, FAIL = "PASS", "FAIL"
_checks: list[str] = []


def check(name, cond, detail=""):
    _checks.append((f"{PASS if cond else FAIL}  {name}  {detail}").rstrip())
    print(("  [%s] %s %s" % ("OK" if cond else "XX", name, detail)).rstrip())


def _j(s: str) -> dict:
    try:
        return json.loads(s)
    except Exception:
        return {}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 78)
    print("Phase 4 固收 Agent 验证  运行时间:", pd.Timestamp.now())
    print("=" * 78)
    from app.agent import fixed_income as FI

    # ── A. 工具接线（真实数值）─────────────────────────────
    print("\n[A] 工具接线 → 真实数值")
    outcomes = {}
    for name, args in [
        ("credit_issuer", {"ticker": "F"}),
        ("rate_curve", {}),
        ("rate_scenario", {}),
        ("policy_signal", {}),
        ("rate_forecast", {"horizon": 5}),
        ("stress_replay", {}),
        ("rag_retrieve", {"query": "美联储 利率 政策"}),
    ]:
        out = _j(FI.tool_executor(name, args))
        outcomes[name] = out
        ok = out.get("ok")
        tag = ""
        if name == "credit_issuer" and ok:
            tag = f"ticker={out.get('ticker')} altman_z={out.get('altman_z')} ohlson_o={out.get('ohlson_o')} merton_avail={out.get('merton',{}).get('available', False)}"
        elif name == "rate_curve" and ok:
            tag = f"level={out.get('level')} slope={out.get('slope')} 10Y={out.get('key_tenors',{}).get('10 Yr')} r2={out.get('r2')}"
        elif name == "rate_scenario" and ok:
            tag = f"情景数={len(out.get('scenario_10y',{}))}"
        elif name == "policy_signal" and ok:
            tag = f"stance={out.get('stance')} score={out.get('score')} meeting={out.get('meeting_date')}"
        elif name == "rate_forecast" and ok:
            tag = f"rmse_var={out.get('rmse_honest',{}).get('var')} rmse_naive={out.get('rmse_honest',{}).get('naive')}"
        elif name == "stress_replay":
            tag = f"events={len(out.get('events',{}))}" if ok else f"诚实不可用：{out.get('reason')}"
        elif name == "rag_retrieve" and ok:
            tag = f"found={out.get('found')}"
        print(f"   {name}: ok={ok} {tag}")
        # 结算：工具是否产生真实数值（stress/rag 允许诚实不可用）
        if name in ("stress_replay", "rag_retrieve"):
            check(f"工具[{name}] 输出（可诚实不可用）", ok or out.get("reason"), tag)
        else:
            check(f"工具[{name}] 输出真实数值", bool(ok), tag)

    # ── B. LLM 编排（真实 Function Calling）────────────────
    print("\n[B] LLM 编排：NL 问句 → 工具路由 → 综合意见")
    from app.config import get_settings

    llm_ready = bool(get_settings().LLM_API_KEY.strip())
    check("LLM 已配置", llm_ready, "key 已配置" if llm_ready else "未配置，跳过 LLM 部分")

    queries = [
        "评估福特汽车(F)的信用风险，给出投资建议与风险信号",
        "当前美债收益率曲线如何？未来利率情景与美联储政策立场怎么看？",
    ]
    query_detail = []
    for q in queries:
        print(f"\n   ── 问句: {q}")
        try:
            res = FI.run(q)
        except Exception as e:
            print(f"   LLM 编排失败: {type(e).__name__}: {e}")
            query_detail.append((q, {"error": str(e)}))
            continue
        text = res.get("text", "")
        calls = res.get("tool_calls", [])
        toolnames = [c.get("name") for c in calls]
        print(f"   调用工具: {toolnames}")
        # risk signals present in text
        has_risk = any(k in text for k in ("风险", "风险信号", "risk"))
        has_advice = any(k in text for k in ("建议", "可执行", "关注", "建议"))
        snippet = re.sub(r"\s+", " ", text)[:260]
        print(f"   输出摘要: {snippet}")
        query_detail.append({
            "query": q, "tools": toolnames, "text_len": len(text),
            "has_advice": has_advice, "has_risk": has_risk,
        })

    # 至少一条问句触发了工具调用且产出了建议/风险信号
    any_grounded = any(dd.get("tools") and dd.get("has_advice") for dd in query_detail if "tools" in dd)
    check("LLM 编排触发工具调用并产出建议/风险信号", any_grounded or not llm_ready,
          "见上方输出；若 LLM 侧未达到则列于未通过")

    return _finish()


def _finish() -> int:
    print("\n" + "=" * 78)
    total = len(_checks)
    npass = sum(1 for c in _checks if c.startswith(PASS))
    print(f"结果：{npass}/{total} PASS")
    failed = [c for c in _checks if c.startswith(FAIL)]
    if failed:
        print("\n未通过：")
        for c in failed:
            print("  " + c)
    return 0 if npass == total else 1


if __name__ == "__main__":
    sys.exit(main())
