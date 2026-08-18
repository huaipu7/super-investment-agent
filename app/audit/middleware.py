"""request_id 中间件 —— contextvar 贯穿单请求，注入日志与审计。

SSE 流式响应体在 dispatch 返回后才被消费，因此流式生成器内取不到
contextvar；需要在端点入口先 `get_request_id()` 捕获并向下透传。
"""
from __future__ import annotations

import contextvars
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.observability.metrics import metrics

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


def get_request_id() -> str:
    return _request_id.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        token = _request_id.set(rid)
        metrics.inc("http_requests")
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            metrics.inc("http_errors")
            raise
        finally:
            _request_id.reset(token)
            metrics.record("http_latency", time.perf_counter() - t0)
        response.headers["X-Request-Id"] = rid
        return response
