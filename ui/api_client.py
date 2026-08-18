"""看板 → 后端 API 客户端（唯一网络入口）。

所有数据均指向 Phase 6 FastAPI 后端（127.0.0.1:8000）；
看板自身不抓取数据、不计算，只做展示与交互。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from app.config import get_settings

_BASE = f"http://127.0.0.1:{get_settings().SERVER_PORT}"
_session = requests.Session()


class ApiError(Exception):
    pass


def _url(path: str) -> str:
    return _BASE + path


def _req(method: str, path: str, **kw) -> requests.Response:
    # 默认 120s：A股专家评分/深度研究端点实测约 30-90s
    timeout = kw.pop("timeout", 120)
    try:
        r = _session.request(method, _url(path), timeout=timeout, **kw)
    except requests.RequestException as e:
        raise ApiError(f"无法连接后端 {_BASE}（请先启动 python api/main.py）: {e}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text[:300])
        except Exception:
            detail = r.text[:300]
        raise ApiError(f"{method} {path} -> HTTP {r.status_code}: {detail}")
    return r


def _json(method: str, path: str, **kw) -> dict | list:
    return _req(method, path, **kw).json()


# ── 基础 ──────────────────────────────────────────────

def health():
    try:
        return _json("GET", "/api/v1/health")
    except ApiError:
        return None


def metrics() -> dict:
    """可观测性指标快照（含 data_sources 数据源健康，Phase 4）。"""
    try:
        return _json("GET", "/api/v1/metrics").get("metrics", {})
    except ApiError:
        return {}


# ── 行情 / 财务 / 评分 ─────────────────────────────────

def search(q: str) -> list[dict]:
    return _json("GET", "/api/v1/stock/search", params={"q": q}).get("results", [])


def quote(ticker: str) -> dict:
    return _json("GET", f"/api/v1/stock/{ticker}")


def history(ticker: str, period: str = "1mo") -> dict:
    return _json("GET", f"/api/v1/stock/{ticker}/history", params={"period": period})


def benchmark(ticker: str) -> dict:
    return _json("GET", f"/api/v1/stock/{ticker}/benchmark")


def financials(ticker: str) -> dict:
    return _json("GET", f"/api/v1/stock/{ticker}/financials")


def info(ticker: str) -> dict:
    return _json("GET", f"/api/v1/stock/{ticker}/info")


def score(ticker: str) -> dict:
    return _json("GET", f"/api/v1/stock/{ticker}/score")


# ── 深度研究 ──────────────────────────────────────────

def analysis_run(ticker: str) -> dict:
    return _json("POST", f"/api/v1/analysis/run/{ticker}")


def analysis_status(ticker: str) -> dict:
    return _json("GET", f"/api/v1/analysis/status/{ticker}")


def analysis_result(ticker: str) -> dict:
    return _json("GET", f"/api/v1/analysis/result/{ticker}")


def analysis_report(ticker: str) -> dict:
    return _json("POST", f"/api/v1/analysis/report/{ticker}")


def analysis_stream(ticker: str) -> requests.Response:
    """SSE 进度流：progress → 阶段推进，status/done/error 收尾。"""
    return _req("GET", f"/api/v1/analysis/stream/{ticker}",
                stream=True, timeout=(10, 900))


def recompute_dcf(ticker: str, *, wacc: float | None = None,
                  terminal_growth: float = 0.03, projection_years: int = 5,
                  beta: float | None = None, risk_free_rate: float | None = None,
                  erp: float | None = None) -> dict:
    """估值参数重算 DCF（免费源内，无 LLM；参数 None 回退分析时基准值）。"""
    return _json("POST", "/api/v1/analysis/recompute-dcf", json={
        "ticker": ticker, "wacc": wacc, "terminal_growth": terminal_growth,
        "projection_years": projection_years, "beta": beta,
        "risk_free_rate": risk_free_rate, "erp": erp,
    })


def reports_list(kind: str = "all", ticker: str = "") -> list[dict]:
    return _json("GET", "/api/v1/reports/list", params={"kind": kind, "ticker": ticker})


def reports_download(name: str) -> bytes:
    return _req("GET", f"/api/v1/reports/download/{name}").content


# ── 多模态 ────────────────────────────────────────────

def multimodal_capabilities() -> dict:
    """多模态能力声明（上传控件可用类型 / 上限）。"""
    try:
        return _json("GET", "/api/v1/multimodal/capabilities")
    except ApiError:
        return {}


def multimodal_ingest(file: bytes, filename: str, language: str = "") -> dict:
    """上传图/音/视频 → 后端本地识别为文本。

    返回 {kind, text, summary, source, as_of, error, ...}；summary 可直接并入对话。
    """
    return _json("POST", "/api/v1/multimodal/ingest",
                 files={"file": (filename, file)},
                 data={"language": language}, timeout=600)


# ── 市场情绪 ─────────────────────────────────────────

def market_sentiment(market: str = "all", llm: bool = True) -> dict:
    """市场级情绪评分。market=a_share|us|hk|all；llm 控制是否追加 AI 解读。

    all → {"markets": {a_share:…, us:…, hk:…}, as_of, generated_at}；
    单市场 → {market, market_label, score, label, breakdown, llm_interpretation,
              sources, as_of, generated_at}。缺信号维度 score=None 诚实标注。
    """
    return _json("GET", "/api/v1/market/sentiment",
                 params={"market": market, "llm": str(llm).lower()})


# ── 对话（SSE）────────────────────────────────────────

def chat(user_input: str) -> str:
    return _json("POST", "/api/v1/chat", data={"user_input": user_input}).get("reply", "")


def chat_stream(user_input: str) -> requests.Response:
    return _req("POST", "/api/v1/chat/stream", data={"user_input": user_input},
                stream=True, timeout=(10, 300))


def fund_stream(user_input: str) -> requests.Response:
    return _req("POST", "/api/v1/chat/fund/stream", data={"user_input": user_input},
                stream=True, timeout=(10, 300))


# ── 基金 ──────────────────────────────────────────────

def fund_suggest(q: str) -> list[dict]:
    return _json("GET", "/api/v1/watchlist/fund/suggest", params={"q": q}).get("results", [])


def fund_quote(code: str) -> dict:
    return _json("GET", f"/api/v1/watchlist/fund/quote/{code}")


# ── 自选股 ────────────────────────────────────────────

def watchlist() -> list[str]:
    return _json("GET", "/api/v1/watchlist").get("tickers", [])


def watchlist_add(ticker: str) -> bool:
    try:
        _json("POST", "/api/v1/watchlist/add", json={"ticker": ticker})
        return True
    except ApiError:
        return False


def watchlist_remove(ticker: str) -> bool:
    try:
        _json("DELETE", "/api/v1/watchlist/remove", json={"ticker": ticker})
        return True
    except ApiError:
        return False


def watchlist_data() -> list[dict]:
    return _json("GET", "/api/v1/watchlist/data").get("items", [])


def watchlist_news() -> list[dict]:
    return _json("GET", "/api/v1/watchlist/news").get("items", [])


def compare(tickers: list[str]) -> dict:
    return _json("POST", "/api/v1/compare", json={"tickers": tickers})


def fund_watchlist() -> list[dict]:
    return _json("GET", "/api/v1/watchlist/fund").get("items", [])


def fund_watchlist_add(code: str, name: str = "") -> bool:
    try:
        _json("POST", "/api/v1/watchlist/fund/add", json={"fund_code": code, "fund_name": name})
        return True
    except ApiError:
        return False


def fund_watchlist_remove(code: str) -> bool:
    try:
        _json("DELETE", "/api/v1/watchlist/fund/remove", json={"fund_code": code})
        return True
    except ApiError:
        return False


def fund_watchlist_data() -> list[dict]:
    return _json("GET", "/api/v1/watchlist/fund/data").get("items", [])
