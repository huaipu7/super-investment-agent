"""多模态 - 视频解析：PyAV 抽关键帧(→OCR) + 抽音轨(→faster-whisper)。

视觉模型暂缓，关键帧只走 OCR；音轨转写为主文本。
"""
from __future__ import annotations

import io
import time
from datetime import datetime

from app.utils.logger import logger

from . import audio as _audio
from . import image as _image


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def extract_video(data: bytes, filename: str = "", fps_interval: float = 2.0,
                  max_frames: int = 8, language: str | None = None) -> dict:
    import av

    t0 = time.perf_counter()
    base = {"kind": "video", "source": ["opencv", "faster-whisper"],
            "as_of": _now(), "filename": filename}

    # ── 抽关键帧 ──
    frames = []
    try:
        container = av.open(io.BytesIO(data))
        try:
            vstream = next((s for s in container.streams if s.type == "video"), None)
            if vstream is None:
                raise ValueError("未找到视频轨")
            rate = float(vstream.average_rate or 30.0) or 30.0
            interval = max(1, int(round(rate * fps_interval)))
            idx = 0
            for frame in container.decode(vstream):
                if idx % interval == 0:
                    ts = float(frame.pts * vstream.time_base) if frame.pts else idx / rate
                    frames.append({"ts": round(ts, 2),
                                   "img": frame.to_ndarray(format="bgr24")})
                    if len(frames) >= max_frames:
                        break
                idx += 1
        finally:
            container.close()
    except Exception as e:
        base.update({"frames": [], "frame_count": 0, "text": "", "audio": None,
                     "error": str(e), "hint": "视频解码失败（需 av/PyAV）",
                     "seconds": round(time.perf_counter() - t0, 3)})
        return base

    frame_results = []
    for f in frames:
        ocr = _image.ocr_array(f["img"])
        frame_results.append({"ts": f["ts"], "ocr": ocr.get("text", ""),
                              "lines": ocr.get("lines", []),
                              "error": ocr.get("error")})

    # ── 音轨 → 转写（主文本） ──
    audio_result = _audio.transcribe_audio(data, filename=filename, language=language)

    base.update({
        "frames": frame_results,
        "frame_count": len(frame_results),
        "audio": {
            "text": audio_result.get("text", ""),
            "segments": audio_result.get("segments", []),
            "language": audio_result.get("language"),
            "duration_sec": audio_result.get("duration_sec"),
            "error": audio_result.get("error"),
            "hint": audio_result.get("hint"),
        },
        "text": audio_result.get("text", ""),
        "seconds": round(time.perf_counter() - t0, 3),
        "error": None,
    })
    return base
