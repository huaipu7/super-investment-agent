"""
统一缓存 —— SQLite 实现，替换原 Agent-for-Investment 的 Redis 缓存（Redis 未运行）。

接口保持与 StockCache 一致（get_dict / set_dict / get_dataframe / set_dataframe / exists），
下游代码（行情、财务、基金、情绪）无需改动即可接入。

TTL 常量沿用原项目 utils/cache.py 的 CACHE_TTL。
"""
from __future__ import annotations

import io
import json
import sqlite3
import threading
import time
from pathlib import Path

import pandas as pd

from app.config import get_settings

# 缓存 TTL 常量（秒）—— 沿用原 Agent-for-Investment 定义
CACHE_TTL = {
    "quote": 300,          # 5 分钟 — 实时报价变化快
    "history_short": 300,  # 5 分钟 — 1d/5d 短期历史
    "history_long": 900,   # 15 分钟 — 1mo+ 中长期历史
    "financials": 3600,    # 1 小时 — 财务概览数据变化慢
    # Phase 4：三表分级 TTL（财务区分年报/季报）
    "financials_annual": 86400,     # 年报 24 小时 — 年度报表极少变动
    "financials_quarterly": 21600,  # 季报 6 小时 — 季度累计值随新披露期变动较快
    "earnings_calendar": 86400,     # 美股财报/分红日历 24 小时（日更一次）
    "info": 3600,          # 1 小时 — 公司信息极少变化
    "search": 86400,       # 24 小时 — 搜索结果稳定
    "sina_quote": 10,      # Sina 行情缓存 10 秒（沿用 Violet Z）
    "hist": 30,            # 日K线缓存 30 秒（沿用 Violet Z）
    # Phase 9：新闻 / 公告 / 股东大会
    "news_em": 900,        # 东财个股新闻 15 分钟（新闻时效性较强）
    "news_yf": 1800,       # yfinance 美股新闻 30 分钟
    "briefings": 3600,     # 业绩说明会/投资者关系公告 1 小时（公告变更慢）
    "meetings": 86400,     # 股东大会（stock_gddh_em 全市场抓取较重，日更一次）
    "sentiment": 600,      # 市场情绪 10 分钟（情绪变化慢，避免频繁打上游数据源）
    # Phase 1：固收零 key 源
    "treasury_curve": 3600,    # 美元国库每日收益率曲线 1 小时（日更，足够）
    "edgar_facts": 86400,      # SEC companyfacts 24 小时（官方日更一次）
    "fomc_statement": 86400,   # FOMC 声明 24 小时
}


class SqliteCache:
    """基于 SQLite 的统一缓存，线程安全，Redis 不可用时替代方案。"""

    def __init__(self, db_path: str | Path | None = None, default_ttl: int = 300):
        s = get_settings()
        self.db_path = Path(db_path) if db_path else Path(s.cache_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_ttl = default_ttl
        self._local = threading.local()
        self._write_lock = threading.RLock()

    # ── 连接管理（线程本地连接）─────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=3000")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )"""
            )
            self._local.conn = conn
        return self._local.conn

    @staticmethod
    def _key(*parts) -> str:
        return "stock:" + ":".join(str(p) for p in parts)

    # ── 基础读写 ────────────────────────────────────────────

    def get(self, key: str) -> str | None:
        try:
            cur = self._conn().execute(
                "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
            )
            row = cur.fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        value, expires_at = row
        if expires_at is not None and time.time() > expires_at:
            self.delete(key)
            return None
        return value

    def set(self, key: str, value: str, ttl: int | None = None):
        expires_at = time.time() + (ttl if ttl is not None else self.default_ttl)
        with self._write_lock:
            try:
                self._conn().execute(
                    "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                    (key, value, expires_at),
                )
                self._conn().commit()
            except sqlite3.Error:
                pass

    def delete(self, key: str):
        with self._write_lock:
            try:
                self._conn().execute("DELETE FROM cache WHERE key = ?", (key,))
                self._conn().commit()
            except sqlite3.Error:
                pass

    def exists(self, *key_parts: str) -> bool:
        return self.get(self._key(*key_parts)) is not None

    def clear_expired(self):
        with self._write_lock:
            try:
                self._conn().execute("DELETE FROM cache WHERE expires_at < ?", (time.time(),))
                self._conn().commit()
            except sqlite3.Error:
                pass

    # ── 类型化读写（与原 StockCache 接口一致）──────────────

    def get_dict(self, *key_parts: str) -> dict | None:
        raw = self.get(self._key(*key_parts))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def set_dict(self, data: dict, ttl: int | None = None, *key_parts: str):
        try:
            raw = json.dumps(data, ensure_ascii=False, default=str)
            self.set(self._key(*key_parts), raw, ttl)
        except (TypeError, ValueError):
            pass

    def get_dataframe(self, *key_parts: str) -> pd.DataFrame | None:
        raw = self.get(self._key(*key_parts))
        if raw is None:
            return None
        try:
            # pandas 3.x 把 str 一律当文件路径；用 StringIO 强制按 JSON 解析
            return pd.read_json(io.StringIO(raw), orient="split")
        except (ValueError, TypeError):
            return None

    def set_dataframe(self, df: pd.DataFrame, ttl: int | None = None, *key_parts: str):
        try:
            raw = df.to_json(orient="split", force_ascii=False)
            self.set(self._key(*key_parts), raw, ttl)
        except (TypeError, ValueError):
            pass

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None


# ── 全局单例 ──────────────────────────────────────────────────

_cache: SqliteCache | None = None


def get_cache() -> SqliteCache:
    global _cache
    if _cache is None:
        _cache = SqliteCache()
    return _cache


def init_cache() -> SqliteCache:
    """应用启动时初始化缓存（api/main.py 调用）。"""
    return get_cache()


def close_cache():
    """应用关闭时释放缓存连接。"""
    global _cache
    if _cache:
        _cache.close()
        _cache = None
