"""可观测性指标 —— 线程安全进程内计数器与时延采样。

计数器: http_requests / http_errors / chat_requests / analysis_requests /
        llm_calls / llm_tokens_in / llm_tokens_out / llm_errors / errors ...
时延:   名称 → {total, count, avg, min, max}（秒）

GET /api/v1/metrics 返回 metrics.snapshot() 快照。进程内存态，重启清零。
"""
from __future__ import annotations

import threading
from collections import Counter


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._latency: dict[str, list[float]] = {}

    def inc(self, name: str, n: int = 1) -> None:
        """计数器自增（线程安全）。"""
        with self._lock:
            self._counters[name] += n

    def record(self, name: str, seconds: float) -> None:
        """记录一次时延采样（秒）。"""
        with self._lock:
            self._latency.setdefault(name, []).append(seconds)

    def snapshot(self) -> dict:
        """当前快照（计数器 + 时延统计），供 /api/v1/metrics 输出。"""
        with self._lock:
            return {
                "counters": dict(self._counters),
                "latencies": {
                    k: {
                        "total": round(sum(v), 6),
                        "count": len(v),
                        "avg": round(sum(v) / len(v), 6) if v else 0.0,
                        "min": round(min(v), 6) if v else None,
                        "max": round(max(v), 6) if v else None,
                    }
                    for k, v in self._latency.items()
                },
            }

    def reset(self) -> None:
        """清零（测试/重置用）。"""
        with self._lock:
            self._counters.clear()
            self._latency.clear()

    def guardrails(self) -> dict:
        """五个护栏指标（质量 3 + 飞轮健康度 2）。口径见 guardrails.py。"""
        from app.observability.guardrails import snapshot

        with self._lock:
            counters = dict(self._counters)
        return snapshot(counters)

    def data_sources(self) -> dict:
        """按数据源聚合健康指标（Phase 4）。

        数据源由 router._ds_instrument 打点：`ds_<src>_<name>` 请求计数、
        `ds_<src>_<name>_errors` 错误计数、`ds_<src>_latency` 聚合时延采样。
        """
        with self._lock:
            out: dict[str, dict] = {}
            for src in ("akshare", "yfinance", "sina"):
                req = err = 0
                for k, v in self._counters.items():
                    if k.startswith(f"ds_{src}_"):
                        if k.endswith("_errors"):
                            err += v
                        else:
                            req += v
                item = {"requests": req, "errors": err}
                lat = self._latency.get(f"ds_{src}_latency")
                if lat:
                    item["avg_seconds"] = round(sum(lat) / len(lat), 4)
                out[src] = item
            return out


metrics = Metrics()
