"""Phase A1 验证：审计日志 + request_id。

运行: D:\\python\\python.exe scripts\\verify_audit.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

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
    tmp = Path(tempfile.mkdtemp(prefix="verify_audit_"))
    audit_db = tmp / "audit.db"

    class _TmpSettings:
        audit_db = tmp / "audit.db"

    # ── 1. 审计写入 / 查询 / request_id / detail 往返 ──
    with mock.patch("app.audit.log.get_settings", lambda: _TmpSettings()):
        from app.audit.log import audit, content_hash, init_db, query

        init_db()
        audit("chat.request", {"input": "查询茅台股价", "msg_id": "abc123"},
              request_id="req-001")
        audit("chat.reply", {"msg_id": "abc123", "content_hash": "deadbeef"},
              request_id="req-001")
        audit("analysis.start", {"ticker": "AAPL"})  # 无 request_id（无请求上下文）

        rows = query()
        check("写入 ≥2 条审计", len(rows) >= 2, f"实际 {len(rows)} 条")

        r = next(x for x in rows if x["event_type"] == "chat.request")
        check("request_id 落库", r["request_id"] == "req-001", r["request_id"])
        check("detail JSON 往返", r["detail"].get("input") == "查询茅台股价")
        check("actor 默认 local", r["actor"] == "local")

        r2 = next(x for x in rows if x["event_type"] == "analysis.start")
        check("无 request_id 时为 ''", r2["request_id"] == "", repr(r2["request_id"]))

        by_type = query(event_type="chat.reply")
        check("按 event_type 过滤", len(by_type) == 1 and by_type[0]["event_type"] == "chat.reply")

        check("content_hash 确定性", content_hash("hello") == content_hash("hello")
              and content_hash("hello") != content_hash("hellp"))

    # ── 2. request_id 中间件（真实 App，TestClient）──
    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:
        r = client.get("/api/v1/health")
        rid = r.headers.get("X-Request-Id")
        check("中间件下发 X-Request-Id", bool(rid), repr(rid))

        ar = client.get("/api/v1/audit?limit=5")
        check("/api/v1/audit 路由可用", ar.status_code == 200 and "rows" in ar.json())
        check("/api/v1/audit 返回 list", isinstance(ar.json().get("rows"), list))

    # ── 3. DB 文件确已创建 ──
    check("audit.db 文件存在", audit_db.exists())

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
