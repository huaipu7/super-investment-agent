"""计划执行器 - 按计划调度执行步骤并汇总结果（迁移自 workshops/runner.py）"""

from app.agent.executor import (
    get_quote,
    get_history,
    get_financials,
    get_ticker_info,
    get_news_sentiment,
    ExecutorError,
)


ACTION_MAP = {
    "get_quote": get_quote,
    "get_history": get_history,
    "get_financials": get_financials,
    "get_ticker_info": get_ticker_info,
    "get_news_sentiment": get_news_sentiment,
}


def execute_plan(plan: dict) -> dict:
    """执行计划并汇总结果

    简单顺序依赖：params 中的 {"$ref": "step.<index>.<field>..."} 会在执行
    前用前序步骤的输出解析（步骤本就顺序执行）。支持 multi-hop 让后续步骤
    复用前序数据。

    Args:
        plan: planner.build_plan() 的输出

    Returns:
        dict: {
            "results": [{"step": dict, "data": any, "error": str | None}, ...],
            "charts": [{"ticker": str, "html": str}, ...],
            "summary": {"total": int, "success": int, "failed": int}
        }
    """
    steps = plan.get("steps", [])
    results = []
    success = 0
    failed = 0

    for step in steps:
        action = step["action"]
        fn = ACTION_MAP.get(action)

        if not fn:
            results.append({"step": step, "data": None, "error": f"未知操作: {action}"})
            failed += 1
            continue

        try:
            params = _resolve_refs(step.get("params", {}), results)
            data = fn(**params)
            results.append({"step": step, "data": data, "error": None})
            success += 1

        except ExecutorError as e:
            results.append({"step": step, "data": None, "error": str(e)})
            failed += 1

    return {
        "results": results,
        "charts": [],
        "summary": {"total": len(steps), "success": success, "failed": failed},
    }


def _resolve_refs(params: dict, results: list[dict]) -> dict:
    """把 {"$ref": "step.<idx>.<field>..."} 解析为前序步骤输出的值。"""

    def _lookup(ref: str):
        parts = ref.split(".")
        if not parts or parts[0] != "step":
            return None
        try:
            idx = int(parts[1])
        except (ValueError, IndexError):
            return None
        if not (0 <= idx < len(results)):
            return None
        obj = results[idx].get("data")
        fields = parts[2:]
        if fields and fields[0] == "data":  # 容忍 "step.0.data.<field>" 写法
            fields = fields[1:]
        for field in fields:
            if isinstance(obj, dict):
                obj = obj.get(field)
            elif isinstance(obj, (list, tuple)) and field.isdigit():
                i = int(field)
                obj = obj[i] if 0 <= i < len(obj) else None
            else:
                return None
        return obj

    out: dict = {}
    for k, v in params.items():
        if isinstance(v, dict) and "$ref" in v and isinstance(v["$ref"], str):
            out[k] = _lookup(v["$ref"])
        else:
            out[k] = v
    return out
