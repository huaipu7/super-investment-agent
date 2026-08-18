"""Phase D2 验证：可观测（metrics 计数器/时延 + 结构化日志 + /api/v1/metrics）。

运行: D:\\python\\python.exe scripts\\verify_observability.py
"""
from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def main():
    from app.observability.metrics import metrics

    # ── (a) metrics 计数器 + 时延统计 ──
    metrics.reset()
    metrics.inc("http_requests", 3)
    metrics.inc("llm_calls")
    metrics.record("test_latency", 0.25)
    metrics.record("test_latency", 0.75)
    snap = metrics.snapshot()
    check("(a) counters 累计", snap["counters"].get("http_requests") == 3
          and snap["counters"].get("llm_calls") == 1, str(snap["counters"]))
    lat = snap["latencies"].get("test_latency", {})
    check("(a) latency avg/count/total",
          lat.get("count") == 2 and abs(lat.get("avg", 0) - 0.5) < 1e-9
          and abs(lat.get("total", 0) - 1.0) < 1e-9, str(lat))
    check("(a) snapshot 结构", set(snap) == {"counters", "latencies"})

    # ── (b) 结构化日志：extra → key=value ──
    from app.utils.logger import _RequestIdFormatter, setup_logger

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(_RequestIdFormatter("%(levelname)s %(message)s"))
    lg = setup_logger("test_obs_structured")
    lg.addHandler(h)
    lg.info("分析完成", extra={"ticker": "600519.SS", "ok": True, "items": [1, 2]})
    out = buf.getvalue()
    check("(b) 结构化 key=value",
          "ticker=600519.SS" in out and "ok=True" in out and "items=[1, 2]" in out,
          out.strip())
    lg.removeHandler(h)

    # ── (c) 中间件计数 + /api/v1/metrics 路由（真实 App）──
    from fastapi.testclient import TestClient
    from api.main import app

    metrics.reset()
    with TestClient(app) as client:
        r = client.get("/api/v1/health")
        check("(c) health 200", r.status_code == 200)
        m = client.get("/api/v1/metrics")
        check("(c) /metrics 路由可用", m.status_code == 200)
        body = m.json()["metrics"]
        check("(c) http_requests 计数", body["counters"].get("http_requests", 0) >= 1,
              str(body["counters"]))
        hlat = body["latencies"].get("http_latency", {})
        check("(c) http_latency 采样",
              hlat.get("count", 0) >= 1 and "avg" in hlat and "total" in hlat,
              str(hlat))
        check("(c) 中间件下发 X-Request-Id", bool(r.headers.get("X-Request-Id")))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
