"""市场情绪 API —— /api/v1/market/*

GET /api/v1/market/sentiment?market=a_share|us|hk|all&llm=true|false
    返回市场级情绪评分（多源真实信号 → 评分 → 标签 + LLM 解读）。

数据源健康/请求计数走统一 metrics；审计走统一 audit。结果按「市场+日期+llm
开关」缓存（市场情绪变化慢，避免频繁打上游数据源）。
"""
from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter

from app.analysis.market_sentiment import MARKETS, compute_market_sentiment
from app.audit.log import audit
from app.audit.middleware import get_request_id
from app.data.cache import CACHE_TTL, get_cache
from app.observability.metrics import metrics

router = APIRouter(prefix="/api/v1/market", tags=["market"])


@router.get("/sentiment")
def sentiment(market: Literal["a_share", "us", "hk", "all"] = "all",
              llm: bool = True):
    """市场级情绪评分。llm=true 时追加 LLM 解读（未就绪/失败 → None）。"""
    metrics.inc("market_sentiment_requests")
    rid = get_request_id()
    include_llm = bool(llm)
    cache = get_cache()
    today = dt.date.today().isoformat()
    llm_part = "llm" if include_llm else "nollm"

    def _one(m: str) -> dict:
        ck = ["market_sentiment", m, today, llm_part]
        cached = cache.get_dict(*ck)
        if cached is not None:
            return cached
        data = compute_market_sentiment(m, llm=include_llm)
        cache.set_dict(data, CACHE_TTL["sentiment"], *ck)
        return data

    if market == "all":
        markets = {m: _one(m) for m in MARKETS}
        as_of = max((v.get("as_of") for v in markets.values() if v.get("as_of")),
                    default=None)
        result = {
            "markets": markets,
            "as_of": as_of,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        audit("market.sentiment", {
            "market": market,
            "scores": {m: v.get("score") for m, v in markets.items()},
        }, request_id=rid)
        return result

    result = _one(market)
    audit("market.sentiment", {
        "market": market,
        "score": result.get("score"),
        "label": result.get("label"),
    }, request_id=rid)
    return result
