"""基金表达车间 - 将基金执行结果转化为分析报告（迁移自 workshops/fund_expressor.py）。

LLM 调用统一走 app.agent.llm；数据格式与 Violetz 原契约一致。
"""
from __future__ import annotations

import json

from app.agent.fund.scorer import FundScorer, FundScore
from app.agent.llm import chat, chat_stream
from app.constants import FUND_DATA_SOURCE_DISCLAIMER


def _find_result(results: list[dict], action: str):
    """从执行结果中查找特定操作的数据"""
    for r in results:
        if r["step"]["action"] == action and not r["error"]:
            return r["data"]
    return None


def _filter_results_by_fund(results: list[dict], fund_code: str) -> list[dict]:
    """筛选指定基金代码的结果"""
    filtered = []
    for r in results:
        params_code = r["step"]["params"].get("fund_code", "")
        if params_code == fund_code:
            filtered.append(r)
    return filtered


FUND_REPORT_PROMPT = """你是 Violetz 投资顾问，一个专业、客观的基金分析师。基于以下真实的基金数据，生成一份简洁的基金分析报告。

要求：
1. 严格基于提供的数据，不要编造任何数字
2. 分析维度：当前净值表现、收益趋势、持仓分析、风险提示
3. 语言客观中立，不要给出买卖建议
4. 用中文输出，Markdown 格式
5. 包含一个小结段落

数据：
{data_json}"""


# ── 基金评分展示 ──

_FUND_DIMENSION_LABELS = {
    "return": ("📈", "收益表现"),
    "risk": ("🛡️", "风险控制"),
    "manager": ("👤", "基金经理"),
    "holdings": ("📦", "持仓质量"),
}

_FUND_WEIGHTS = {"return": 35, "risk": 25, "manager": 20, "holdings": 20}


def _score_label(score: float) -> str:
    if score >= 85:
        return "优秀"
    if score >= 70:
        return "良好"
    if score >= 55:
        return "中等"
    if score >= 40:
        return "需关注"
    return "差"


def _progress_bar(score: float, length: int = 10) -> str:
    s = max(0, min(100, score))
    filled = int(s / (100 / length))
    return "█" * filled + "░" * (length - filled)


def _format_fund_score(fund_score: FundScore) -> str:
    """将 FundScore 格式化为带标记的 markdown 文本"""
    total = fund_score.total_score
    stars = fund_score.rating_stars
    rating = fund_score.rating
    bd = fund_score.breakdown

    rows = []
    for key in ("return", "risk", "manager", "holdings"):
        dim = bd.get(key)
        if dim is None:
            continue
        emoji, name = _FUND_DIMENSION_LABELS.get(key, ("", key))
        s = dim.score
        w = _FUND_WEIGHTS.get(key, 0)
        label = _score_label(s)
        bar = _progress_bar(s)
        rows.append(f"| {emoji} {name} | {s:.0f} | {w}% | {bar} {label}")

    table = "\n".join(rows)

    return (
        "[__VZ_SCORE__]\n"
        f"| **🏆 Violetz 基金评分** | **{total:.0f}/100** | {stars} {rating} |\n"
        "|---|---|---|\n"
        f"{table}\n"
        "[__VZ_END__]"
    )


# ── 净值报告 ──


def _format_nav_report(nav: dict) -> str:
    """格式化净值报告"""
    code = nav.get("基金代码", "")
    name = nav.get("基金简称", "")
    unit_nav = nav.get("单位净值")
    cum_nav = nav.get("累计净值")
    daily_change = nav.get("日增长率")
    nav_date = nav.get("净值日期", "未知")

    lines = [f"## {name} ({code}) 实时净值", ""]
    if unit_nav is not None:
        change_str = f"{daily_change:+.2f}%" if daily_change is not None else "N/A"
        lines.append(f"**单位净值**: ¥{unit_nav:.4f} ({change_str})")
    if cum_nav is not None:
        lines.append(f"**累计净值**: ¥{cum_nav:.4f}")
    lines.append(f"**净值日期**: {nav_date}")

    if nav.get("申购状态"):
        lines.append(f"**申购状态**: {nav['申购状态']}")
    if nav.get("赎回状态"):
        lines.append(f"**赎回状态**: {nav['赎回状态']}")
    if nav.get("手续费") and nav["手续费"] != "-":
        lines.append(f"**手续费**: {nav['手续费']}")

    lines.append("")
    lines.append(f"*{FUND_DATA_SOURCE_DISCLAIMER}*")
    return "\n".join(lines)


