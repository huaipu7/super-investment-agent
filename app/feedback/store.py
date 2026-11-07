"""反馈存储 —— SQLite data/feedback.db（只 INSERT，单用户，无鉴权）。

表 feedback(id, ts, request_id, msg_id, label, scope, fact_id,
            options_json, note, model, prompt_version, git_sha)

两条不可让步的纪律：
  1 request_id / msg_id 是反馈与 audit.db、memory.db 关联的唯一钥匙。
    缺了它们，反馈就是孤儿数据 —— 所以接口层强制非空。
  2 只提供写入与只读查询，没有 UPDATE/DELETE。👎 只进待审队列
     data/flywheel/inbox.jsonl，**人工审阅后**才能 promote 成回归用例；
     自动进回归集会让一次误操作把错误答案固化成金标准。

版本戳由服务端填写（model / prompt_version / git_sha），不接受客户端自称——
客户端传什么版本都不能作为归因依据。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from app.config import get_settings
from app.observability.version import stamp

LABELS = ("up", "down")
SCOPES = ("message", "fact", "report")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    request_id TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    label TEXT NOT NULL,
    scope TEXT DEFAULT 'message',
    fact_id TEXT,
    options_json TEXT DEFAULT '[]',
    note TEXT,
    model TEXT,
    prompt_version TEXT,
    git_sha TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_msg ON feedback(msg_id);
CREATE INDEX IF NOT EXISTS idx_feedback_req ON feedback(request_id);
"""

_local = threading.local()


def _connection() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        path = get_settings().feedback_db
        os.makedirs(os.path.dirname(str(path)), exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.executescript(_SCHEMA)
        _local.conn = conn
    return conn


def init_db() -> None:
    """启动时建表（幂等，供 lifespan 调用）。"""
    _connection().commit()


def _inbox_path() -> Path:
    return get_settings().flywheel_dir / "inbox.jsonl"


def _append_inbox(record: dict) -> Path:
    """👎 记录进待审队列（append-only jsonl，status 恒为 pending_review）。"""
    path = _inbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def add(request_id: str, msg_id: str, label: str, scope: str = "message",
        fact_id: str | None = None, options: list[str] | None = None,
        note: str | None = None) -> dict:
    """落一条反馈，返回落库后的记录。非法 label/scope 抛 ValueError。"""
    rid = (request_id or "").strip()
    mid = (msg_id or "").strip()
    if not rid or not mid:
        raise ValueError("request_id / msg_id 不能为空（否则反馈是孤儿数据）")
    if label not in LABELS:
        raise ValueError(f"label 必须是 {LABELS}，收到 {label!r}")
    if scope not in SCOPES:
        raise ValueError(f"scope 必须是 {SCOPES}，收到 {scope!r}")

    ver = stamp()
    row = {
        "ts": time.time(),
        "request_id": rid,
        "msg_id": mid,
        "label": label,
        "scope": scope,
        "fact_id": fact_id,
        "options_json": json.dumps(list(options or []), ensure_ascii=False),
        "note": (note or "").strip() or None,
        **ver,
    }
    conn = _connection()
    cur = conn.execute(
        "INSERT INTO feedback (ts, request_id, msg_id, label, scope, fact_id,"
        " options_json, note, model, prompt_version, git_sha)"
        " VALUES (:ts,:request_id,:msg_id,:label,:scope,:fact_id,"
        " :options_json,:note,:model,:prompt_version,:git_sha)",
        row,
    )
    conn.commit()
    row["id"] = int(cur.lastrowid)

    if label == "down":
        # 只入待审队列，不碰任何回归集
        _append_inbox({**row, "status": "pending_review", "promoted": False})
    return _shape(row)


def _shape(row: dict | sqlite3.Row) -> dict:
    d = dict(row)
    raw = d.pop("options_json", "[]")
    try:
        d["options"] = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        d["options"] = []
    return d


def recent(limit: int = 50) -> list[dict]:
    rows = _connection().execute(
        "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (int(limit),),
    ).fetchall()
    return [_shape(r) for r in rows]


def stats() -> dict:
    """反馈汇总：总数 / 👍 / 👎 / 覆盖到的消息数。无反馈时计数为 0。"""
    row = _connection().execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN label='up' THEN 1 ELSE 0 END) AS up,"
        " SUM(CASE WHEN label='down' THEN 1 ELSE 0 END) AS down,"
        " COUNT(DISTINCT msg_id) AS msgs FROM feedback",
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "up": int(row["up"] or 0),
        "down": int(row["down"] or 0),
        "distinct_msgs": int(row["msgs"] or 0),
    }
