"""合规复核队列 —— SQLite 持久化（单用户手动复核）。

表在首次连接时懒建，线程池内写入安全。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

from app.config import get_settings

_local = threading.local()


def _get_connection() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        db_path = get_settings().compliance_db
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS flagged_outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                source TEXT DEFAULT '',
                content_hash TEXT DEFAULT '',
                snippet TEXT DEFAULT '',
                advice_flag INTEGER DEFAULT 0,
                hit_words TEXT DEFAULT '[]',
                reviewed INTEGER DEFAULT 0
            )
        """)
        _local.conn = conn
    return _local.conn


def init_db():
    conn = _get_connection()
    conn.commit()
    return conn


def flag(source: str, content_hash: str, snippet: str = "",
         hit_words: list[str] | None = None, advice_flag: int = 1) -> None:
    conn = _get_connection()
    conn.execute(
        "INSERT INTO flagged_outputs (source, content_hash, snippet, hit_words, advice_flag)"
        " VALUES (?, ?, ?, ?, ?)",
        (source, content_hash, snippet,
         json.dumps(hit_words or [], ensure_ascii=False), advice_flag),
    )
    conn.commit()


def list_flagged(reviewed: int | None = None, limit: int = 100) -> list[dict]:
    conn = _get_connection()
    if reviewed is None:
        rows = conn.execute(
            "SELECT * FROM flagged_outputs ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM flagged_outputs WHERE reviewed = ? ORDER BY id DESC LIMIT ?",
            (reviewed, limit),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["hit_words"] = json.loads(d.get("hit_words") or "[]")
        except Exception:
            d["hit_words"] = []
        out.append(d)
    return out


def mark_reviewed(flag_id: int) -> bool:
    conn = _get_connection()
    cur = conn.execute(
        "UPDATE flagged_outputs SET reviewed = 1 WHERE id = ?", (flag_id,),
    )
    conn.commit()
    return cur.rowcount > 0