# ── 持仓报告 ──


def _format_holdings_report(holdings: list[dict], topk: int = 10) -> str:
    """格式化持仓报告"""
    if not holdings:
        return ""

    lines = ["## 前十大持仓股票", ""]
    lines.append("| 序号 | 股票名称 | 占净值比例 | 持仓市值 |")
    lines.append("|------|----------|-----------|---------|")

    for i, h in enumerate(holdings[:topk], 1):
        name = h.get("股票名称", h.get("stock_name", ""))
        pct = h.get("占净值比例", "")
        mkv = h.get("持仓市值", "")
        lines.append(f"| {i} | {name} | {pct} | {mkv} |")

    return "\n".join(lines)


# ── 基本信息报告 ──


def _format_info_report(info: dict) -> str:
    """格式化基金基本信息报告"""
    if not info:
        return ""

    lines = ["## 基金概况", ""]
    show_fields = [
        ("基金全称", "基金全称"),
        ("基金类型", "基金类型"),
        ("成立日期", "成立日期"),
        ("净资产规模", "净资产规模"),
        ("基金管理人", "基金管理人"),
        ("基金经理人", "基金经理人"),
    ]
    for label, key in show_fields:
        val = info.get(key)
        if val and str(val) != "-" and str(val) != "暂无" and str(val) != "nan":
            lines.append(f"- **{label}**: {val}")
    return "\n".join(lines)


# ── 评级报告 ──


def _format_ratings_report(ratings: dict) -> str:
    """格式化评级报告"""
    if not ratings:
        return ""
    lines = ["## 基金评级", ""]
    for agency, rating in ratings.items():
        if rating and str(rating) != "-":
            # 星级显示
            star_str = "⭐" * int(rating) if str(rating).isdigit() else str(rating)
            lines.append(f"- **{agency}**: {star_str}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


# ── AI 分析报告（流式）──


def _convert_for_json(obj):
    """递归将 numpy 类型转换为 Python 原生类型"""
    import numpy as np
    if isinstance(obj, dict):
        return {k: _convert_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_for_json(v) for v in obj]
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return _convert_for_json(obj.tolist())
    return obj


def _build_report_context(results: list[dict]) -> dict:
    """从执行结果构建报告上下文"""
    context = {}
    for r in results:
        if r["error"]:
            continue
        action = r["step"]["action"]
        data = r["data"]

        if action == "get_fund_nav":
            context["nav"] = data
        elif action == "get_fund_history":
            context["history_summary"] = data.get("summary") if data else None
        elif action == "get_fund_holdings":
            context["holdings"] = data[:10] if data else None
        elif action == "get_fund_info":
            context["fund_info"] = data
        elif action == "get_fund_ratings":
            context["ratings"] = data
    return _convert_for_json(context)


def stream_fund_analysis_report(results: list[dict], intent: str):
    """流式生成基金 AI 分析报告 token

    Args:
        results: execute_fund_plan() 的 results 列表
        intent: "fund_analysis" | "fund_compare"

    Yields:
        str: 逐个文本 token
    """
    context = _build_report_context(results)
    if not context:
        yield "（报告数据不足，无法生成分析。）"
        return

    data_json = json.dumps(context, ensure_ascii=False, indent=2)
    prompt = FUND_REPORT_PROMPT.format(data_json=data_json)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"请根据以上数据，生成一份{'对比分析' if intent == 'fund_compare' else '综合分析'}报告。"},
    ]

    try:
        for token in chat_stream(messages, task="report"):
            yield token
    except Exception:
        yield "（AI分析暂时不可用，请参考上方数据自行判断。）"


# ── 报告入口 ──


