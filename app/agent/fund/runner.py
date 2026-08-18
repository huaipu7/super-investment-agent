"""基金计划执行器 - 按计划调度执行基金步骤并汇总结果（迁移自 workshops/fund_runner.py）"""

from app.agent.fund.executor import (
    get_fund_nav,
    get_fund_history,
    get_fund_holdings_data,
    get_fund_info,
    get_fund_ratings_data,
    FundExecutorError,
)

FUND_ACTION_MAP = {
    "get_fund_nav": get_fund_nav,
    "get_fund_history": get_fund_history,
    "get_fund_holdings": get_fund_holdings_data,
    "get_fund_info": get_fund_info,
    "get_fund_ratings": get_fund_ratings_data,
}


def execute_fund_plan(plan: dict) -> dict:
    """执行基金计划并汇总结果

    Args:
        plan: fund_planner.build_fund_plan() 的输出

    Returns:
        dict: {
            "results": [{"step": dict, "data": any, "error": str | None}],
            "charts": [{"fund_code": str, "html": str}],
            "summary": {"total": int, "success": int, "failed": int}
        }
    """
    steps = plan.get("steps", [])
    results = []
    success = 0
    failed = 0

    for step in steps:
        action = step["action"]
        params = step["params"]
        fn = FUND_ACTION_MAP.get(action)

        if not fn:
            results.append({"step": step, "data": None, "error": f"未知基金操作: {action}"})
            failed += 1
            continue

        try:
            data = fn(**params)
            results.append({"step": step, "data": data, "error": None})
            success += 1

        except FundExecutorError as e:
            results.append({"step": step, "data": None, "error": str(e)})
            failed += 1

    return {
        "results": results,
        "charts": [],
        "summary": {"total": len(steps), "success": success, "failed": failed},
    }
