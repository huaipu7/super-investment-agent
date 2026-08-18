"""合规复核 API —— 查看待复核输出并标记已审（单用户，无鉴权）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.compliance.queue import list_flagged, mark_reviewed

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])


@router.get("/flagged")
def flagged(reviewed: str | None = None, limit: int = 100):
    rv = None
    if reviewed is not None:
        rv = 1 if reviewed.lower() in ("1", "true", "yes") else 0
    rows = list_flagged(reviewed=rv, limit=limit)
    return {"count": len(rows), "rows": rows}


@router.post("/{flag_id}/review")
def review(flag_id: int):
    ok = mark_reviewed(flag_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"记录不存在: {flag_id}")
    return {"ok": True, "id": flag_id}