def generate_fund_report(execution_result: dict, intent_result: dict) -> str:
    """生成完整的基金分析报告

    Args:
        execution_result: fund_runner.execute_fund_plan() 的输出
        intent_result: fund_intent.parse() 的输出

    Returns:
        str: 包含评分和合规声明的完整报告
    """
    results = execution_result.get("results", [])
    intent = intent_result.get("intent", "fund_nav")
    total = execution_result["summary"]["total"]
    success = execution_result["summary"]["success"]

    # 全部失败
    if success == 0:
        errors = [r["error"] for r in results if r["error"]]
        text = f"## 数据获取失败\n\n{'  \n'.join(f'- ❌ {e}' for e in errors)}"
        return text

    parts = []
    fund_codes = intent_result.get("fund_codes", [])

    # 不同意图的报告策略
    if intent == "fund_nav":
        for code in fund_codes:
            nav = _find_result_by_code(results, "get_fund_nav", code)
            if nav:
                parts.append(_format_nav_report(nav))

    elif intent == "fund_history":
        for code in fund_codes:
            nav = _find_result_by_code(results, "get_fund_nav", code)
            if nav:
                parts.append(_format_nav_report(nav))
            history = _find_result_by_code(results, "get_fund_history", code)
            if history and history.get("summary"):
                s = history["summary"]
                lines = ["## 历史净值摘要", ""]
                lines.append(f"**分析区间**: {s.get('start_date')} → {s.get('end_date')} ({s.get('days')}个交易日)")
                if s.get("start_nav") and s.get("end_nav"):
                    change = (s["end_nav"] - s["start_nav"]) / s["start_nav"] * 100
                    lines.append(f"**始末净值**: ¥{s['start_nav']:.4f} → ¥{s['end_nav']:.4f} ({change:+.2f}%)")
                if s.get("max_daily_increase"):
                    lines.append(f"**最大日涨幅**: {s['max_daily_increase']:+.2f}%")
                if s.get("max_daily_decrease"):
                    lines.append(f"**最大日跌幅**: {s['max_daily_decrease']:+.2f}%")
                if s.get("avg_daily_change"):
                    lines.append(f"**日均涨跌**: {s['avg_daily_change']:+.4f}%")
                parts.append("\n".join(lines))

    elif intent == "fund_holdings":
        for code in fund_codes:
            holdings = _find_result_by_code(results, "get_fund_holdings", code)
            if holdings:
                parts.append(_format_holdings_report(holdings))
            info = _find_result_by_code(results, "get_fund_info", code)
            if info:
                parts.append(_format_info_report(info))

    elif intent in ("fund_analysis", "fund_compare"):
        # 显示净值
        for code in fund_codes:
            nav = _find_result_by_code(results, "get_fund_nav", code)
            if nav:
                parts.append(_format_nav_report(nav))

        # 显示基本信息
        for code in fund_codes:
            info = _find_result_by_code(results, "get_fund_info", code)
            if info:
                parts.append(_format_info_report(info))

        # 显示持仓
        for code in fund_codes:
            holdings = _find_result_by_code(results, "get_fund_holdings", code)
            if holdings:
                parts.append(_format_holdings_report(holdings))

        # 显示评级
        for code in fund_codes:
            ratings = _find_result_by_code(results, "get_fund_ratings", code)
            if ratings:
                r = _format_ratings_report(ratings)
                if r:
                    parts.append(r)

        # AI 分析（非流式）
        analysis_context = _build_report_context(results)
        if analysis_context:
            try:
                data_json = json.dumps(analysis_context, ensure_ascii=False, indent=2)
                prompt = FUND_REPORT_PROMPT.format(data_json=data_json)
                analysis_text = chat([
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"请根据以上数据，生成一份{'对比分析' if intent == 'fund_compare' else '综合分析'}报告。"},
                ])
                parts.append(analysis_text)
            except Exception:
                parts.append("（AI分析暂时不可用，请参考上方数据自行判断。）")

        # 评分
        scorer = FundScorer()
        for code in fund_codes:
            nav = _find_result_by_code(results, "get_fund_nav", code)
            history = _find_result_by_code(results, "get_fund_history", code)
            holdings = _find_result_by_code(results, "get_fund_holdings", code)
            info = _find_result_by_code(results, "get_fund_info", code)
            ratings = _find_result_by_code(results, "get_fund_ratings", code)
            fund_score = scorer.compute(
                nav_data=nav,
                history_data=history,
                holdings_data=holdings,
                info_data=info,
                ratings_data=ratings,
            )
            parts.append(_format_fund_score(fund_score))

    # 合并报告
    return "\n\n".join(parts)


