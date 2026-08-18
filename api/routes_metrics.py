"""可观测性指标 API —— GET /api/v1/metrics（进程内快照）。

返回 metrics.snapshot()：counters（请求/LLM/token/错误计数）与
latencies（各阶段总耗时/次数/均值）。单用户无鉴权。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.observability.metrics import metrics

router = APIRouter(prefix="/api/v1", tags=["metrics"])


@router.get("/metrics")
def get_metrics() -> dict:
    snap = metrics.snapshot()
    snap["data_sources"] = metrics.data_sources()
    return {"metrics": snap}
