"""长时记忆 —— SQLite data/memory.db（单用户，无鉴权，actor=local）。

表:
  conversations(id, role, content, intent, tickers, created_at)  只 INSERT
  user_profiles(key, value, updated_at)                         UPSERT
  sessions(id, summary, updated_at)                             滚动摘要（只 INSERT）

线程安全：thread-local 连接 + WAL + 懒建表（复用 audit/log.py 模式）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from app.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    intent TEXT,
    tickers TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS user_profiles (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

_local = threading.local()


def _connection() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(get_settings().memory_db), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.executescript(_SCHEMA)
        _local.conn = conn
    return conn


def init_db() -> None:
    """应用启动时初始化（api/main.py lifespan 调用）。"""
    _connection().commit()


# ── 对话历史（只 INSERT）─────────────────────────────────────

def add_message(role: str, content: str, intent: str | None = None,
                tickers: list[str] | None = None) -> int:
    """追加一条消息，返回自增 id。"""
    tickers_json = json.dumps(list(tickers), ensure_ascii=False) if tickers else None
    conn = _connection()
    cur = conn.execute(
        "INSERT INTO conversations(role, content, intent, tickers, created_at) "
        "VALUES (?,?,?,?,?)",
        (role, content, intent, tickers_json, time.time()),
    )
    conn.commit()
    return int(cur.lastrowid)


def tag_last_user_message(intent: str | None, tickers: list[str] | None = None) -> None:
    """给最近一条 user 消息补记意图/标的（意图在消息落库之后才解析出）。"""
    tickers_json = json.dumps(list(tickers), ensure_ascii=False) if tickers else None
    conn = _connection()
    conn.execute(
        "UPDATE conversations SET intent=?, tickers=? "
        "WHERE id=(SELECT MAX(id) FROM conversations WHERE role='user')",
        (intent, tickers_json),
    )
    conn.commit()


def recent_messages(limit: int = 50) -> list[dict]:
    """按时间正序返回最近 limit 条，形如 [{id, role, text, intent, tickers}]。"""
    rows = _connection().execute(
        "SELECT id, role, content, intent, tickers FROM conversations "
        "ORDER BY id DESC LIMIT ?", (limit,),
    ).fetchall()
    out = []
    for r in reversed(rows):
        out.append({
            "id": f"msg-{r[0]}",
            "role": r[1],
            "text": r[2],
            "intent": r[3],
            "tickers": json.loads(r[4]) if r[4] else None,
        })
    return out


def count_messages() -> int:
    row = _connection().execute("SELECT COUNT(*) FROM conversations").fetchone()
    return int(row[0]) if row else 0


def clear_messages() -> None:
    """清空对话历史与会话摘要（用户主动 /clear）。"""
    conn = _connection()
    conn.execute("DELETE FROM conversations")
    conn.execute("DELETE FROM sessions")
    conn.commit()


# ── 用户画像（key-value）────────────────────────────────────

def set_profile(key: str, value: str) -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO user_profiles(key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, time.time()),
    )
    conn.commit()


def get_profile(key: str) -> str | None:
    row = _connection().execute(
        "SELECT value FROM user_profiles WHERE key=?", (key,),
    ).fetchone()
    return row[0] if row else None


def get_all_profiles() -> dict:
    rows = _connection().execute("SELECT key, value FROM user_profiles").fetchall()
    return {k: v for k, v in rows}


# ── 会话滚动摘要（只 INSERT）────────────────────────────────

def update_session(summary: str) -> None:
    conn = _connection()
    conn.execute("INSERT INTO sessions(summary, updated_at) VALUES (?,?)",
                 (summary, time.time()))
    conn.commit()


def latest_session_summary() -> str | None:
    row = _connection().execute(
        "SELECT summary FROM sessions ORDER BY id DESC LIMIT 1",
    ).fetchone()
    return row[0] if row else None
