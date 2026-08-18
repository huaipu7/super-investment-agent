"""深度研报管线结果持久化缓存（SQLite）。

解决痛点：api/routes_analysis._tasks 是内存 dict，进程重启即丢，同 ticker 二次
查看会重跑整条 LLM 管线（约 9 分钟）。
方案：结果落 data/analysis_cache.db，按 (ticker, datekey, paramkey) 分层；
`GET /analysis/result/{ticker}` 无任务时命中缓存直接返回。`?force=1` 强制重跑
由前端走 POST /analysis/run/{ticker} 触发（run 恒重跑并覆盖缓存）。

诚实标注：缓存条目记录 created_at（生成时刻），结果保留原始 pipeline_time，
不伪造新鲜度。失败时静默降级（返回 None/False），不阻断主流程。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from app.config import settings
from app.utils.logger import logger

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS analysis_results (
    ticker     TEXT NOT NULL,
    datekey    TEXT NOT NULL,
    paramkey   TEXT NOT NULL,
    created_at REAL,
    result_json TEXT,
    PRIMARY KEY (ticker, datekey, paramkey)
)
"""


def _conn() -> sqlite3.Connection:
    db = Path(settings.analysis_cache_db)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=30)
    conn.execute(_TABLE_SQL)
    conn.commit()
    return conn


def _datekey(as_of=None) -> str:
    if as_of:
        return str(as_of)[:10]
    return time.strftime("%Y-%m-%d")


def get_cached_result(ticker: str, as_of=None, paramkey: str = "annual") -> dict | None:
    """读取缓存结果；无缓存/异常返回 None。"""
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT result_json, created_at FROM analysis_results "
            "WHERE ticker=? AND datekey=? AND paramkey=?",
            (ticker.upper(), _datekey(as_of), paramkey),
        ).fetchone()
        conn.close()
        if not row:
            return None
        result = json.loads(row[0])
        if isinstance(result, dict):
            result.setdefault("cache", {"hit": True, "created_at": row[1],
                                        "datekey": _datekey(as_of)})
        return result
    except Exception as e:
        logger.warning("读取分析缓存失败: %s", e)
        return None


def set_cached_result(ticker: str, result: dict, paramkey: str = "annual") -> bool:
    """写入缓存（覆盖同日同参）。成功返回 True，失败 False。"""
    try:
        conn = _conn()
        conn.execute(
            "INSERT OR REPLACE INTO analysis_results "
            "(ticker, datekey, paramkey, created_at, result_json) VALUES (?,?,?,?,?)",
            (ticker.upper(), _datekey(), paramkey, time.time(),
             json.dumps(result, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.warning("写入分析缓存失败: %s", e)
        return False


def cached_status(ticker: str, as_of=None) -> bool:
    """是否存在可用缓存（供 status 接口返回 completed）。"""
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT 1 FROM analysis_results WHERE ticker=? AND datekey=? AND paramkey=?",
            (ticker.upper(), _datekey(as_of), "annual"),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False
