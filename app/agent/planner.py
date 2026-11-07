"""策划车间 - 任务分解与路径规划（逐字迁移自 workshops/planner.py）

模板覆盖不到的请求（没识别出标的、或意图来自规则兜底）走一次 LLM 排步骤；
LLM 排出的步骤仍必须落在 `runner.ACTION_MAP` 白名单内，否则整条丢弃。
同一 (意图, 原文 hash) 只问一次模型——重复提问不重复烧 token。
"""
import hashlib
import json
import re

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

    模板优先，但以下两种情况下先问一次 LLM 排步骤（确定性分支原样保留，
    LLM 不可用或排不出合法步骤就退回模板，行为与改动前一致）：

      * 没识别出标的 —— 模板只会返回「未能识别股票代码」的死胡同；
      * 意图来自规则兜底（`confidence == "low"`）—— 靠关键词猜的，不可信。

    Args:
        intent_result: intent.parse() 的输出

    Returns:
        dict: {
            "steps": [{"action": str, "params": dict, "description": str}],
            "parallel_groups": [[int, ...], ...],  # 可并行执行的步骤索引组
            "meta": {"intent": str, "tickers": list}
        }
    """
    if _needs_llm_plan(intent_result):
        llm_plan = _try_llm_plan(intent_result)
        if llm_plan:
            return llm_plan
    return _build_deterministic(intent_result)


def _build_deterministic(intent_result: dict) -> dict:
    """模板分支。改动前后逐字一致——B2 只在它前面加了一层兜底。"""
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


# ── 模板外兜底：LLM 排步骤（B2）────────────────────────────────
#
# 三条纪律：
#   1. 只在模板确实无解时触发（没标的 / 规则兜底的意图），不覆盖已验证的确定性分支；
#   2. 排出来的每一步都必须能落到 runner.ACTION_MAP，标的不合法的步骤整条丢弃；
#   3. 一条都留不下就当没发生过，调用方拿到的还是原来那份模板计划。

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,11}$")

# (意图, 原文 hash) → 计划。进程内缓存：同一句话问第二次不再花 token。
_PLAN_CACHE: dict[tuple[str, str], dict] = {}

PLAN_REQUEST_TOOL = {
    "type": "function",
    "function": {
        "name": "build_execution_plan",
        "description": ("为用户的投资查询排出可执行步骤。每个步骤只能调用给定的数据工具，"
                        "且必须指定标的。排不出可执行步骤就不要调用本工具。"),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": sorted(_KNOWN_ACTIONS)},
                            "ticker": {"type": "string",
                                       "description": "如 AAPL / 0700.HK / 600519.SS"},
                            "period": {"type": "string",
                                       "description": "仅 get_history 需要，如 1mo / 1y / 5y"},
                            "description": {"type": "string"},
                        },
                        "required": ["action", "ticker"],
                    },
                }
            },
            "required": ["steps"],
        },
    },
}


def _needs_llm_plan(intent_result: dict) -> bool:
    """模板无解的两个真实信号：没有标的，或意图是规则兜底猜出来的。"""
    if not (intent_result.get("tickers") or []):
        return True
    return str(intent_result.get("confidence") or "").strip().lower() == "low"


def _plan_key(intent_result: dict) -> tuple[str, str]:
    text = str(intent_result.get("raw_input") or "")
    return (str(intent_result.get("intent") or ""),
            hashlib.sha1(text.encode("utf-8")).hexdigest()[:16])


def clear_plan_cache() -> None:
    _PLAN_CACHE.clear()


def plan_cache_stats() -> dict:
    return {"size": len(_PLAN_CACHE), "keys": sorted(k[0] for k in _PLAN_CACHE)}


def _try_llm_plan(intent_result: dict) -> dict | None:
    """让模型排一次步骤。不可用 / 排不合法 / 全被丢弃 → None（走模板）。"""
    key = _plan_key(intent_result)
    cached = _PLAN_CACHE.get(key)
    if cached is not None:
        return {**cached, "meta": {**cached["meta"], "cache": "hit"}}

    text = str(intent_result.get("raw_input") or "").strip()
    if not text:
        return None

    try:
        from app.agent.llm import chat_tool_calls

        calls = chat_tool_calls(
            messages=[
                {"role": "system", "content":
                    "你是投资任务规划器。只返回可执行的数据获取步骤，不要复述用户问题，"
                    "不要编造标的。"},
                {"role": "user", "content":
                    f"用户请求：{text}\n"
                    f"当前粗解析：意图={intent_result.get('intent')}，"
                    f"标的={intent_result.get('tickers') or []}。"},
            ],
            tools=[PLAN_REQUEST_TOOL],
            task="plan",
        )
    except Exception:  # noqa: BLE001 LLM 不可用 → 维持模板行为
        return None
    if not calls:
        return None

    for tc in calls:
        if tc.get("name") != "build_execution_plan":
            continue
        try:
            raw_steps = json.loads(tc.get("arguments") or "{}").get("steps") or []
        except (json.JSONDecodeError, AttributeError):
            continue
        steps = _sanitize_steps(raw_steps)
        if not steps:
            continue
        plan = {
            "steps": steps,
            "parallel_groups": [list(range(len(steps)))],
            "meta": {"intent": intent_result.get("intent"), "tickers":
                     sorted({s["params"]["ticker"] for s in steps}),
                     "planner": "llm-fallback", "cache": "miss"},
        }
        _PLAN_CACHE[key] = plan
        return plan
    return None


def _sanitize_steps(raw_steps) -> list[dict]:
    """白名单过滤：动作不在 ACTION_MAP、标的格式不对的步骤整条丢掉。"""
    from app.agent.runner import ACTION_MAP

    steps: list[dict] = []
    for s in raw_steps:
        if not isinstance(s, dict):
            continue
        action = str(s.get("action") or "").strip()
        ticker = str(s.get("ticker") or "").strip().upper()
        if action not in ACTION_MAP or not _TICKER_RE.match(ticker):
            continue
        params: dict = {"ticker": ticker}
        period = str(s.get("period") or "").strip()
        if action == "get_history" and period:
            params["period"] = period
        desc = str(s.get("description") or "").strip() or f"{action}({ticker})"
        steps.append(_step(action, params, desc))
    return steps
