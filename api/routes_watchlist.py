"""自选股 API —— /api/v1/watchlist/*（迁移自 Violetz api/watchlist.py + fund_watchlist.py）

股票自选 + 基金自选，SQLite 持久化（app/watchlist/db.py）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.watchlist import db
from app.agent.executor import get_quote, get_history
from app.agent.intent import TICKER_MAP
from app.data.router import get_market_data
from app.data.sources.fund_source import fund_search, fund_realtime_nav

router = APIRouter(prefix="/api/v1/watchlist", tags=["watchlist"])


class TickerRequest(BaseModel):
    ticker: str


class FundCodeRequest(BaseModel):
    fund_code: str
    fund_name: str = ""


# ── 股票自选 ──────────────────────────────────────────────────

@router.get("")
async def list_watchlist():
    """获取所有自选股代码列表"""
    return {"tickers": db.get_all_tickers()}


@router.post("/add")
async def add_to_watchlist(req: TickerRequest):
    """添加自选股"""
    ticker = req.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="股票代码不能为空")
    ok = db.add_ticker(ticker)
    if not ok:
        raise HTTPException(status_code=409, detail=f"{ticker} 已在自选列表中")
    return {"ok": True, "ticker": ticker}


@router.delete("/remove")
async def remove_from_watchlist(req: TickerRequest):
    """删除自选股"""
    ticker = req.ticker.strip().upper()
    ok = db.remove_ticker(ticker)
    if not ok:
        raise HTTPException(status_code=404, detail=f"{ticker} 不在自选列表中")
    return {"ok": True, "ticker": ticker}


@router.get("/suggest")
async def suggest_stocks(q: str = ""):
    """智能搜索建议：匹配公司名或股票代码"""
    if not q or len(q.strip()) < 1:
        return {"results": []}
    q = q.strip().lower()

    ticker_names: dict[str, list[str]] = {}
    for name, ticker in TICKER_MAP.items():
        t = ticker.upper()
        ticker_names.setdefault(t, []).append(name)

    results = []
    for ticker, names in ticker_names.items():
        label = names[0]  # 中文名优先
        if q in ticker.lower() or any(q in n.lower() for n in names):
            results.append({"label": label, "ticker": ticker})
    results.sort(key=lambda x: (
        0 if x["label"].lower().startswith(q) or x["ticker"].lower().startswith(q) else 1,
        x["label"],
    ))
    return {"results": results[:8]}


@router.get("/data")
async def watchlist_data():
    """获取所有自选股的实时数据（价格 + 涨跌幅 + 7日涨跌）"""
    tickers = db.get_all_tickers()
    items = []
    for ticker in tickers:
        try:
            quote = get_quote(ticker)
            change_7d = None
            try:
                hist = get_history(ticker, "5d")
                if hist is not None and len(hist) >= 2:
                    change_7d = round(
                        float((hist["Close"].iloc[-1] - hist["Close"].iloc[0])
                              / hist["Close"].iloc[0] * 100), 2)
            except Exception:
                pass
            items.append({
                "ticker": quote.get("ticker", ticker),
                "name": quote.get("company_name", ticker),
                "price": quote.get("current_price"),
                "currency": quote.get("currency", "USD"),
                "change_1d_pct": quote.get("change_percent"),
                "change_7d_pct": change_7d,
            })
        except Exception:
            continue
    return {"items": items}


@router.get("/news")
async def watchlist_news():
    """自选股最新新闻/公告 + 股东大会（逐个获取，单股异常跳过）"""
    tickers = db.get_all_tickers()
    md = get_market_data()
    items = []
    for ticker in tickers:
        try:
            quote = get_quote(ticker)
            news = md.get_news(ticker, limit=10)
            meetings = md.get_meetings(ticker)
            items.append({
                "ticker": quote.get("ticker", ticker),
                "name": quote.get("company_name", ticker),
                "news": {
                    "articles": (news or {}).get("articles", [])[:10],
                    "briefings": (news or {}).get("briefings", [])[:5],
                },
                "meetings": (meetings or [])[:3],
            })
        except Exception:
            continue
    return {"items": items}


# ── 基金自选 ──────────────────────────────────────────────────

@router.get("/fund")
async def list_fund_watchlist():
    """获取所有自选基金代码及名称"""
    return {"items": db.get_all_fund_tickers()}


@router.post("/fund/add")
async def add_to_fund_watchlist(req: FundCodeRequest):
    """添加自选基金"""
    code = req.fund_code.strip()
    name = req.fund_name.strip()
    if not code:
        raise HTTPException(status_code=400, detail="基金代码不能为空")
    ok = db.add_fund_ticker(code, name)
    if not ok:
        raise HTTPException(status_code=409, detail=f"{code} 已在自选列表中")
    return {"ok": True, "fund_code": code, "fund_name": name}


@router.delete("/fund/remove")
async def remove_from_fund_watchlist(req: FundCodeRequest):
    """删除自选基金"""
    code = req.fund_code.strip()
    ok = db.remove_fund_ticker(code)
    if not ok:
        raise HTTPException(status_code=404, detail=f"{code} 不在自选列表中")
    return {"ok": True, "fund_code": code}


@router.get("/fund/suggest")
async def suggest_funds(q: str = ""):
    """基金搜索建议：匹配基金代码或名称"""
    if not q or len(q.strip()) < 1:
        return {"results": []}
    try:
        results = fund_search(q.strip(), topk=8)
        return {"results": results[:8]}
    except Exception:
        return {"results": []}


@router.get("/fund/quote/{fund_code}")
async def fund_quote(fund_code: str):
    """单个基金实时净值（看板基金分析模块用）"""
    code = fund_code.strip()
    nav = fund_realtime_nav(code)
    if not nav:
        raise HTTPException(status_code=404, detail=f"未找到基金 {code} 的净值数据")
    return nav


@router.get("/fund/data")
async def fund_watchlist_data():
    """获取所有自选基金的实时净值数据"""
    items = db.get_all_fund_tickers()
    data_items = []
    for item in items:
        code = item["fund_code"]
        try:
            nav = fund_realtime_nav(code)
            if nav:
                data_items.append({
                    "fund_code": code,
                    "fund_name": nav.get("基金简称", item["fund_name"]),
                    "unit_nav": nav.get("单位净值"),
                    "daily_change": nav.get("日增长率"),
                    "nav_date": nav.get("净值日期", ""),
                })
            else:
                data_items.append({
                    "fund_code": code, "fund_name": item["fund_name"],
                    "unit_nav": None, "daily_change": None, "nav_date": "",
                })
        except Exception:
            continue
    return {"items": data_items}
