"""舆情情绪分析 - 新闻获取 + 关键词评分 + LLM 边界确认（迁移自 utils/sentiment.py）。

LLM 调用统一走 app.agent.llm（内部是统一 DeepSeek 客户端）。
"""
from __future__ import annotations

import re

import yfinance as yf

from app.agent.llm import chat

# 正面/负面关键词词库
POSITIVE_WORDS = [
    "上涨", "突破", "增长", "盈利", "创新高", "利好", "反弹", "放量",
    "upgrade", "beat", "growth", "surge", "rally", "bullish", "positive",
    "record", "profit", "outperform", "upgraded", "buy",
]

NEGATIVE_WORDS = [
    "下跌", "亏损", "调查", "下调", "降级", "风险", "诉讼", "做空",
    "layoff", "downgrade", "decline", "bearish", "negative", "sell",
    "loss", "cut", "investigation", "crash", "volatile", "overvalued",
]

# 股票代码 → 公司名 映射（用于标题匹配）
_KNOWN_TICKER_NAMES = {}


def _load_ticker_name(ticker: str) -> str | None:
    """从 yfinance 获取公司名并缓存"""
    if ticker not in _KNOWN_TICKER_NAMES:
        try:
            info = yf.Ticker(ticker).info
            name = info.get("longName") or info.get("shortName") or ""
            _KNOWN_TICKER_NAMES[ticker] = name
        except Exception:
            return None
    return _KNOWN_TICKER_NAMES[ticker]


def _extract_news_field(item: dict, field: str) -> str:
    """从 yfinance news item 中安全提取字段

    yfinance 返回格式: { "content": { "title": "...", "summary": "...", "provider": {...}, ... } }
    """
    content = item.get("content") or item
    if not isinstance(content, dict):
        return ""
    if field == "title":
        return content.get("title") or ""
    if field == "summary":
        return (content.get("summary") or content.get("description") or "")[:300]
    if field == "publisher":
        provider = content.get("provider") or {}
        return provider.get("displayName") or provider.get("name") or ""
    if field == "link":
        cu = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        return cu.get("url") or ""
    return ""


def _keyword_score(text: str) -> float:
    """对一条文本进行关键词情绪打分

    Returns:
        float: [-1, +1] 范围的分数
    """
    text_lower = text.lower()
    score = 0.0

    for word in POSITIVE_WORDS:
        count = len(re.findall(re.escape(word), text_lower, re.IGNORECASE))
        score += count * 0.25

    for word in NEGATIVE_WORDS:
        count = len(re.findall(re.escape(word), text_lower, re.IGNORECASE))
        score -= count * 0.25

    return max(-1.0, min(1.0, score))


def _llm_sentiment_score(articles: list[dict]) -> float:
    """调用 LLM 对新闻进行情绪打分（用于边界模糊情况）

    Returns:
        float: [-1, +1] 范围的分数
    """
    articles_text = "\n".join(
        f"{i+1}. {a.get('title', '')} - {a.get('summary', '')[:200]}"
        for i, a in enumerate(articles[:5])
    )
    prompt = (
        "你对以下关于某家公司的新闻标题进行整体市场情绪打分。\n\n"
        f"{articles_text}\n\n"
        "请从投资者的角度判断这些新闻传递的情绪是正面还是负面。\n"
        "仅返回一个 -1.0 到 +1.0 之间的数字（含一位小数），不要返回其他内容。\n"
        "-1.0 = 极度负面，0 = 中性，+1.0 = 极度正面。"
    )
    try:
        result = chat([
            {"role": "system", "content": "你是 Violetz 投资顾问的情绪分析专家。你只返回数字，不返回任何其他文字。"},
            {"role": "user", "content": prompt},
        ], max_tokens=10, task="sentiment")
        result = result.strip()
        score = float(result)
        return max(-1.0, min(1.0, score))
    except (ValueError, TypeError, Exception):
        return 0.0


def _determine_label(score: float) -> str:
    """根据分数确定情绪标签"""
    if score >= 0.5:
        return "正面"
    if score >= 0.15:
        return "偏正面"
    if score > -0.15:
        return "中性"
    if score > -0.5:
        return "偏负面"
    return "负面"


def _get_sentiment_emoji(score: float) -> str:
    """根据分数返回对应 emoji"""
    if score >= 0.5:
        return "🟢"
    if score >= 0.15:
        return "📗"
    if score > -0.15:
        return "⚪"
    if score > -0.5:
        return "📕"
    return "🔴"


def get_news_sentiment(ticker: str, max_articles: int = 5) -> dict:
    """获取股票的最新新闻并分析情绪

    Args:
        ticker: 股票代码
        max_articles: 最大获取新闻条数

    Returns:
        dict: {
            "ticker": str,
            "score": float,          # -1 ~ +1 综合情绪评分
            "label": str,            # 情绪标签（正面/偏正面/中性/偏负面/负面）
            "summary": str,          # 一句话情绪总结
            "articles": [            # 新闻列表
                {"title": str, "publisher": str, "link": str, "sentiment": str}
            ],
            "article_count": int,
            "error": str | None,
        }
    """
    try:
        stock = yf.Ticker(ticker)
        raw_news = stock.news

        if not raw_news or len(raw_news) == 0:
            return {
                "ticker": ticker.upper(),
                "score": 0.0,
                "label": "中性",
                "summary": "未获取到相关新闻",
                "articles": [],
                "article_count": 0,
                "error": None,
            }

        # 解析并限制条数
        articles = []
        for item in raw_news[:max_articles]:
            title = _extract_news_field(item, "title")
            summary = _extract_news_field(item, "summary")
            publisher = _extract_news_field(item, "publisher")
            link = _extract_news_field(item, "link")
            articles.append({
                "title": title,
                "summary": summary[:300],
                "publisher": publisher,
                "link": link,
                "raw_score": 0.0,
                "label": "中性",
            })

        # 关键词评分
        article_scores = []
        for article in articles:
            combined_text = f"{article['title']} {article['summary']}"
            score = _keyword_score(combined_text)
            article["raw_score"] = score
            article["label"] = _determine_label(score)
            article_scores.append(score)

        base_score = sum(article_scores) / len(article_scores) if article_scores else 0.0

        # 边界模糊 → LLM 精确打分
        if -0.3 <= base_score <= 0.3:
            llm_score = _llm_sentiment_score(articles)
            final_score = llm_score if llm_score != 0.0 else base_score
        else:
            final_score = base_score

        # 生成总结
        company_name = _load_ticker_name(ticker) or ticker.upper()
        label = _determine_label(final_score)
        if final_score >= 0.3:
            summary = f"{company_name} 近期舆情偏积极"
        elif final_score <= -0.3:
            summary = f"{company_name} 近期舆情偏消极"
        else:
            summary = f"{company_name} 近期舆情中性"

        # 返回结果时移除中间计算字段
        clean_articles = [
            {
                "title": a["title"],
                "publisher": a["publisher"],
                "link": a["link"],
                "sentiment": a["label"],
            }
            for a in articles
        ]

        return {
            "ticker": ticker.upper(),
            "score": round(final_score, 2),
            "label": label,
            "emoji": _get_sentiment_emoji(final_score),
            "summary": summary,
            "articles": clean_articles,
            "article_count": len(clean_articles),
            "error": None,
        }

    except Exception as e:
        return {
            "ticker": ticker.upper(),
            "score": 0.0,
            "label": "中性",
            "summary": "情绪分析暂不可用",
            "articles": [],
            "article_count": 0,
            "error": str(e),
        }
