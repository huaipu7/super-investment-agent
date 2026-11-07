# -*- coding: utf-8 -*-
"""固收智能投研 Agent（Phase 4，JD 职责③）。

自然语言问句 → LLM Function Calling 路由到固收工具（①信用 / ②利率情景 / ③RAG）→
LLM 依据**工具返回的真实数值**综合成投研意见 + 风险信号。

诚实边界（约束#7）：
  - 数值一律来自 app/credit 与 app/rates 引擎的真实计算（或 RAG 真实检索）；
    LLM 只做叙述与结构化，不凭空捏造数字。system 已强制要求引用工具返回数值。
  - 无商业评级 feed：信用输出为结构模型代理分（Altman Z'' / Ohlson O / Merton，
    Merton 市场数据缺失时如实标注不可用）。
"""
from __future__ import annotations

import json
import sys

import pandas as pd

from app.llm.client import get_llm_client, to_anthropic_tool

# 工具以 OpenAI 格式定义，经 to_anthropic_tool 转 Anthropic 后传入 chat_with_tools
FI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "credit_issuer",
            "description": "评估一家海外发债主体的结构信用风险：Altman Z'' / Ohlson O、关键会计比率、Merton 违约距离（若市场数据可用）。返回结构风险分与风险/安全区代理标签，非评级机构评级。",
            "parameters": {"type": "object",
                           "properties": {"ticker": {"type": "string", "description": "美股 ticker，如 AAPL、F"}},
                           "required": ["ticker"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rate_curve",
            "description": "当前美债收益率曲线的 NSS 因子读数：水平/斜率/曲率、拟合 R²，及关键期限（2Y/10Y）即期收益率。",
            "parameters": {"type": "object",
                           "properties": {"months": {"type": "integer", "description": "回看月数（默认 13）"}},
                           "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rate_forecast",
            "description": "基于 NSS 水平/斜率/曲率因子的 VAR 多步预测：返回水平因子未来 N 步路径（利率中枢外推）与 naive 基准对照。",
            "parameters": {"type": "object",
                           "properties": {"horizon": {"type": "integer", "description": "预测步数（默认 5）"}},
                           "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rate_scenario",
            "description": "利率情景库：基准/乐观(下行)/悲观(上行)/极端激进/极端避险的期限曲线路径 + 当前因子读数。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "policy_signal",
            "description": "最新 FOMC 政策声明的鸽鹰分：立场（偏鸽/中性/偏鹰）+ 连续分（-1~1）+ 关键词命中明细。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stress_replay",
            "description": "历史压力重演：2020 疫情冲击与 2022 激进加息窗口的真实利率水平/斜率位移（bp）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_retrieve",
            "description": "从本地知识库检索与某主题相关的文本语料（宏观/研报/新闻）。无索引语料时返回空。",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string", "description": "检索关键词"}},
                           "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "action_plan",
            "description": "把信用/利率/政策引擎输出综合成结构化行动方案：信用观点 + 久期/曲线倾向 + 风险信号 + 合规复核队列。确定性规则、无 LLM，动作以久期/曲线层面表达（无价格级止损止盈）。",
            "parameters": {"type": "object",
                           "properties": {"ticker": {"type": "string", "description": "美股债券主体 ticker（可选，默认 F）"}},
                           "required": []},
        },
    },
]


# ── 工具实现（数值全部来自真实引擎）────────────────────────

def _credit_issuer(args: dict) -> dict:
    from app.credit import structural as S
    from app.credit import features as F
    from app.data import fixed_income
    fi = fixed_income.get_fixed_income_data()
    ticker = (args.get("ticker") or "").strip().upper()
    if not ticker:
        return {"ok": False, "reason": "缺少 ticker"}
    try:
        panel = fi.get_credit_panel(ticker)
    except Exception as e:
        return {"ok": False, "reason": f"EDGAR 不可用：{e}"}
    if panel.empty:
        return {"ok": False, "reason": "EDGAR 财务为空"}
    ff = F.build_feature_frame(panel)
    if ff.empty or ff["altman_z"].dropna().empty:
        return {"ok": False, "reason": "结构分不可算（缺 Altman Z'' 输入项）"}
    last = ff.iloc[-1]

    def _f(v, nd=2):
        return None if (v is None or pd.isna(v)) else round(float(v), nd)

    out = {
        "ok": True, "ticker": ticker,
        "as_of_end": str(panel.index[-1].date()),
        "filed": str(last["filed"])[:10] if pd.notna(last["filed"]) else None,
        "altman_z": _f(last["altman_z"]),
        "ohlson_o": _f(last["ohlson_o"]),
        "label_risk_zone": _f(last["label"]),
        "ratios": {k: _f(last.get(k)) for k in
                   ["debt_ratio", "debt_to_equity", "current_ratio", "net_margin",
                    "roa", "roe", "ebit_margin", "wcta", "reta", "ebita", "eq_lia"]
                   if k in last},
    }
    out["label_zone_note"] = ("Altman Z''<2.60=灰/风险区（代理标签，非评级机构）"
                              if out["label_risk_zone"] == 1 else "结构分安全区")

    # Merton：市场数据可用才给，缺失诚实标注
    try:
        from app.data.sources.yfinance_source import YFinanceSource
        yf = YFinanceSource()
        info = yf.fetch_company_info(ticker)
        cap = info.get("market_cap")
        h = yf.fetch_price_history(ticker, start="2024-01-01", end="2026-08-31")
        vol = S.equity_vol_from_history(h["Close"].to_numpy()) if not h.empty else None
        # 负债面值取自科目面板（feature frame 不携带 liabilities，取到恒为 None → Merton 恒不可用）。
        p_last = panel.iloc[-1]
        debt = p_last["liabilities"] if pd.notna(p_last.get("liabilities")) else None
        m = S.merton_from_market(cap, debt, vol)
        out["merton"] = ({"dd": _f(m["dd"]), "pd": _f(m["pd"], 4),
                          "sigma_V": _f(m["sigma_V"], 4)}
                         if m.get("available") else {"available": False,
                                                     "reason": m.get("reason")})
    except Exception as e:
        out["merton"] = {"available": False, "reason": f"市场数据不可用（{type(e).__name__}）"}
    return out


def _rate_curve(args: dict) -> dict:
    from app.rates import curve as C
    from app.data import fixed_income
    fi = fixed_income.get_fixed_income_data()
    months = int(args.get("months") or 13)
    curve = fi.get_yield_curve(months=months)
    if curve is None or curve.empty:
        return {"ok": False, "reason": "Treasury 曲线为空"}
    df = C.fit_curve_path(curve)
    if df.empty:
        return {"ok": False, "reason": "NSS 拟合失败"}
    last = df.iloc[-1]
    # 关键期限即期收益：用 day_fit 重建
    day = C.day_fit(curve)
    tenor = {}
    for t in ("2 Yr", "10 Yr", "30 Yr"):
        if t in curve.columns:
            v = curve[t].iloc[-1]
            tenor[t] = None if pd.isna(v) else round(float(v), 3)
    return {
        "ok": True, "as_of": str(curve.index.max().date()),
        "level": None if pd.isna(last["level"]) else round(float(last["level"]), 3),
        "slope": None if pd.isna(last["slope"]) else round(float(last["slope"]), 3),
        "curvature": None if pd.isna(last["curvature"]) else round(float(last["curvature"]), 3),
        "r2": None if pd.isna(last["r2"]) else round(float(last["r2"]), 4),
        "key_tenors": tenor,
        "fit": day.get("ok"),
    }


def _rate_forecast(args: dict) -> dict:
    from app.rates import curve as C
    from app.rates import models as M
    from app.data import fixed_income
    fi = fixed_income.get_fixed_income_data()
    horizon = int(args.get("horizon") or 5)
    curve = fi.get_yield_curve(months=13)
    if curve is None or curve.empty:
        return {"ok": False, "reason": "Treasury 曲线为空"}
    factors = C.fit_curve_path(curve)
    if factors.empty:
        return {"ok": False, "reason": "NSS 拟合失败"}
    cols = ["level", "slope", "curvature"]
    cols = [c for c in cols if c in factors.columns]
    # 确定性 VAR 外推（level 因子路径）
    try:
        fc = M.fit_forecast_var(factors, cols, horizon, factors.index.max())
        level_path = [None if pd.isna(v) else round(float(v), 3) for v in fc["level"]]
    except Exception as e:
        level_path = None
    # 诚实对照：同 n 的 walk-forward RMSE（var vs naive）；step 大些只留几折，保持响应
    try:
        rv = M.walk_forward(factors, cols, horizon=horizon, min_train=60, step=30, use="var")
        rn = M.walk_forward(factors, cols, horizon=horizon, min_train=60, step=30, use="naive")
        mv, mn = M.backtest_metrics(rv, col="level"), M.backtest_metrics(rn, col="level")
        rmse = {"var": mv.get("rmse"), "naive": mn.get("rmse")}
    except Exception:
        rmse = None
    return {"ok": True, "horizon": horizon, "level_path_forecast": level_path,
            "rmse_honest": rmse,
            "note": "level 因子≈随机游走，短窗 naive 往往不劣于 VAR；RMSE 诚实并列。"}


def _rate_scenario(args: dict) -> dict:
    from app.rates import curve as C
    from app.rates import scenario as SC
    from app.data import fixed_income
    fi = fixed_income.get_fixed_income_data()
    curve = fi.get_yield_curve(months=13)
    if curve is None or curve.empty:
        return {"ok": False, "reason": "Treasury 曲线为空"}
    factors = C.fit_curve_path(curve)
    eng = SC.ScenarioEngine(factors)
    scen = eng.scenarios(forecast_dates=[str(factors.index[-1].date())])
    summ = eng.summary()
    if not scen.get("ok"):
        return {"ok": False, "reason": "情景库不可用"}
    # 只回传每个情景的 10Y 点位（grid index of 10）
    grid = scen.get("maturities", [])
    ten = grid.index(10.0) if 10.0 in grid else None
    out = {"ok": True, "as_of": summ.get("as_of"),
           "level": summ.get("level"), "slope": summ.get("slope"),
           "scenario_10y": {}}
    for name, d in scen.get("scenarios", {}).items():
        path = d.get("path", [[]])
        y10 = path[0][ten] if ten is not None and path and len(path[0]) > ten else None
        out["scenario_10y"][name] = {
            "10y": None if y10 is None else round(float(y10), 3),
            "description": d.get("description"),
        }
    out["note"] = "情景冲击量为分析师假设，非预测。"
    return out


def _policy_signal(args: dict) -> dict:
    from app.rates import policy as P
    from app.data import fixed_income
    fi = fixed_income.get_fixed_income_data()
    pol = P.PolicySignal().score_statement(fi.get_policy_signal())
    return {"ok": bool(pol.get("ok")), "stance": pol.get("stance"),
            "score": pol.get("score"), "meeting_date": pol.get("meeting_date"),
            "num_hawk_tokens": pol.get("num_hawk_tokens"),
            "num_dove_tokens": pol.get("num_dove_tokens")}


def _stress_replay(args: dict) -> dict:
    from app.rates import curve as C
    from app.rates import scenario as SC
    from app.data import fixed_income
    fi = fixed_income.get_fixed_income_data()
    curve = fi.get_yield_curve(months=84)
    if curve is None or curve.empty:
        return {"ok": False, "reason": "长历史 Treasury 为空"}
    factors = C.fit_curve_path(curve)
    ev = SC.ScenarioEngine(factors).stress_replay(curve).get("events", {})
    out = {"ok": True, "events": {}}
    for name, d in ev.items():
        if d.get("ok"):
            out["events"][name] = {"level_delta_bp": d["level_delta_bp"],
                                   "slope_delta_bp": d["slope_delta_bp"],
                                   "window": d["window"]}
        else:
            out["events"][name] = {"available": False, "reason": d.get("reason")}
    return out


def _rag_retrieve(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "reason": "缺少 query"}
    try:
        from app.rag.retriever import get_knowledge_retriever
        r = get_knowledge_retriever()
        if not r.available:
            return {"ok": False, "reason": "RAG 嵌入/向量库不可用（已静默降级）"}
        docs = r.invoke(query, top_k=3)
    except Exception as e:
        return {"ok": False, "reason": f"RAG 不可用：{type(e).__name__}: {e}"}
    if not docs:
        return {"ok": True, "found": 0, "docs": [],
                "note": "相关语料未索引，无检索结果。"}
    return {
        "ok": True, "found": len(docs),
        "docs": [{"content": d.page_content[:400], "source": d.metadata.get("source"),
                  "score": d.metadata.get("score")} for d in docs],
    }


def _action_plan(args: dict) -> dict:
    """综合行动方案：复用信用/利率/政策工具的真实输出 → 确定性动作引擎。"""
    from app.action.fixed_income import synthesize_action
    ticker = (args.get("ticker") or "F").strip().upper() or "F"
    credit = _credit_issuer({"ticker": ticker})
    curve = _rate_curve({})
    policy = _policy_signal({})
    return synthesize_action(issuer=ticker, credit=credit, policy=policy, curve=curve)


_DISPATCH = {
    "credit_issuer": _credit_issuer,
    "rate_curve": _rate_curve,
    "rate_forecast": _rate_forecast,
    "rate_scenario": _rate_scenario,
    "policy_signal": _policy_signal,
    "stress_replay": _stress_replay,
    "rag_retrieve": _rag_retrieve,
    "action_plan": _action_plan,
}


def tool_executor(name: str, args: dict) -> str:
    """按名字派发工具并返回 JSON 字符串（供 LLM 读取真实数值）。"""
    fn = _DISPATCH.get(name)
    if fn is None:
        return json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False)
    try:
        result = fn(args or {})
    except Exception as e:
        result = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    return json.dumps(result, ensure_ascii=False, default=str)


