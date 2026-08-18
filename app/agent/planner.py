"""策划车间 - 任务分解与路径规划（逐字迁移自 workshops/planner.py）"""

import json

# ===== 各意图对应的默认时间范围 =====
DEFAULT_TIMEFRAME = {
    "query_price": None,
    "query_history": "1mo",
    "analysis": "6mo",
    "compare": "6mo",
    "financials": "1y",
    "deep_compare": "5y",
}

# 多跳规划可用的动作（对齐 runner.ACTION_MAP）
_KNOWN_ACTIONS = {
    "get_quote", "get_history", "get_financials",
    "get_ticker_info", "get_news_sentiment",
}


def build_plan(intent_result: dict) -> dict:
    """根据意图解析结果生成可执行计划

    Args:
        intent_result: intent.parse() 的输出

    Returns:
        dict: {
            "steps": [{"action": str, "params": dict, "description": str}],
            "parallel_groups": [[int, ...], ...],  # 可并行执行的步骤索引组
            "meta": {"intent": str, "tickers": list}
        }
    """
    intent = intent_result["intent"]
    tickers = intent_result["tickers"]
    timeframe = intent_result.get("timeframe") or DEFAULT_TIMEFRAME.get(intent)
    analysis_type = intent_result.get("analysis_type")

    if not tickers:
        return {
            "steps": [],
            "parallel_groups": [],
            "meta": {"intent": intent, "tickers": [], "error": "未能识别股票代码"},
        }

    if intent == "query_price":
        return _plan_query_price(tickers)

    if intent == "query_history":
        return _plan_query_history(tickers, timeframe)

    if intent == "financials":
        return _plan_financials(tickers)

    if intent == "compare":
        return _plan_compare(tickers, timeframe, analysis_type)

    if intent == "deep_compare":
        return _plan_deep_compare(tickers, timeframe)

    # analysis (默认)
    return _plan_analysis(tickers, timeframe, analysis_type)


def _step(action: str, params: dict, description: str) -> dict:
    return {"action": action, "params": params, "description": description}


def _plan_query_price(tickers: list[str]) -> dict:
    """查价格：每只股票一个报价步骤"""
    steps = [_step("get_quote", {"ticker": t}, f"获取 {t} 实时报价") for t in tickers]
    return {
        "steps": steps,
        "parallel_groups": [list(range(len(steps)))],
        "meta": {"intent": "query_price", "tickers": tickers},
    }


def _plan_query_history(tickers: list[str], timeframe: str) -> dict:
    """查历史：每只股票获取历史行情"""
    steps = [_step("get_history", {"ticker": t, "period": timeframe}, f"获取 {t} 最近 {timeframe} 历史行情") for t in tickers]
    return {
        "steps": steps,
        "parallel_groups": [list(range(len(steps)))],
        "meta": {"intent": "query_history", "tickers": tickers},
    }


def _plan_financials(tickers: list[str]) -> dict:
    """查财报：获取财务数据 + 公司信息"""
    steps = []
    for t in tickers:
        steps.append(_step("get_financials", {"ticker": t}, f"获取 {t} 财务数据"))
        steps.append(_step("get_ticker_info", {"ticker": t}, f"获取 {t} 公司概况"))
    idx = list(range(len(steps)))
    parallel_groups = [idx[i:i+2] for i in range(0, len(idx), 2)]  # 每只股票的组可并行
    return {
        "steps": steps,
        "parallel_groups": parallel_groups,
        "meta": {"intent": "financials", "tickers": tickers},
    }


def _plan_analysis(tickers: list[str], timeframe: str, analysis_type: str | None) -> dict:
    """综合分析：报价 + 历史 + 财务 + 信息 + 情绪"""
    steps = []
    for t in tickers:
        steps.append(_step("get_quote", {"ticker": t}, f"获取 {t} 实时报价"))
        steps.append(_step("get_history", {"ticker": t, "period": timeframe}, f"获取 {t} 最近 {timeframe} 历史行情"))
        steps.append(_step("get_financials", {"ticker": t}, f"获取 {t} 财务指标"))
        steps.append(_step("get_ticker_info", {"ticker": t}, f"获取 {t} 公司概况"))
        steps.append(_step("get_news_sentiment", {"ticker": t}, f"获取 {t} 市场情绪"))
    n = len(tickers)
    parallel_groups = [[i*5 + j for j in range(5)] for i in range(n)]
    return {
        "steps": steps,
        "parallel_groups": parallel_groups,
        "meta": {"intent": "analysis", "tickers": tickers, "analysis_type": analysis_type},
    }


