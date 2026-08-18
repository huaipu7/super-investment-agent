"""用户画像 API —— /api/v1/profile（单用户，持久化到 memory.user_profiles）。

支持字段（受限白名单，避免任意 key 写入）：risk_preference / holdings /
notes / investment_goal。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.memory import db as memory_db

router = APIRouter(prefix="/api/v1/profile", tags=["profile"])

ALLOWED_KEYS = {"risk_preference", "holdings", "notes", "investment_goal"}


class ProfileUpdate(BaseModel):
    key: str
    value: str


@router.get("")
def get_profile():
    return {"profile": memory_db.get_all_profiles()}


@router.put("")
def put_profile(req: ProfileUpdate):
    key = req.key.strip()
    if key not in ALLOWED_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的画像字段: {key}（可选: {sorted(ALLOWED_KEYS)}）",
        )
    value = req.value.strip()
    memory_db.set_profile(key, value)
    return {"ok": True, "key": key, "value": value}
