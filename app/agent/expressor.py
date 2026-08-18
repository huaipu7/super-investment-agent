"""表达车间 - 将执行结果转化为分析报告（迁移自 workshops/expressor.py）。

LLM 调用统一走 app.agent.llm；数据格式与 Violetz 原契约一致。
"""
from __future__ import annotations

import json

from app.agent.ambiguity import get_market_label
from app.agent.llm import chat, chat_stream
from app.agent.safety import check_output
from app.agent.scorer import VioletzScorer
from app.compliance.rules import (
    COMPLIANCE_DISCLAIMER, apply_gate, check_advice, content_hash,
)
from app.constants import DATA_SOURCE_DISCLAIMER


def _find_result(results: list[dict], action: str) -> any:
    """从执行结果中查找特定操作的数据"""
    for r in results:
        if r["step"]["action"] == action and not r["error"]:
            return r["data"]
    return None


def _filter_results_by_ticker(results: list[dict], ticker: str) -> list[dict]:
    """从执行结果中筛选指定 ticker 的数据"""
    filtered = []
    for r in results:
        params_ticker = r["step"]["params"].get("ticker", "")
        if params_ticker.upper() == ticker.upper():
            filtered.append(r)
    return filtered


REPORT_PROMPT = """你是 Violetz 投资顾问，一个专业、客观的金融分析师。基于以下真实的金融数据，生成一份简洁的投资分析报告。

要求：
1. 严格基于提供的数据，不要编造任何数字
2. 分析维度：当前价格位置、趋势判断、关键指标解读、风险点提示
3. 语言客观中立，不要给出买卖建议
4. 用中文输出，Markdown 格式
5. 包含一个小结段落
{history_context}
数据：
{data_json}"""


def _build_context_summary(history: list[dict] | None) -> str:
    """从对话历史构建上下文摘要，注入分析 prompt"""
    if not history:
        return ""

    user_msgs = [h["text"] for h in history if h.get("role") == "user"]
    if not user_msgs:
        return ""

    recent = user_msgs[-3:]
    lines = ["6. 注意对话上下文：用户之前已查询了以下内容，请在分析时考虑连贯性"]
    for i, text in enumerate(recent, 1):
        # 截取过长文本
        short = text[:60] + "..." if len(text) > 60 else text
        lines.append(f"   {i}. {short}")
    lines.append("     当前分析应基于以上上下文进行适当扩展，避免简单重复已提供的信息。")
    return "\n".join(lines)


def _fmt_cap(cap: float, currency: str) -> str:
    """按货币格式化市值（CNY 用万亿/亿，其他用 T/B/M），避免把人民币市值标成 $。"""
    if currency == "CNY":
        if cap >= 1e12:
            return f"¥{cap/1e12:.2f}万亿"
        if cap >= 1e8:
            return f"¥{cap/1e8:.2f}亿"
        return f"¥{cap/1e4:.0f}万"
    if cap >= 1e12:
        return f"${cap/1e12:.2f}T"
    if cap >= 1e9:
        return f"${cap/1e9:.2f}B"
    return f"${cap/1e6:.2f}M"


def _format_price_report(quote: dict) -> str:
    """格式化简单报价报告"""
    ticker = quote.get("ticker", "")
    name = quote.get("company_name", ticker)
    price = quote.get("current_price", "N/A")
    currency = quote.get("currency", "USD")
    change = quote.get("change_percent", 0)
    change_str = f"+{change:.2f}%" if change and change >= 0 else f"{change:.2f}%" if change else "N/A"
    direction = "📈" if change and change >= 0 else "📉"

    market_label = get_market_label(ticker)
    lines = [
        f"## {market_label} {name} ({ticker}) 实时报价",
        "",
        f"**当前价格**: {currency} {price:.2f} {direction} ({change_str})",
    ]

    if quote.get("open"):
        lines.append(f"**开盘**: {quote['open']:.2f} | **前收盘**: {quote.get('previous_close', 0):.2f}")
    if quote.get("day_high"):
        lines.append(f"**日内区间**: {quote.get('day_low', 0):.2f} ~ {quote['day_high']:.2f}")
    if quote.get("volume"):
        lines.append(f"**成交量**: {quote['volume']:,}")
    if quote.get("market_cap"):
        lines.append(f"**市值**: {_fmt_cap(quote['market_cap'], currency)}")
    if quote.get("pe_ratio"):
        lines.append(f"**市盈率(PE)**: {quote['pe_ratio']:.2f}")
    if quote.get("52_week_high"):
        lines.append(f"**52周范围**: {quote.get('52_week_low', 0):.2f} ~ {quote['52_week_high']:.2f}")

    lines.append("")
    lines.append(f"*{DATA_SOURCE_DISCLAIMER}*")
    return "\n".join(lines)