def _plan_compare(tickers: list[str], timeframe: str, analysis_type: str | None) -> dict:
    """对比分析：对每个标的做分析，步骤可并行"""
    return _plan_analysis(tickers, timeframe, analysis_type)


# ── 多跳查询（deep_compare）────────────────────────────────────

PLAN_STEPS_TOOL = {
    "type": "function",
    "function": {
        "name": "build_deep_compare_plan",
        "description": "为跨标的/时序多跳查询生成可执行步骤（每个步骤 action 必须是已知工具）",
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": sorted(_KNOWN_ACTIONS),
                            },
                            "ticker": {"type": "string"},
                            "period": {"type": "string"},
                            "depends_on": {"type": "integer",
                                           "description": "依赖的前序步骤索引（可省）"},
                        },
                        "required": ["action", "ticker"],
                    },
                }
            },
            "required": ["steps"],
        },
    },
}


def _plan_deep_compare(tickers: list[str], timeframe: str) -> dict:
    """复合意图：LLM 生成步骤（chat_tool_calls）优先，失败则确定性兜底。

    步骤仍由 runner.ACTION_MAP 确定性执行；LLM 只负责"排步骤"。
    """
    plan = _try_llm_steps(tickers, timeframe)
    if plan:
        return plan

    steps = []
    for t in tickers:
        steps.append(_step("get_history", {"ticker": t, "period": timeframe},
                           f"获取 {t} 近 {timeframe} 历史（盈利趋势）"))
        steps.append(_step("get_financials", {"ticker": t},
                           f"获取 {t} 财务（盈利能力指标）"))
        steps.append(_step("get_quote", {"ticker": t}, f"获取 {t} 行情（估值基准）"))
        steps.append(_step("get_ticker_info", {"ticker": t}, f"获取 {t} 公司概况"))
    n = len(tickers)
    parallel_groups = [[i * 4 + j for j in range(4)] for i in range(n)]
    return {
        "steps": steps,
        "parallel_groups": parallel_groups,
        "meta": {"intent": "deep_compare", "tickers": tickers,
                 "planner": "deterministic-fallback"},
    }


def _try_llm_steps(tickers: list[str], timeframe: str) -> dict | None:
    """用 chat_tool_calls 让 LLM 排步骤；仅保留已知动作。失败/不可用 → None。"""
    try:
        from app.agent.llm import chat_tool_calls

        prompt = (f"为以下多跳分析生成可执行步骤：标的 {tickers}，"
                  f"时间范围 {timeframe}。目标：对比各标的盈利能力与估值。"
                  f"每个标的需要覆盖历史行情、财务指标与报价。")
        calls = chat_tool_calls(
            messages=[
                {"role": "system", "content": "你是投资任务规划器，只返回可执行步骤。"},
                {"role": "user", "content": prompt},
            ],
            tools=[PLAN_STEPS_TOOL],
            task="plan",
        )
    except Exception:  # noqa: BLE001 LLM 不可用/失败 → 兜底
        return None
    if not calls:
        return None
    for tc in calls:
        if tc.get("name") != "build_deep_compare_plan":
            continue
        try:
            steps_raw = json.loads(tc.get("arguments", "{}")).get("steps") or []
        except json.JSONDecodeError:
            continue
        steps: list[dict] = []
        for s in steps_raw:
            action = s.get("action")
            ticker = s.get("ticker")
            if action not in _KNOWN_ACTIONS or not ticker:
                continue
            params = {"ticker": ticker}
            if s.get("period"):
                params["period"] = s["period"]
            step = _step(action, params, f"{action}({ticker})")
            dep = s.get("depends_on")
            if isinstance(dep, int):
                step["depends_on"] = dep
            steps.append(step)
        if len(steps) >= 2:
            return {
                "steps": steps,
                "parallel_groups": [list(range(len(steps)))],
                "meta": {"intent": "deep_compare", "tickers": tickers,
                         "planner": "llm"},
            }
    return None
