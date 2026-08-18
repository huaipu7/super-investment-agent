"""行情 / 财务 / 评分 API —— /api/v1/stock/*

数据经统一数据层（app.agent.executor 的归一化工具函数），与对话 Agent 同一套逻辑。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.agent.executor import (
    get_quote, get_history, get_financials, get_ticker_info,
    search_tickers, get_news_sentiment,
    TickerNotFoundError, NoDataError,
)
from app.agent.scorer import VioletzScorer
from app.data.router import is_ashare

router = APIRouter(prefix="/api/v1/stock", tags=["stock"])

_PERIOD_WINDOW = {
    "5d": 5, "1mo": 21, "3mo": 63, "6mo": 126, "1y": 252, "2y": 504, "5y": 1260,
}


def _clean_ticker(ticker: str) -> str:
    return ticker.strip().upper()


def _build_score(ticker: str) -> dict:
    """单股评分数据包（/score 端点与 /compare 复用）。

    返回 {ticker, quote, financial, violetz_score, expert_score}；
    get_quote/get_financials 异常向上抛，由调用方映射为 HTTP 错误。
    """
    quote_data = get_quote(ticker)
    fin = get_financials(ticker)

    sentiment = None
    try:
        sentiment = get_news_sentiment(ticker, max_articles=3)
    except Exception:
        sentiment = None

    vz = None
    try:
        score_obj = VioletzScorer().compute(financials=fin, quote=quote_data, sentiment=sentiment)
        vz = {
            "total_score": score_obj.total_score,
            "rating": score_obj.rating,
            "rating_stars": score_obj.rating_stars,
            "breakdown": {
                k: {"score": v.score, "detail": v.detail}
                for k, v in score_obj.breakdown.items()
            },
            "available_metrics": score_obj.available_metrics,
            "total_metrics": score_obj.total_metrics,
        }
    except Exception:
        vz = None

    expert = None
    if is_ashare(ticker):
        try:
            from app.analysis.expert_scoring import run_expert_analysis
            expert = run_expert_analysis(ticker)
        except Exception:
            expert = None

    return {
        "ticker": ticker,
        "quote": quote_data,
        "financial": fin,
        "violetz_score": vz,
        "expert_score": expert,
    }


@router.get("/search")
def search(q: str = ""):
    """搜索股票：匹配 TICKER_MAP（中文/别名）+ 数据层搜索（代码/英文名）"""
    q = q.strip()
    if not q:
        return {"results": []}
    ql = q.lower()

    seen: dict[str, str] = {}  # ticker -> label
    # 1) TICKER_MAP：中文名 / 别名 直接命中
    try:
        from app.agent.intent import TICKER_MAP
        for name, ticker in TICKER_MAP.items():
            if ql in name.lower() or ql in str(ticker).lower():
                seen.setdefault(str(ticker).upper(), name)
    except Exception:
        pass

    # 2) 数据层搜索（yfinance：代码 / 英文名）
    try:
        for r in search_tickers(q, top_k=8):
            t = str(r.get("ticker", "")).upper()
            if t:
                seen.setdefault(t, r.get("name", t))
    except Exception:
        pass

    # 3) 直接输入为合法代码（数字或带交易所后缀）
    import re
    if re.fullmatch(r"[0-9]{4,6}(\.(SS|SH|SZ|BJ|HK))?", q.upper()):
        seen.setdefault(q.upper(), q.upper())

    results = [{"label": label, "ticker": t} for t, label in seen.items()]
    results.sort(key=lambda x: (
        0 if x["label"].lower().startswith(ql) or x["ticker"].lower().startswith(ql) else 1,
        x["label"],
    ))
    return {"results": results[:10]}


@router.get("/{ticker}")
def quote(ticker: str, as_of: str | None = None):
    """行情。as_of=YYYY-MM-DD 时返回 ≤as_of 最后一根真实历史 bar（附 vintage）。"""
    t = _clean_ticker(ticker)
    try:
        return get_quote(t, as_of=as_of)
    except TickerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NoDataError as e:
        raise HTTPException(status_code=502, detail=f"数据获取失败: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{ticker}/history")
def history(ticker: str, period: str = "1mo", as_of: str | None = None):
    """历史行情（含摘要统计）。as_of 锚定序列终点（≤as_of）。"""
    t = _clean_ticker(ticker)
    period = period if period in _PERIOD_WINDOW else "1mo"
    try:
        df = get_history(t, period, as_of=as_of)
    except TickerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NoDataError as e:
        raise HTTPException(status_code=502, detail=f"数据获取失败: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if df is None or len(df) == 0:
        raise HTTPException(status_code=502, detail=f"无 {t} 的历史数据")

    records = []
    for idx, row in df.iterrows():
        records.append({
            "date": str(idx.date()),
            "open": round(float(row["Open"]), 2),
            "high": round(float(row["High"]), 2),
            "low": round(float(row["Low"]), 2),
            "close": round(float(row["Close"]), 2),
            "volume": int(row["Volume"]) if "Volume" in df.columns else 0,
        })

    first, last = records[0]["close"], records[-1]["close"]
    return {
        "ticker": t,
        "period": period,
        "as_of": as_of or None,
        "vintage": df.attrs.get("vintage") if hasattr(df, "attrs") else None,
        "count": len(records),
        "summary": {
            "start_date": records[0]["date"],
            "end_date": records[-1]["date"],
            "start_close": first,
            "end_close": last,
            "change_pct": round((last - first) / first * 100, 2) if first else 0,
            "high": max(r["high"] for r in records),
            "low": min(r["low"] for r in records),
        },
        "records": records,
    }


@router.get("/{ticker}/benchmark")
def benchmark(ticker: str):
    """基准指数历史行情（A股→沪深300 / 港股→恒指 / 其他→标普500）。

    供回测绩效曲线/估值参照叠加。失败返回空 records（不抛 5xx）。
    """
    t = _clean_ticker(ticker)
    try:
        from app.data.router import get_market_data
        df = get_market_data().get_benchmark_history(t, "1y")
    except Exception:
        df = None
    records = []
    if df is not None and len(df) > 0:
        for idx, row in df.iterrows():
            records.append({
                "date": str(idx.date()),
                "close": round(float(row["Close"]), 2),
            })
    return {"ticker": t, "count": len(records), "records": records}


@router.get("/{ticker}/financials")
def financials(ticker: str, as_of: str | None = None):
    """财务概览。as_of 仅作查询锚点：免费源无法回退历史财务快照，
    返回最新一期并标注 is_point_in_time=False（附 vintage）。"""
    t = _clean_ticker(ticker)
    try:
        return get_financials(t, as_of=as_of)
    except TickerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NoDataError as e:
        raise HTTPException(status_code=502, detail=f"数据获取失败: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{ticker}/info")
def info(ticker: str):
    """公司信息"""
    t = _clean_ticker(ticker)
    try:
        return get_ticker_info(t)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{ticker}/score")
def score(ticker: str):
    """评分：Violetz 推荐评分 + （A股）四大师专家评分"""
    t = _clean_ticker(ticker)
    try:
        data = _build_score(t)
    except TickerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NoDataError as e:
        raise HTTPException(status_code=502, detail=f"数据获取失败: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ticker": data["ticker"], "violetz_score": data["violetz_score"],
            "expert_score": data["expert_score"]}
