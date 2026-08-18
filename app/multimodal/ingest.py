"""多模态统一入口 —— 图 / 音 / 视频 → 结构化文本。

按扩展名分派到 image / audio / video 处理器；
缺依赖或失败 → 返回 {error, hint}，绝不静默编造。
"""
from __future__ import annotations

import os

from . import audio as _audio
from . import image as _image
from . import video as _video

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".gif"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".ts", ".wmv"}

MAX_BYTES = 50 * 1024 * 1024


def kind_for(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return "unknown"


def ingest_bytes(data: bytes, filename: str = "", *, language: str | None = None,
                 fps_interval: float = 2.0, max_frames: int = 8,
                 max_bytes: int = MAX_BYTES) -> dict:
    if not data:
        return {"kind": "unknown", "error": "空文件", "hint": "上传内容为空"}
    if len(data) > max_bytes:
        mb = max_bytes // (1024 * 1024)
        return {"kind": "unknown", "error": f"文件超过 {mb}MB 上限",
                "hint": "请压缩或裁剪后重试"}
    kind = kind_for(filename)
    if kind == "image":
        return _image.ocr_image(data)
    if kind == "audio":
        return _audio.transcribe_audio(data, filename=filename, language=language)
    if kind == "video":
        return _video.extract_video(data, filename=filename, fps_interval=fps_interval,
                                    max_frames=max_frames, language=language)
    return {"kind": "unknown", "error": f"不支持的文件类型: {filename}",
            "hint": "支持 图片(jpg/png/webp) / 音频(mp3/wav/m4a) / 视频(mp4/mov/mkv)"}


def summarize(result: dict) -> str:
    """把多模态结果转成可注入对话上下文的文本摘要。"""
    kind = result.get("kind")
    text = result.get("text", "")
    if kind == "image":
        n = len(result.get("lines") or [])
        return (f"[图片已识别] 源={result.get('source')}，OCR {n} 行"
                + (f"\n{text[:2000]}" if text else ""))
    if kind == "audio":
        dur = result.get("duration_sec")
        dur_s = f"{dur}s" if dur is not None else "?"
        return (f"[音频已转写] 时长={dur_s} · 语言={result.get('language')}"
                + (f"\n{text[:2000]}" if text else ""))
    if kind == "video":
        n = result.get("frame_count", 0)
        aud = result.get("audio") or {}
        dur = aud.get("duration_sec")
        aud_s = f"音轨{dur}s" if dur is not None else "无音轨"
        return (f"[视频已解析] {n} 帧OCR + {aud_s}"
                + (f"\n{text[:2000]}" if text else ""))
    return ""