def _find_result_by_code(results: list[dict], action: str, fund_code: str):
    """按 fund_code 和 action 查找结果"""
    for r in results:
        if r["error"]:
            continue
        if r["step"]["action"] == action:
            params_code = r["step"]["params"].get("fund_code", "")
            if params_code == fund_code:
                return r["data"]
    return None


def stream_fund_report_tokens(execution_result: dict, intent_result: dict):
    """流式生成基金报告 token（生成器，用于 SSE 推送）

    Args:
        execution_result: fund_runner.execute_fund_plan() 的输出
        intent_result: fund_intent.parse() 的输出

    Yields:
        str: 逐个文本 token
    """
    results = execution_result.get("results", [])
    intent = intent_result.get("intent", "fund_nav")
    total = execution_result["summary"]["total"]
    success = execution_result["summary"]["success"]
    fund_codes = intent_result.get("fund_codes", [])

    # 全部失败
    if success == 0:
        errors = [r["error"] for r in results if r["error"]]
        yield f"## 数据获取失败\n\n" + "\n".join(f"- ❌ {e}" for e in errors)
        return

    if intent == "fund_nav":
        for code in fund_codes:
            nav = _find_result_by_code(results, "get_fund_nav", code)
            if nav:
                yield _format_nav_report(nav) + "\n\n"

    elif intent == "fund_history":
        for code in fund_codes:
            nav = _find_result_by_code(results, "get_fund_nav", code)
            if nav:
                yield _format_nav_report(nav) + "\n\n"
            history = _find_result_by_code(results, "get_fund_history", code)
            if history and history.get("summary"):
                s = history["summary"]
                lines = ["## 历史净值摘要", ""]
                lines.append(f"**分析区间**: {s.get('start_date')} → {s.get('end_date')} ({s.get('days')}个交易日)")
                if s.get("start_nav") and s.get("end_nav"):
                    change = (s["end_nav"] - s["start_nav"]) / s["start_nav"] * 100
                    lines.append(f"**始末净值**: ¥{s['start_nav']:.4f} → ¥{s['end_nav']:.4f} ({change:+.2f}%)")
                if s.get("max_daily_increase"):
                    lines.append(f"**最大日涨幅**: {s['max_daily_increase']:+.2f}%")
                if s.get("max_daily_decrease"):
                    lines.append(f"**最大日跌幅**: {s['max_daily_decrease']:+.2f}%")
                yield "\n".join(lines) + "\n\n"

    elif intent == "fund_holdings":
        for code in fund_codes:
            holdings = _find_result_by_code(results, "get_fund_holdings", code)
            if holdings:
                yield _format_holdings_report(holdings) + "\n\n"
            info = _find_result_by_code(results, "get_fund_info", code)
            if info:
                yield _format_info_report(info) + "\n\n"

    elif intent in ("fund_analysis", "fund_compare"):
        # 净值
        for code in fund_codes:
            nav = _find_result_by_code(results, "get_fund_nav", code)
            if nav:
                yield _format_nav_report(nav) + "\n\n"

        # 基金概况
        for code in fund_codes:
            info = _find_result_by_code(results, "get_fund_info", code)
            if info:
                yield _format_info_report(info) + "\n\n"

        # 持仓
        for code in fund_codes:
            holdings = _find_result_by_code(results, "get_fund_holdings", code)
            if holdings:
                yield _format_holdings_report(holdings) + "\n\n"

        # 评级
        for code in fund_codes:
            ratings = _find_result_by_code(results, "get_fund_ratings", code)
            if ratings:
                r = _format_ratings_report(ratings)
                if r:
                    yield r + "\n\n"

        # AI 分析
        yield "\n---\n\n"
        yield from stream_fund_analysis_report(results, intent)
        yield "\n\n"

        # 评分
        scorer = FundScorer()
        for code in fund_codes:
            nav = _find_result_by_code(results, "get_fund_nav", code)
            history = _find_result_by_code(results, "get_fund_history", code)
            holdings = _find_result_by_code(results, "get_fund_holdings", code)
            info = _find_result_by_code(results, "get_fund_info", code)
            ratings = _find_result_by_code(results, "get_fund_ratings", code)
            fund_score = scorer.compute(
                nav_data=nav,
                history_data=history,
                holdings_data=holdings,
                info_data=info,
                ratings_data=ratings,
            )
            yield _format_fund_score(fund_score)
