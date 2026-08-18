"""基金策划车间 - 基金任务分解与路径规划（迁移自 workshops/fund_planner.py）"""

DEFAULT_TIMEFRAME = {
    "fund_nav": None,
    "fund_history": "1y",
    "fund_analysis": "1y",
    "fund_compare": "1y",
    "fund_holdings": None,
}


def build_fund_plan(intent_result: dict) -> dict:
    """根据基金意图解析结果生成可执行计划

    Args:
        intent_result: fund_intent.parse() 的输出

    Returns:
        dict: {
            "steps": [{"action": str, "params": dict, "description": str}],
            "parallel_groups": [[int, ...], ...],
            "meta": {"intent": str, "fund_codes": list}
        }
    """
    intent = intent_result["intent"]
    fund_codes = intent_result.get("fund_codes") or []
    timeframe = intent_result.get("timeframe") or DEFAULT_TIMEFRAME.get(intent)

    if not fund_codes:
        return {
            "steps": [],
            "parallel_groups": [],
            "meta": {"intent": intent, "fund_codes": [], "error": "未能识别基金代码"},
        }

    if intent == "fund_nav":
        return _plan_fund_nav(fund_codes)
    if intent == "fund_history":
        return _plan_fund_history(fund_codes, timeframe)
    if intent == "fund_holdings":
        return _plan_fund_holdings(fund_codes)
    if intent == "fund_compare":
        return _plan_fund_compare(fund_codes, timeframe)
    # fund_analysis (默认)
    return _plan_fund_analysis(fund_codes, timeframe)


def _step(action: str, params: dict, description: str) -> dict:
    return {"action": action, "params": params, "description": description}


def _plan_fund_nav(fund_codes: list[str]) -> dict:
    """查净值：每只基金一个净值步骤"""
    steps = [_step("get_fund_nav", {"fund_code": c}, f"获取 {c} 实时净值") for c in fund_codes]
    return {
        "steps": steps,
        "parallel_groups": [list(range(len(steps)))],
        "meta": {"intent": "fund_nav", "fund_codes": fund_codes},
    }


def _plan_fund_history(fund_codes: list[str], timeframe: str) -> dict:
    """查历史：每只基金获取净值+历史走势"""
    steps = []
    for c in fund_codes:
        steps.append(_step("get_fund_nav", {"fund_code": c}, f"获取 {c} 实时净值"))
        steps.append(_step("get_fund_history", {"fund_code": c}, f"获取 {c} 历史净值"))
    n = len(fund_codes)
    parallel_groups = [[i*2 + j for j in range(2)] for i in range(n)]
    return {
        "steps": steps,
        "parallel_groups": parallel_groups,
        "meta": {"intent": "fund_history", "fund_codes": fund_codes},
    }


def _plan_fund_holdings(fund_codes: list[str]) -> dict:
    """查持仓：持仓+基本信息"""
    steps = []
    for c in fund_codes:
        steps.append(_step("get_fund_holdings", {"fund_code": c}, f"获取 {c} 持仓明细"))
        steps.append(_step("get_fund_info", {"fund_code": c}, f"获取 {c} 基本信息"))
    n = len(fund_codes)
    parallel_groups = [[i*2 + j for j in range(2)] for i in range(n)]
    return {
        "steps": steps,
        "parallel_groups": parallel_groups,
        "meta": {"intent": "fund_holdings", "fund_codes": fund_codes},
    }


def _plan_fund_analysis(fund_codes: list[str], timeframe: str) -> dict:
    """综合分析：净值+历史+持仓+信息+评级"""
    steps = []
    for c in fund_codes:
        steps.append(_step("get_fund_nav", {"fund_code": c}, f"获取 {c} 实时净值"))
        steps.append(_step("get_fund_history", {"fund_code": c}, f"获取 {c} 历史净值"))
        steps.append(_step("get_fund_holdings", {"fund_code": c}, f"获取 {c} 持仓明细"))
        steps.append(_step("get_fund_info", {"fund_code": c}, f"获取 {c} 基本信息"))
        steps.append(_step("get_fund_ratings", {"fund_code": c}, f"获取 {c} 评级"))
    n = len(fund_codes)
    parallel_groups = [[i*5 + j for j in range(5)] for i in range(n)]
    return {
        "steps": steps,
        "parallel_groups": parallel_groups,
        "meta": {"intent": "fund_analysis", "fund_codes": fund_codes},
    }


def _plan_fund_compare(fund_codes: list[str], timeframe: str) -> dict:
    """对比分析：对每只基金执行完整分析"""
    return _plan_fund_analysis(fund_codes, timeframe)
