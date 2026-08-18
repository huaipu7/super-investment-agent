"""个股新闻/公告/股东大会入库 —— 深度研究运行时按股增量补录。

统一 news item schema（复用 app/data/router.get_news/get_meetings）：
  articles/briefings: {title, summary, published, source, url}
  meetings: {name, start_date, record_date, resolution_date, announcement_date, proposals}
"""
from __future__ import annotations

from app.data.router import get_market_data
from app.rag.store import doc_id, get_knowledge_store
from app.utils.logger import logger

NEWS_LIMIT = 20


def _news_doc(ticker: str, item: dict, kind: str) -> dict:
    title = str(item.get("title", ""))
    summary = str(item.get("summary", ""))[:300]
    published = str(item.get("published", ""))[:10]
    content = f"{title}\n{summary}" if summary else title
    return {
        "id": doc_id(kind, ticker, str(item.get("url", "") or title), published),
        "page_content": content,
        "metadata": {
            "kind": kind,
            "ticker": ticker,
            "date": published,
            "source": str(item.get("source", "")),
            "section": kind,
            "url": str(item.get("url", "")),
        },
    }


def _meeting_doc(ticker: str, m: dict) -> dict:
    name = str(m.get("name", ""))
    proposals = str(m.get("proposals", ""))
    date = str(m.get("announcement_date") or m.get("start_date") or "")
    content = f"股东大会：{name}\n提案：{proposals}" if proposals else f"股东大会：{name}"
    return {
        "id": doc_id("股东大会", ticker, name, proposals),
        "page_content": content,
        "metadata": {
            "kind": "股东大会",
            "ticker": ticker,
            "date": date,
            "source": "巨潮资讯",
            "section": "股东大会",
            "url": "",
        },
    }


def index_ticker(ticker: str) -> dict:
    """拉取单股新闻/公告/股东大会并入知识库。返回 {news, meetings}。

    非 A 股（美股等）meetings 恒为 0（无可靠数据源，如实缺省）。
    """
    store = get_knowledge_store()
    md = get_market_data()
    result = {"news": 0, "meetings": 0}

    try:
        news = md.get_news(ticker, limit=NEWS_LIMIT)
        docs = [_news_doc(ticker, a, "新闻") for a in news.get("articles", [])]
        docs += [_news_doc(ticker, b, "公告") for b in news.get("briefings", [])]
        result["news"] = store.upsert(docs)
    except Exception as e:
        logger.warning("个股新闻入库失败 %s: %s", ticker, e)

    try:
        meetings = md.get_meetings(ticker, limit=5)
        if meetings:
            docs = [_meeting_doc(ticker, m) for m in meetings]
            result["meetings"] = store.upsert(docs)
    except Exception as e:
        logger.warning("个股股东大会入库失败 %s: %s", ticker, e)

    return result
