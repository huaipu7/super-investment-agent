"""多股横向对比 API —— /api/v1/compare

对多只股票逐个复用评分逻辑（routes_stock._build_score），
汇总统一指标矩阵（兼容 A股 akshare 百分数与 yfinance 小数两种 schema）。
少于 2 只股票返回 HTTP 400。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.routes_stock import _build_score

router = APIRouter(prefix="/api/v1", tags=["compare"])

_METRIC_ROWS = [
    {"key": "pe_ratio", "label": "PE 市盈率 (倍)"},
    {"key": "price_to_book", "label": "PB 市净率 (倍)"},
    {"key": "return_on_equity", "label": "ROE 净资产收益率"},
    {"key": "profit_margin", "label": "净利率"},
    {"key": "revenue_growth", "label": "营收增长率"},
    {"key": "debt_to_equity", "label": "负债/权益比 (D/E)"},
]


class CompareRequest(BaseModel):
    tickers: list[str]


def _num(v):
    try:
        return round(float(v), 4) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pct(v):
    """百分数（A股 akshare，如 29.79）→ 小数（0.2979）。"""
    try:
        return round(float(v) / 100, 4) if v is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_metrics(fin: dict, quote: dict) -> dict:
    """统一指标层：yfinance（ratio 小数）与 A股 akshare（百分数）→ 规范小数。

    缺失字段返回 None，前端如实显示"—"，不编造。
    """
    fin = fin or {}
    quote = quote or {}
    is_ashare_fin = any(k in fin for k in ("roe", "net_margin", "debt_ratio"))

    pe = _num(fin.get("pe_ratio"))
    if pe is None:
        pe = _num(quote.get("pe_ratio"))
    if pe is None:
        pe = _num(quote.get("pe"))
    pb = _num(fin.get("price_to_book"))
    if pb is None:
        pb = _num(quote.get("price_to_book"))
    if pb is None:
        pb = _num(quote.get("pb"))

    if is_ashare_fin:
        roe = _pct(fin.get("roe"))
        profit_margin = _pct(fin.get("net_margin"))
        revenue_growth = _pct(fin.get("revenue_growth"))
        d = _pct(fin.get("debt_ratio"))
        # 资产负债率 d = L/A → D/E = L/(A-L) = d/(1-d)，与 yfinance D/E 对齐（纯比率）
        debt_to_equity = round(d / (1 - d), 4) if d is not None and d < 1 else None
    else:
        roe = _num(fin.get("return_on_equity"))
        profit_margin = _num(fin.get("profit_margin"))
        revenue_growth = _num(fin.get("revenue_growth"))
        de = _num(fin.get("debt_to_equity"))
        # yfinance debtToEquity 为百分数（如 78.4 即 78.4%）→ 除以 100 得纯比率
        debt_to_equity = round(de / 100, 4) if de is not None else None

    return {
        "pe_ratio": pe,
        "price_to_book": pb,
        "return_on_equity": roe,
        "profit_margin": profit_margin,
        "revenue_growth": revenue_growth,
        "debt_to_equity": debt_to_equity,
    }


@router.post("/compare")
def compare(req: CompareRequest):
    tickers = list(dict.fromkeys(t.strip().upper() for t in req.tickers if t and t.strip()))
    if len(tickers) < 2:
        raise HTTPException(status_code=400, detail="至少选择2只股票")

    items = []
    errors: dict[str, str] = {}
    for t in tickers:
        try:
            data = _build_score(t)
            quote = data.get("quote") or {}
            items.append({
                "ticker": data["ticker"],
                "name": quote.get("company_name") or data["ticker"],
                "currency": quote.get("currency") or "USD",
                "quote": quote,
                "financial": data.get("financial") or {},
                "metrics": _normalize_metrics(data.get("financial") or {}, quote),
                "violetz_score": data.get("violetz_score"),
                "expert_score": data.get("expert_score"),
            })
        except Exception as e:
            errors[t] = str(e)

    return {
        "items": items,
        "metric_rows": _METRIC_ROWS,
        "errors": errors,
    }
