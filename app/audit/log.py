"""审计日志 —— SQLite 只追加存储（单用户，actor=local）。

只提供 INSERT 与查询，无 UPDATE/DELETE 路径。表在首次连接时懒建，
保证任意线程（含线程池内的 Agent 步骤）首次写入都能自举建表。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from typing import Any

from app.config import get_settings

_local = threading.local()


def _get_connection() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        db_path = get_settings().audit_db
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                event_type TEXT NOT NULL,
                actor TEXT DEFAULT 'local',
                request_id TEXT DEFAULT '',
                detail_json TEXT DEFAULT '{}'
            )
        """)
        _local.conn = conn
    return _local.conn


def init_db():
    """初始化审计表（幂等，供 lifespan 显式调用）。"""
    conn = _get_connection()
    conn.commit()
    return conn


def audit(event_type: str, detail: dict | None = None, actor: str = "local",
          request_id: str | None = None) -> None:
    """写一条审计记录。只追加，不提供删除/更新路径。"""
    rid = request_id
    if not rid:
        try:
            from app.audit.middleware import get_request_id
            rid = get_request_id()
        except Exception:
            rid = ""
    conn = _get_connection()
    conn.execute(
        "INSERT INTO audit_log (event_type, actor, request_id, detail_json)"
        " VALUES (?, ?, ?, ?)",
        (event_type, actor, rid or "",
         json.dumps(detail or {}, ensure_ascii=False, default=str)),
    )
    conn.commit()


def query(event_type: str | None = None, limit: int = 100) -> list[dict]:
    conn = _get_connection()
    if event_type:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE event_type = ? ORDER BY id DESC LIMIT ?",
            (event_type, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
        except Exception:
            d["detail"] = {}
        out.append(d)
    return out


def content_hash(text: str) -> str:
    """内容短哈希（用于输出溯源/去重）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
