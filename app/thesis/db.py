"""论点台账持久化 —— 一条论点 = 结论 + 假设 + 证伪条件 + 证据 + 复盘留痕。

与 watchlist 同一套模式（线程本地连接 + WAL）。这里只做存取与字段校验；
证伪条件的判定在 checks.py，是纯函数，不碰数据库。

设计意图：投研里最容易失效的不是数字，是「当初为什么买」。把论点和它的
证伪条件写成结构化数据，才能在事实变了的时候被机器指出来，而不是靠记性。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from typing import Any

from app.config import get_settings

_local = threading.local()

STANCES = ("bullish", "bearish", "neutral")
STATUSES = ("active", "confirmed", "falsified", "abandoned")
DIRECTIONS = ("supports", "contradicts")
VERDICTS = ("holds", "weakens", "broken")
EVIDENCE_KINDS = ("fact", "snippet", "artifact", "doc")

# 允许 update_thesis 改的列；其余（id/created_at）不可改
_UPDATABLE = {
    "ticker", "statement", "stance", "horizon", "key_assumptions",
    "falsifiers", "status", "conviction", "artifact_ref",
}


def _get_connection() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        db_path = get_settings().thesis_db
        os.makedirs(os.path.dirname(str(db_path)), exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn
    return _local.conn


def init_db():
    """建表（幂等）。"""
    conn = _get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thesis (
            id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL DEFAULT '',
            statement TEXT NOT NULL,
            stance TEXT NOT NULL DEFAULT 'neutral',
            horizon TEXT NOT NULL DEFAULT '',
            key_assumptions TEXT NOT NULL DEFAULT '[]',
            falsifiers TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            conviction INTEGER NOT NULL DEFAULT 3,
            artifact_ref TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thesis_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thesis_id TEXT NOT NULL,
            ref TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'fact',
            direction TEXT NOT NULL DEFAULT 'supports',
            note TEXT NOT NULL DEFAULT '',
            added_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thesis_review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thesis_id TEXT NOT NULL,
            verdict TEXT NOT NULL,
            snapshot TEXT NOT NULL DEFAULT '{}',
            note TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_thesis_ticker ON thesis(ticker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ev_thesis ON thesis_evidence(thesis_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rv_thesis ON thesis_review(thesis_id)")
    conn.commit()
    return conn


# ── 校验 ──────────────────────────────────────────────────────

def _check_enum(name: str, value: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise ValueError(f"{name} 必须是 {allowed} 之一，收到 {value!r}")
    return value


def _normalize_falsifiers(items: Any) -> str:
    """校验证伪条件并序列化。

    每条必须带 metric/op/value：metric 是台账里的字段路径（如
    valuation_analysis.fair_value），checks.py 直接拿它去 Ledger.find()。
    允许额外字段（note/as_of），不认识的键原样保留，不静默丢弃。
    """
    if items is None:
        return "[]"
    if not isinstance(items, list):
        raise ValueError("falsifiers 必须是列表")
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError(f"falsifiers[{i}] 必须是对象")
        metric = str(it.get("metric") or "").strip()
        op = str(it.get("op") or "").strip()
        if not metric:
            raise ValueError(f"falsifiers[{i}] 缺 metric")
        if op not in ("<", "<=", ">", ">=", "==", "!="):
            raise ValueError(f"falsifiers[{i}].op 不支持：{op!r}")
        try:
            value = float(it.get("value"))
        except (TypeError, ValueError) as e:
            raise ValueError(f"falsifiers[{i}].value 必须是数字") from e
        entry = dict(it)
        entry.update({"metric": metric, "op": op, "value": value})
        out.append(entry)
    return json.dumps(out, ensure_ascii=False)


def _normalize_assumptions(items: Any) -> str:
    if items is None:
        return "[]"
    if not isinstance(items, list):
        raise ValueError("key_assumptions 必须是列表")
    return json.dumps([str(x) for x in items], ensure_ascii=False)


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []


def _row_to_thesis(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["key_assumptions"] = _loads(d.get("key_assumptions"))
    d["falsifiers"] = _loads(d.get("falsifiers"))
    return d


# ── 论点 ──────────────────────────────────────────────────────

def create_thesis(*, statement: str, ticker: str = "", stance: str = "neutral",
                  horizon: str = "", key_assumptions: Any = None,
                  falsifiers: Any = None, conviction: int = 3,
                  artifact_ref: str = "") -> str:
    if not str(statement or "").strip():
        raise ValueError("statement 不能为空")
    _check_enum("stance", stance, STANCES)
    conviction = int(conviction)
    if not 1 <= conviction <= 5:
        raise ValueError("conviction 必须在 1-5")
    conn = init_db()
    tid = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO thesis
           (id, ticker, statement, stance, horizon, key_assumptions,
            falsifiers, status, conviction, artifact_ref)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (tid, (ticker or "").strip().upper(), statement.strip(), stance,
         horizon or "", _normalize_assumptions(key_assumptions),
         _normalize_falsifiers(falsifiers), "active", conviction,
         artifact_ref or ""),
    )
    conn.commit()
    return tid


def get_thesis(thesis_id: str) -> dict | None:
    conn = init_db()
    row = conn.execute("SELECT * FROM thesis WHERE id = ?", (thesis_id,)).fetchone()
    return _row_to_thesis(row) if row else None


def list_theses(*, ticker: str | None = None, status: str | None = None,
                limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM thesis WHERE 1=1"
    args: list[Any] = []
    if ticker:
        sql += " AND ticker = ?"
        args.append(ticker.strip().upper())
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
    args.append(int(limit))
    conn = init_db()
    return [_row_to_thesis(r) for r in conn.execute(sql, args).fetchall()]


def update_thesis(thesis_id: str, **fields) -> bool:
    """按白名单更新；空调用返回 False。"""
    updates = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not updates:
        return False
    if "stance" in updates:
        _check_enum("stance", updates["stance"], STANCES)
    if "status" in updates:
        _check_enum("status", updates["status"], STATUSES)
    if "conviction" in updates:
        c = int(updates["conviction"])
        if not 1 <= c <= 5:
            raise ValueError("conviction 必须在 1-5")
        updates["conviction"] = c
    if "key_assumptions" in updates:
        updates["key_assumptions"] = _normalize_assumptions(updates["key_assumptions"])
    if "falsifiers" in updates:
        updates["falsifiers"] = _normalize_falsifiers(updates["falsifiers"])

    cols = ", ".join(f"{k} = ?" for k in updates)
    args = list(updates.values()) + [thesis_id]
    conn = init_db()
    cur = conn.execute(
        f"UPDATE thesis SET {cols}, updated_at = datetime('now') WHERE id = ?", args)
    conn.commit()
    return cur.rowcount > 0


def delete_thesis(thesis_id: str) -> bool:
    conn = init_db()
    conn.execute("DELETE FROM thesis_evidence WHERE thesis_id = ?", (thesis_id,))
    conn.execute("DELETE FROM thesis_review WHERE thesis_id = ?", (thesis_id,))
    cur = conn.execute("DELETE FROM thesis WHERE id = ?", (thesis_id,))
    conn.commit()
    return cur.rowcount > 0


def count_theses() -> int:
    conn = init_db()
    return int(conn.execute("SELECT COUNT(*) FROM thesis").fetchone()[0])


# ── 证据 ──────────────────────────────────────────────────────

def add_evidence(thesis_id: str, ref: str, *, kind: str = "fact",
                 direction: str = "supports", note: str = "") -> int:
    if not str(ref or "").strip():
        raise ValueError("ref 不能为空")
    _check_enum("kind", kind, EVIDENCE_KINDS)
    _check_enum("direction", direction, DIRECTIONS)
    if get_thesis(thesis_id) is None:
        raise ValueError(f"论点不存在：{thesis_id}")
    conn = init_db()
    cur = conn.execute(
        "INSERT INTO thesis_evidence (thesis_id, ref, kind, direction, note)"
        " VALUES (?,?,?,?,?)",
        (thesis_id, ref.strip(), kind, direction, note or ""),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_evidence(thesis_id: str) -> list[dict]:
    conn = init_db()
    rows = conn.execute(
        "SELECT * FROM thesis_evidence WHERE thesis_id = ? ORDER BY id ASC",
        (thesis_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_evidence(evidence_id: int) -> bool:
    conn = init_db()
    cur = conn.execute("DELETE FROM thesis_evidence WHERE id = ?", (int(evidence_id),))
    conn.commit()
    return cur.rowcount > 0


# ── 复盘 ──────────────────────────────────────────────────────

def add_review(thesis_id: str, verdict: str, *, snapshot: Any = None,
               note: str = "") -> int:
    _check_enum("verdict", verdict, VERDICTS)
    if get_thesis(thesis_id) is None:
        raise ValueError(f"论点不存在：{thesis_id}")
    conn = init_db()
    cur = conn.execute(
        "INSERT INTO thesis_review (thesis_id, verdict, snapshot, note)"
        " VALUES (?,?,?,?)",
        (thesis_id, verdict,
         json.dumps(snapshot or {}, ensure_ascii=False, default=str), note or ""),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_reviews(thesis_id: str) -> list[dict]:
    conn = init_db()
    rows = conn.execute(
        "SELECT * FROM thesis_review WHERE thesis_id = ? ORDER BY id DESC",
        (thesis_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["snapshot"] = _loads(d.get("snapshot"))
        out.append(d)
    return out
