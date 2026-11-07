"""反馈 API —— POST /api/v1/feedback（数据飞轮入口）。

request_id / msg_id 必填：这两个字段是与 audit.db、memory.db 关联的钥匙，
缺了反馈就是孤儿数据，无法归因到具体一次回复。

model / prompt_version / git_sha 由服务端打戳，客户端传了也不采信——
否则「哪个版本变差」可以被人为指定，归因就失效了。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.audit.log import audit
from app.audit.middleware import get_request_id
from app.feedback import store

router = APIRouter(prefix="/api/v1", tags=["feedback"])


class FeedbackIn(BaseModel):
    request_id: str = Field(..., description="原对话请求的 X-Request-Id")
    msg_id: str = Field(..., description="助手消息 id（对话响应回传）")
    label: Literal["up", "down"]
    scope: Literal["message", "fact", "report"] = "message"
    fact_id: str | None = None
    options: list[str] = Field(default_factory=list)
    note: str | None = Field(None, max_length=2000)


@router.post("/feedback", status_code=201)
def post_feedback(fb: FeedbackIn) -> dict:
    try:
        rec = store.add(
            request_id=fb.request_id, msg_id=fb.msg_id, label=fb.label,
            scope=fb.scope, fact_id=fb.fact_id, options=fb.options, note=fb.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    audit("feedback.submit", {
        "feedback_id": rec["id"], "msg_id": rec["msg_id"],
        "label": rec["label"], "scope": rec["scope"], "fact_id": rec.get("fact_id"),
        "options": rec["options"],
    }, request_id=get_request_id())

    return {
        "ok": True,
        "id": rec["id"],
        "label": rec["label"],
        # 👎 只进待审队列；说清楚它没有自动进回归集
        "inbox": "data/flywheel/inbox.jsonl" if rec["label"] == "down" else None,
        "next": ("已进入待审队列，需人工审阅后才能成为回归用例"
                 if rec["label"] == "down" else None),
    }


@router.get("/feedback/recent")
def list_feedback(limit: int = Query(50, ge=1, le=500)) -> dict:
    """最近反馈（人工审阅队列的只读视图）。"""
    return {"items": store.recent(limit), "stats": store.stats()}
