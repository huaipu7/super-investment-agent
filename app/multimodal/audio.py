"""多模态 - 音频转写：faster-whisper 本地（中文准确、零成本）。

音频解码用 PyAV（av）重采样到 16k mono float32，喂 faster-whisper。
模型默认 base（可用 WHISPER_MODEL 环境变量覆盖，如 small），首次从
HuggingFace 下载；无网络时返回 {error, hint}，不编造。
"""
from __future__ import annotations

import io
import os
import threading
import time
from datetime import datetime

from app.utils.logger import logger

MODEL_NAME = os.getenv("WHISPER_MODEL", "base")

_model = None
_model_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            # 优先本地缓存（离线可用）；未缓存才联网下载
            try:
                _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8",
                                      local_files_only=True)
            except Exception as e:
                logger.info("whisper 模型未缓存（%s），尝试联网下载 %s"
                            "（国内网络可设 HF_ENDPOINT=https://hf-mirror.com）", e, MODEL_NAME)
                _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
    return _model


def decode_pcm(data: bytes):
    """从音频/视频字节解码为 16k mono float32（PyAV 重采样）。"""
    import numpy as np
    import av
    from av.audio.resampler import AudioResampler

    container = av.open(io.BytesIO(data))
    try:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("未找到音轨")
        resampler = AudioResampler(format="s16", layout="mono", rate=16000)
        chunks = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray())
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray())
    finally:
        container.close()
    if not chunks:
        raise ValueError("音轨解码为空")
    pcm = np.concatenate(chunks, axis=1).flatten().astype(np.float32) / 32768.0
    if pcm.size == 0:
        raise ValueError("音轨解码为空")
    return pcm


def transcribe_pcm(pcm, language: str | None = None) -> dict:
    model = _get_model()
    segments, info = model.transcribe(pcm, language=language or None,
                                      beam_size=1, vad_filter=True)
    segs = []
    for s in segments:
        txt = s.text.strip()
        segs.append({"start": round(float(s.start), 2),
                     "end": round(float(s.end), 2), "text": txt})
    text = "\n".join(x["text"] for x in segs if x["text"])
    return {
        "text": text,
        "segments": segs,
        "language": getattr(info, "language", None),
        "duration_sec": round(float(getattr(info, "duration", 0.0) or 0.0), 2),
    }


def transcribe_audio(data: bytes, filename: str = "", language: str | None = None) -> dict:
    t0 = time.perf_counter()
    base = {"kind": "audio", "source": "faster-whisper", "model": MODEL_NAME,
            "as_of": _now(), "filename": filename}
    try:
        pcm = decode_pcm(data)
    except Exception as e:
        base.update({"text": "", "segments": [], "duration_sec": None,
                     "error": str(e), "hint": "音频解码失败（需 av/PyAV）",
                     "seconds": round(time.perf_counter() - t0, 3)})
        return base
    try:
        out = transcribe_pcm(pcm, language=language)
    except Exception as e:
        logger.error("faster-whisper 转写失败: %s", e)
        base.update({"text": "", "segments": [], "duration_sec": None,
                     "error": str(e),
                     "hint": "faster-whisper 转写失败（模型未下载或无网络）",
                     "seconds": round(time.perf_counter() - t0, 3)})
        return base
    out.update(base)
    out["seconds"] = round(time.perf_counter() - t0, 3)
    out["error"] = None
    return out