def _format_history_report(ticker: str, df) -> str:
    """格式化历史行情摘要"""
    if df is None or len(df) == 0:
        return ""
    first_close = df["Close"].iloc[0]
    last_close = df["Close"].iloc[-1]
    change_pct = ((last_close - first_close) / first_close) * 100
    direction = "📈" if change_pct >= 0 else "📉"
    high = df["High"].max()
    low = df["Low"].min()
    avg_vol = df["Volume"].mean()

    return (
        f"## 行情摘要\n\n"
        f"**区间**: {df.index[0].date()} → {df.index[-1].date()} ({len(df)}个交易日)\n\n"
        f"**始末**: {first_close:.2f} → {last_close:.2f} ({change_pct:+.2f}%) {direction}\n\n"
        f"**最高**: {high:.2f} | **最低**: {low:.2f}\n\n"
        f"**日均成交量**: {avg_vol:,.0f}\n"
    )


def _format_financials_report(fin: dict) -> str:
    """格式化财务报告"""
    currency = fin.get("currency", "USD")
    lines = [f"## {fin.get('company_name', fin.get('ticker', ''))} 财务概览", ""]
    fin_items = {
        "市值": ("market_cap", lambda v: _fmt_cap(v, currency)),
        "企业价值": ("enterprise_value", lambda v: _fmt_cap(v, currency)),
        "市盈率(PE)": ("pe_ratio", lambda v: f"{v:.2f}"),
        "前瞻PE": ("forward_pe", lambda v: f"{v:.2f}"),
        "市净率(PB)": ("price_to_book", lambda v: f"{v:.2f}"),
        "营收": ("revenue", lambda v: f"${v/1e9:.2f}B"),
        "营收增长率": ("revenue_growth", lambda v: f"{v*100:.1f}%"),
        "利润率": ("profit_margin", lambda v: f"{v*100:.1f}%"),
        "每股收益(EPS)": ("eps", lambda v: f"${v:.2f}"),
        "股息率": ("dividend_yield", lambda v: f"{v*100:.2f}%"),
        "净资产收益率(ROE)": ("return_on_equity", lambda v: f"{v*100:.1f}%"),
        "自由现金流": ("free_cash_flow", lambda v: f"${v/1e9:.2f}B"),
    }
    for label, (key, fmt) in fin_items.items():
        if key in fin:
            lines.append(f"- **{label}**: {fmt(fin[key])}")
    lines.append("")
    lines.append(f"*{DATA_SOURCE_DISCLAIMER}*")
    return "\n".join(lines)


def _format_analysis_report(results: list[dict], intent: str, history: list[dict] = None) -> str:
    """调用 DeepSeek 生成深度分析报告"""
    context = _build_report_context(results)
    history_context = _build_context_summary(history) if history else ""

    try:
        prompt = REPORT_PROMPT.format(
            data_json=json.dumps(context, ensure_ascii=False, indent=2),
            history_context=history_context,
        )
        report = chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"请根据以上数据，生成一份{'对比分析' if intent == 'compare' else '综合分析'}报告。"},
        ], task="report")
        return report
    except Exception:
        return "（AI分析暂时不可用，请参考上方数据自行判断。）"


def _format_sentiment_report(sentiment: dict) -> str:
    """格式化情绪报告段"""
    if sentiment.get("error"):
        return ""

    score = sentiment.get("score", 0)
    label = sentiment.get("label", "中性")
    emoji = sentiment.get("emoji", "⚪")
    summary = sentiment.get("summary", "")
    articles = sentiment.get("articles", [])

    if not articles:
        return ""

    lines = [
        f"## {emoji} 市场情绪",
        "",
        f"**情绪评分**: {score:+.2f} ({label})",
        f"**情绪概要**: {summary}",
        "",
    ]
    for a in articles:
        title = a.get('title', '')
        link = a.get('link', '')
        sentiment_label = a.get('sentiment', '')
        if link:
            lines.append(f"- [{title}]({link}) | {sentiment_label}")
        else:
            lines.append(f"- {title} | {sentiment_label}")

    return "\n".join(lines)


_VZ_DIMENSION_LABELS = {
    "value": ("📊", "估值价值"),
    "growth": ("📈", "成长能力"),
    "health": ("🛡️", "财务健康"),
    "sentiment": ("📰", "市场情绪"),
}

