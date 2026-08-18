"""多模态 API —— 图 / 音 / 视频本地预处理为文本。

POST /api/v1/multimodal/ingest         multipart 上传（file + 可选 language）
GET  /api/v1/multimodal/capabilities    能力声明（供看板控制上传按钮）
"""
from __future__ import annotations

import asyncio
import functools

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.audit.log import audit
from app.audit.middleware import get_request_id
from app.multimodal.ingest import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    MAX_BYTES,
    VIDEO_EXTS,
    ingest_bytes,
    summarize,
)
from app.observability.metrics import metrics

router = APIRouter(prefix="/api/v1/multimodal", tags=["multimodal"])


@router.post("/ingest")
async def ingest(file: UploadFile = File(...), language: str = Form("")):
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 50MB 上限")
    metrics.inc("multimodal_ingest_requests")
    rid = get_request_id()
    fn = file.filename or ""
    result = await asyncio.get_event_loop().run_in_executor(
        None,
        functools.partial(ingest_bytes, data, fn, language=language or None),
    )
    # 注入统一摘要文本，供看板预览与并入对话上下文（与本地 summarize 同源）
    result["summary"] = summarize(result)
    audit("multimodal.ingest",
          {"kind": result.get("kind"), "filename": fn,
           "error": result.get("error")},
          request_id=rid)
    return result


@router.get("/capabilities")
def capabilities():
    return {
        "image": {"source": "easyocr", "vision": False,
                  "vision_note": "视觉模型暂缓（磁盘空间不足）"},
        "audio": {"source": "faster-whisper", "model": "base"},
        "video": {"frame_ocr": True, "audio_transcribe": True},
        "supported_exts": {
            "image": sorted(IMAGE_EXTS),
            "audio": sorted(AUDIO_EXTS),
            "video": sorted(VIDEO_EXTS),
        },
        "max_bytes": MAX_BYTES,
    }
