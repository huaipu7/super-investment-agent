"""超级投研 Agent —— FastAPI 统一后端入口。

整合：
  /api/v1/stock/*      行情 / 财务 / 评分（统一数据层 + 专家评分）
  /api/v1/analysis/*   深度研究分析（后台任务）
  /api/v1/chat/*       对话 Agent（股票/基金，SSE 流式）
  /api/v1/report/*     报告列表 / 下载 / 导出
  /api/v1/watchlist/*  自选股（股票 + 基金）
  /api/v1/multimodal/* 多模态接入（图 / 音 / 视频 → 文本预处理）
  /api/v1/market/*     市场情绪（A股 / 美股 / 港股）

CORS 放行 Streamlit 前端（8501）。
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.audit.log import init_db as audit_init_db
from app.audit.middleware import RequestIdMiddleware
from app.compliance import queue as compliance_queue
from app.config import get_settings
from app.data.cache import init_cache, close_cache
from app.feedback import store as feedback_store
from app.memory import db as memory_db
from app.thesis import db as thesis_db
from app.watchlist import db as watchlist_db

from api.routes_analysis import router as analysis_router
from api.routes_audit import router as audit_router
from api.routes_chat import router as chat_router
from api.routes_compliance import router as compliance_router
from api.routes_credit import router as credit_router
from api.routes_feedback import router as feedback_router
from api.routes_investigation import router as investigation_router
from api.routes_rates import router as rates_router
from api.routes_compare import router as compare_router
from api.routes_metrics import router as metrics_router
from api.routes_market import router as market_router
from api.routes_multimodal import router as multimodal_router
from api.routes_profile import router as profile_router
from api.routes_report import router as report_router
from api.routes_stock import router as stock_router
from api.routes_thesis import router as thesis_router
from api.routes_search import router as search_router
from api.routes_watchlist import router as watchlist_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_cache()
    watchlist_db.init_db()
    audit_init_db()
    compliance_queue.init_db()
    memory_db.init_db()
    feedback_store.init_db()
    thesis_db.init_db()
    # 用库中历史回填内存会话列表：对话 prompt 用的是这份内存列表，
    # 不回填则进程重启后上下文失忆（库里的 conversations 仍在）。
    # list.extend/list.clear 为基类方法，绕过 _PersistentHistory.append 的落库副作用。
    from api.routes_chat import chat_history
    chat_history.clear()
    list.extend(chat_history, memory_db.recent_messages(50))
    yield
    close_cache()


app = FastAPI(
    title="超级投研 Agent",
    description="统一投资研究系统：行情 / 深度研究 / 对话 Agent / 报告 / 自选股",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS：放行 Streamlit 看板（默认 8501）与本地开发端口
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8501",
        "http://127.0.0.1:8501",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# request_id 中间件（最外层，先于 CORS 执行；为每个请求生成 X-Request-Id）
app.add_middleware(RequestIdMiddleware)

app.include_router(stock_router)
app.include_router(analysis_router)
app.include_router(chat_router)
app.include_router(report_router)
app.include_router(watchlist_router)
app.include_router(compare_router)
app.include_router(audit_router)
app.include_router(compliance_router)
app.include_router(profile_router)
app.include_router(metrics_router)
app.include_router(market_router)
app.include_router(multimodal_router)
app.include_router(credit_router)
app.include_router(rates_router)
app.include_router(investigation_router)
app.include_router(feedback_router)
app.include_router(thesis_router)
app.include_router(search_router)


@app.get("/api/v1/fixed-income/status")
def fixed_income_status():
    """固收引擎可观测总览：数据源开关 + 近端调用成败/时延（进程内，重启清零）。"""
    from app.config import get_settings
    from app.observability.metrics import metrics

    st = get_settings()
    snap = metrics.snapshot()
    c = snap["counters"]
    lat = snap["latencies"]
    src = {"treasury": bool(st.SOURCE_TREASURY),
           "edgar": bool(st.SOURCE_EDGAR),
           "fomc": bool(st.SOURCE_FOMC),
           "merton_market": bool(st.SOURCE_YFINANCE)}

    def _agg(prefix: str) -> dict:
        req = err = 0
        for k, v in c.items():
            if k.startswith(f"fi_{prefix}_") and k.endswith("_requests"):
                req += v
            if k.startswith(f"fi_{prefix}_") and k.endswith("_errors"):
                err += v
        item = {"requests": req, "errors": err}
        l = lat.get(f"fi_{prefix}_latency")
        if l:
            item["avg_seconds"] = round(sum(l) / len(l), 4)
        return item

    # 兼容 credit / action 前缀（routes_credit 用 fi_credit_/fi_action_）
    return {
        "sources_enabled": src,
        "services": {
            "credit": _agg("credit"),
            "action": _agg("action"),
            "rates": _agg("rates"),
            "montecarlo": _agg("mc"),
        },
        "note": "sources_enabled 为 .env 如实读取；services 为进程内累计指标（重启清零）。"
                "某源开启≠该源可用；具体可用性见各端点返回的 ok/reason。",
    }


@app.get("/api/v1/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "version": "1.0.0",
        "llm_ready": settings.check_llm_ready(),
        "model": settings.LLM_MODEL,
    }


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host="127.0.0.1", port=settings.SERVER_PORT)
