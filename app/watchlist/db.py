"""自选股 SQLite 持久化 —— 迁移自 Violetz(models.py)，路径接统一配置。

表：watchlist_items（股票）、fund_watchlist_items（基金）。
"""
from __future__ import annotations

import os
import sqlite3
import threading

from app.config import get_settings

_local = threading.local()


def _get_connection() -> sqlite3.Connection:
    """线程本地数据库连接（WAL 模式）。"""
    if not hasattr(_local, "conn") or _local.conn is None:
        db_path = get_settings().watchlist_db
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn
    return _local.conn


def init_db():
    """初始化表结构（幂等）。"""
    conn = _get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL UNIQUE,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_watchlist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fund_code TEXT NOT NULL UNIQUE,
            fund_name TEXT DEFAULT '',
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


# ── 股票自选 ──────────────────────────────────────────────────

def get_all_tickers() -> list[str]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT ticker FROM watchlist_items ORDER BY added_at ASC"
    ).fetchall()
    return [row["ticker"] for row in rows]


def add_ticker(ticker: str) -> bool:
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO watchlist_items (ticker) VALUES (?)",
            (ticker.upper(),),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        return False


def remove_ticker(ticker: str) -> bool:
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM watchlist_items WHERE ticker = ?",
            (ticker.upper(),),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        return False


# ── 基金自选 ──────────────────────────────────────────────────

def get_all_fund_tickers() -> list[dict]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT fund_code, fund_name FROM fund_watchlist_items ORDER BY added_at ASC"
    ).fetchall()
    return [{"fund_code": r["fund_code"], "fund_name": r["fund_name"]} for r in rows]


def add_fund_ticker(fund_code: str, fund_name: str = "") -> bool:
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO fund_watchlist_items (fund_code, fund_name) VALUES (?, ?)",
            (fund_code.strip(), fund_name.strip()),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        return False


def remove_fund_ticker(fund_code: str) -> bool:
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM fund_watchlist_items WHERE fund_code = ?",
            (fund_code.strip(),),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        return False