# ── Agent 主逻辑 ───────────────────────────────────────────

_SYSTEM = """你是固收投研 Agent，服务海外债与利率研究。你只能引用工具返回的**真实数值**，
绝不能编造数字。工具涵盖：①信用(credit_issuer) ②利率与情景(rate_curve / rate_forecast /
rate_scenario / policy_signal / stress_replay) ③文本知识(rag_retrieve)。

请结合问句选择合适工具（可多步/组合），并输出：
1) 一段投研判断（引用工具数值，说明数据来源与日期）
2) 「投资建议」：可执行/可观察，区分主/次
3) 「风险信号」：标出风险点与需人工复核项（如结构代理标签、无商业评级、市场数据缺失）

若某项数据缺失（如 Merton 市场数据不可用、RAG 无语料），如实说明，不要猜测补齐。
"""


def run(user_query: str, max_turns: int = 6, task: str = "research") -> dict:
    """NL 问句 → 工具路由 → LLM 综合，返回 {text, tool_calls, messages}。

    task 走模型网关（MODEL_TIERS 可配 research 任务）。
    """
    client = get_llm_client()
    tools_anthro = [to_anthropic_tool(t) for t in FI_TOOLS]
    result = client.chat_with_tools(
        messages=[{"role": "user", "content": user_query}],
        system=_SYSTEM,
        tools=tools_anthro,
        tool_executor=tool_executor,
        max_turns=max_turns,
        task=task,
    )
    return {
        "text": result.text,
        "tool_calls": result.tool_calls,
        "messages": result.messages,
    }
