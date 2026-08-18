"""审计查询 API —— 单用户只读视图。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.audit.log import query

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("")
def list_audit(event_type: str | None = Query(None),
               limit: int = Query(100, ge=1, le=1000)):
    rows = query(event_type=event_type, limit=limit)
    return {"count": len(rows), "rows": rows}