_VZ_WEIGHTS = {"value": 30, "growth": 25, "health": 25, "sentiment": 20}


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


def _format_violetz_score(vz_score) -> str:
    """将 VioletzScore 格式化为带标记的 markdown 文本"""
    total = vz_score.total_score
    stars = vz_score.rating_stars
    rating = vz_score.rating
    bd = vz_score.breakdown

    rows = []
    for key in ("value", "growth", "health", "sentiment"):
        dim = bd.get(key)
        if dim is None:
            continue
        emoji, name = _VZ_DIMENSION_LABELS.get(key, ("", key))
        s = dim.score
        w = _VZ_WEIGHTS.get(key, 0)
        label = _score_label(s)
        bar = _progress_bar(s)
        rows.append(f"| {emoji} {name} | {s:.0f} | {w}% | {bar} {label}")

    table = "\n".join(rows)

    return (
        "[__VZ_SCORE__]\n"
        f"| **Violetz 推荐评分** | **{total:.0f}/100** | {stars} {rating} |\n"
        "|---|---|---|\n"
        f"{table}\n"
        "[__VZ_END__]"
    )


def _build_report_context(results: list[dict]) -> dict:
    """从执行结果构建报告上下文（AI 分析的输入数据）"""
    context = {}
    for r in results:
        if r["error"]:
            continue
        action = r["step"]["action"]
        data = r["data"]
        if action in ("get_quote", "get_financials", "get_ticker_info"):
            context[action] = data
        elif action == "get_history" and hasattr(data, "to_dict"):
            df = data
            context["history_summary"] = {
                "start_date": str(df.index[0].date()),
                "end_date": str(df.index[-1].date()),
                "days": len(df),
                "start_price": round(float(df["Close"].iloc[0]), 2),
                "end_price": round(float(df["Close"].iloc[-1]), 2),
                "high": round(float(df["High"].max()), 2),
                "low": round(float(df["Low"].min()), 2),
                "avg_volume": int(df["Volume"].mean()),
                "change_pct": round(float((df["Close"].iloc[-1] - df["Close"].iloc[0]) / df["Close"].iloc[0] * 100), 2),
            }
        elif action == "get_news_sentiment" and isinstance(data, dict):
            context["sentiment"] = {
                "ticker": data.get("ticker"),
                "score": data.get("score"),
                "label": data.get("label"),
                "summary": data.get("summary"),
                "articles": data.get("articles", []),
            }
    return context


def stream_analysis_report(results: list[dict], intent: str, history: list[dict] = None):
    """流式生成 AI 分析报告 token（生成器，用于 SSE 推送）

    Args:
        results: execute_plan() 的 results 列表
        intent: "analysis" | "compare"
        history: 可选，对话历史用于上下文增强

    Yields:
        str: 逐个文本 token
    """
    context = _build_report_context(results)
    if not context:
        yield "（报告数据不足，无法生成分析。）"
        return

    history_context = _build_context_summary(history) if history else ""
    data_json = json.dumps(context, ensure_ascii=False, indent=2)
    prompt = REPORT_PROMPT.format(
        data_json=data_json,
        history_context=history_context,
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"请根据以上数据，生成一份{'对比分析' if intent == 'compare' else '综合分析'}报告。"},
    ]

    # 流式输出；末尾做合规检测，命中则追加免责声明 + 落复核队列
    full_text = ""
    try:
        for token in chat_stream(messages, task="report"):
            full_text += token
            yield token
    except Exception:
        yield "（AI分析暂时不可用，请参考上方数据自行判断。）"
        return
    flagged, hits = check_advice(full_text)
    if flagged:
        from app.compliance.queue import flag as queue_flag
        queue_flag(source="analysis_stream", content_hash=content_hash(full_text),
                   snippet=full_text[:200], hit_words=hits, advice_flag=1)
        yield COMPLIANCE_DISCLAIMER


