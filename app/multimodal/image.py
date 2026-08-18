"""多模态 - 图片识别：easyocr 本地 OCR（视觉模型暂缓，磁盘空间不足）。

引擎说明：paddleocr 3.5 需新装 paddlepaddle(~200MB)，磁盘受限下改用本机
已装的 easyocr（torch 已装，零新依赖），中文识别已验证。视觉语义暂缓。

输出统一带 as_of 时点标注；失败返回 {error, hint}，绝不编造。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from app.utils.logger import logger

_reader = None
_reader_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _get_reader():
    """惰性加载 easyocr（首次较慢，含模型加载）。"""
    global _reader
    if _reader is not None:
        return _reader
    with _reader_lock:
        if _reader is None:
            import easyocr
            _reader = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
    return _reader


def _img_array(data: bytes):
    import cv2
    import numpy as np
    buf = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("无法解码图片（格式不支持或文件损坏）")
    return img


def _extract_lines(img) -> list[dict]:
    reader = _get_reader()
    lines = []
    for box, txt, conf in reader.readtext(img):
        if txt and str(txt).strip():
            lines.append({"text": str(txt), "confidence": float(conf)})
    return lines


def ocr_array(img) -> dict:
    """对已解码图像 ndarray 做 OCR。"""
    t0 = time.perf_counter()
    try:
        lines = _extract_lines(img)
        return {
            "kind": "image",
            "text": "\n".join(l["text"] for l in lines),
            "lines": lines,
            "vision": None,  # 视觉模型暂缓（磁盘空间不足）
            "source": "easyocr",
            "as_of": _now(),
            "seconds": round(time.perf_counter() - t0, 3),
            "error": None,
        }
    except Exception as e:
        logger.error("OCR 失败: %s", e)
        return {
            "kind": "image", "text": "", "lines": [], "vision": None,
            "source": "easyocr", "as_of": _now(), "seconds": 0.0,
            "error": str(e), "hint": "图片 OCR 不可用（检查 easyocr 及模型）",
        }


def ocr_image(data: bytes) -> dict:
    """对图片字节做 OCR。"""
    try:
        img = _img_array(data)
    except Exception as e:
        return {
            "kind": "image", "text": "", "lines": [], "vision": None,
            "source": "easyocr", "as_of": _now(), "seconds": 0.0,
            "error": str(e), "hint": "图片解码失败",
        }
    return ocr_array(img)
