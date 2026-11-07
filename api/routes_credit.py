# -*- coding: utf-8 -*-
"""固收信用 API —— /api/v1/credit/*（评级、违约概率、行动方案）。

复用 app/agent/fixed_income 的工具实现（真实引擎 → 诚实降级），对外暴露 REST，
供非 Streamlit 客户端 / 后续接入消费。所有响应带 as_of 与来源说明；缺失字段
显式 None，不编造（约束 #7）。

可观测：每请求打点 metrics.inc/record（fi_credit_* 命名空间），
供 /api/v1/fixed-income/status 聚合。
"""
from __future__ import annotations

import json

from fastapi import APIRouter

from app.observability.metrics import metrics
from app.agent import fixed_income as _fi

router = APIRouter(prefix="/api/v1/credit", tags=["fixed-income-credit"])


def _run(name: str, args: dict, metric: str) -> dict:
    import time
    t0 = time.perf_counter()
    try:
        raw = _fi.tool_executor(name, args)
        out = json.loads(raw)
        ok = bool(out.get("ok", out.get("available", True)))
        metrics.inc(f"fi_{metric}_requests", 1)
        if not ok:
            metrics.inc(f"fi_{metric}_errors", 1)
    except Exception as e:  # 上抛 JSON 解析失败视为错误
        out = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        metrics.inc(f"fi_{metric}_errors", 1)
    metrics.record(f"fi_{metric}_latency", time.perf_counter() - t0)
    return out


@router.get("/issuer/{ticker}")
def issuer(ticker: str):
    """单一海外发债主体结构信用风险：Altman Z'' / Ohlson O / Merton（若市场数据可用）。"""
    return _run("credit_issuer", {"ticker": ticker}, "credit")


@router.get("/action/{ticker}")
def action(ticker: str):
    """把信用/利率/政策综合成行动方案（久期/曲线层面 + 风险信号 + 合规队列）。"""
    return _run("action_plan", {"ticker": ticker}, "action")


@router.get("/status")
def status():
    """固收信用侧数据源可用性（配置开关 + 近端调用成败计数）。"""
    from app.config import get_settings
    st = get_settings()
    snap = metrics.snapshot()
    counters = snap["counters"]
    lat = snap["latencies"].get("fi_credit_latency")
    return {
        "sources": {
            "edgar": {"enabled": bool(st.SOURCE_EDGAR)},
            "merton_market": {"enabled": bool(st.SOURCE_YFINANCE)},
        },
        "credit_calls": counters.get("fi_credit_requests", 0),
        "credit_errors": counters.get("fi_credit_errors", 0),
        "action_calls": counters.get("fi_action_requests", 0),
        "action_errors": counters.get("fi_action_errors", 0),
        "credit_avg_seconds": lat["avg"] if lat else None,
        "note": "配置开关为真实读取；成败计数为进程内累计（重启清零）。"
                "Merton 为市场数据(市值/波动)驱动，缺失时如实标不可用。",
    }