def stream_compare_pros_cons(results: list[dict], tickers: list[str], history: list[dict] = None):
    """逐 ticker 生成优缺点对比输出（带 PC_GRID/PC_CONS/PC_END 标记）

    Args:
        results: execute_plan() 的 results 列表
        tickers: 对比 ticker 列表
        history: 对话历史

    Yields:
        str: 文本片段，前端 renderMarkdown 转换为子栏布局
    """
    pros_cons_prompt = """你是 Violetz 投资顾问。根据以下金融数据，列出 {ticker} 的投资优缺点。

要求：
1. 严格基于提供的数据，不要编造
2. 每行以✅或❌开头（不要加序号），✅表示优点，❌表示缺点
3. 先列出所有✅优点，再列出所有❌缺点
4. 每条控制在 30 字以内，简洁明了
5. 至少各列 2 条

数据：
{data_json}"""

    for i, ticker in enumerate(tickers):
        if i > 0:
            yield "\n\n[__COL__]\n\n"

        ticker_results = _filter_results_by_ticker(results, ticker)
        context = _build_report_context(ticker_results)
        if not context:
            yield f"**{ticker}**\n\n数据不足，无法生成优缺点分析。\n\n"
            continue

        data_json = json.dumps(context, ensure_ascii=False, indent=2)
        prompt = pros_cons_prompt.format(ticker=ticker, data_json=data_json)
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"请基于数据列出 {ticker} 的优缺点。"},
        ]

        full_text = ""
        try:
            for token in chat_stream(messages, task="report"):
                full_text += token
        except Exception:
            yield f"**{ticker}**\n\n优缺点分析暂时不可用。\n\n"
            continue

        # 解析 ✅/❌ 行（清理可能的 bullet 前缀）
        pros, cons = [], []
        for line in full_text.split("\n"):
            s = line.strip()
            if not s:
                continue
            if s.startswith("- ") or s.startswith("* "):
                s = s[2:]
            if s.startswith("✅"):
                pros.append(s)
            elif s.startswith("❌"):
                cons.append(s)

        # 输出标题 + 子栏标记（注意：标记之间不要 \n\n，保持单段落）
        yield f"**{ticker}**\n"
        yield "[__PC_GRID__]\n"
        if pros:
            yield "\n".join(f"- {p}" for p in pros) + "\n"
        else:
            yield "- ✅ 暂无充分数据分析\n"
        yield "[__PC_CONS__]\n"
        if cons:
            yield "\n".join(f"- {c}" for c in cons) + "\n"
        else:
            yield "- ❌ 暂无充分数据分析\n"
        yield "[__PC_END__]\n"


def generate_report(execution_result: dict, intent_result: dict, history: list[dict] = None) -> str:
    """生成完整的分析报告

    Args:
        execution_result: runner.execute_plan() 的输出
        intent_result: intent.parse() 的输出

    Returns:
        str: 包含合规声明的完整报告
    """
    results = execution_result.get("results", [])
    intent = intent_result.get("intent", "query_price")
    tickers = intent_result.get("tickers", [])
    total = execution_result["summary"]["total"]
    success = execution_result["summary"]["success"]

    # 全部失败
    if success == 0:
        errors = [r["error"] for r in results if r["error"]]
        text = f"## 数据获取失败\n\n{'  \n'.join(f'- ❌ {e}' for e in errors)}"
        gated, _, _ = apply_gate(check_output(text)["sanitized_text"], source="chat_error")
        return gated

    parts = []

    # 不同类型的报告策略
    if intent == "query_price":
        quote = _find_result(results, "get_quote")
        if quote:
            parts.append(_format_price_report(quote))

    elif intent == "query_history":
        quote = _find_result(results, "get_quote")
        if quote:
            parts.append(_format_price_report(quote))
        df = _find_result(results, "get_history")
        if df is not None:
            parts.append(_format_history_report(tickers[0], df))

    elif intent == "financials":
        fin = _find_result(results, "get_financials")
        if fin:
            parts.append(_format_financials_report(fin))

    elif intent in ("analysis", "compare"):
        # 先展示数据摘要
        for t in tickers:
            quote = _find_result(results, "get_quote")
            if quote and quote.get("ticker") == t.upper():
                parts.append(_format_price_report(quote))
        # 再生成AI分析
        analysis = _format_analysis_report(results, intent, history=history)
        parts.append(analysis)
        # 最后嵌入情绪评分
        for t in tickers:
            sentiment = _find_result(results, "get_news_sentiment")
            if sentiment and sentiment.get("ticker") == t.upper():
                sentiment_section = _format_sentiment_report(sentiment)
                if sentiment_section:
                    parts.append(sentiment_section)

    else:
        # 兜底：列出所有可用数据
        for r in results:
            if not r["error"]:
                parts.append(f"- {r['step']['description']}: ✅ 成功获取")

    # 合并报告
    raw = "\n\n".join(parts)

    # 安全检查 + 合规闸门（命中建议模式 → 追加免责声明 + 落复核队列）
    safe_result = check_output(raw)
    gated, _, _ = apply_gate(safe_result["sanitized_text"], source="chat_report")
    return gated
