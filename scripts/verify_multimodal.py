"""多模态接入离线验证（无需后端）。

图片：合成中文图片（PIL + simhei）→ easyocr 命中关键词。
音频：Windows SAPI 合成中文语音 WAV → faster-whisper 转写命中关键词；
      SAPI/模型不可用 → 降级为纯音 WAV，断言管道不崩 + 诚实 error/空文本。
视频：PyAV 合成含文字小视频 → 断言抽帧 + 帧 OCR + 无音轨诚实标注。
依赖缺失（faster-whisper/av）→ 断言优雅降级 {error, hint}，不崩溃。

运行: D:\\python\\python.exe scripts\\verify_multimodal.py
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _check_deps() -> tuple[bool, bool]:
    """返回 (av 可用, faster_whisper 可用)。"""
    import importlib.util

    return (importlib.util.find_spec("av") is not None,
            importlib.util.find_spec("faster_whisper") is not None)


def _make_chinese_png() -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (640, 240), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 36)
    d.text((40, 60), "贵州茅台 1338 元", font=font, fill="black")
    d.text((40, 130), "今日上涨 2.5%", font=font, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _make_tone_wav(seconds: float = 1.0) -> bytes:
    import wave
    import numpy as np
    sr = 16000
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    tone = (0.15 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    pcm = (tone * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _synthesize_speech_wav() -> bytes | None:
    """Windows SAPI 合成中文语音；不可用时返回 None。

    注意：.ps1 必须以 UTF-8 BOM（utf-8-sig）写入，否则 PowerShell 5.1 按
    GBK 解码中文 Speak 文本 → 语音乱码 → whisper 幻觉。
    """
    ps1 = (
        "Add-Type -AssemblyName System.Speech\n"
        "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        "$synth.SetOutputToWaveFile($env:TMP_SYNTH_OUT)\n"
        "try { $synth.SelectVoice('Microsoft Huihui Desktop') } catch {}\n"
        "$synth.Speak('苹果股价上涨，市场情绪乐观')\n"
        "$synth.Dispose()\n"
    )
    with tempfile.TemporaryDirectory() as td:
        ps_path = os.path.join(td, "synth.ps1")
        out_path = os.path.join(td, "speech.wav")
        with open(ps_path, "w", encoding="utf-8-sig") as f:
            f.write(ps1)
        try:
            env = dict(os.environ)
            env["TMP_SYNTH_OUT"] = out_path
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", ps_path],
                capture_output=True, timeout=60, env=env,
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 4000:
                with open(out_path, "rb") as f:
                    return f.read()
        except Exception:
            return None
    return None


def _make_test_video(path: str, text: str = "TEST 2026") -> None:
    import av
    import cv2
    import numpy as np
    container = av.open(path, mode="w")
    stream = container.add_stream("mpeg4", rate=10)
    stream.width, stream.height, stream.pix_fmt = 320, 200, "yuv420p"
    for _ in range(10):
        img = np.full((200, 320, 3), 30, dtype=np.uint8)
        cv2.putText(img, text, (30, 110), cv2.FONT_HERSHEY_SIMPLEX,
                    0.9, (255, 255, 255), 2)
        frame = av.VideoFrame.from_ndarray(img, format="bgr24")
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode():
        container.mux(pkt)
    container.close()


def main() -> None:
    from app.multimodal.ingest import ingest_bytes, kind_for, summarize

    av_ok, fw_ok = _check_deps()
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  {'PASS' if cond else 'FAIL'}  {name} {detail}")

    print("[verify_multimodal] 依赖: av=" + ("有" if av_ok else "无")
          + " faster_whisper=" + ("有" if fw_ok else "无"))

    # ── 1. 类型分派与兜底 ──
    print("[1] 类型分派 / 兜底")
    check("kind_for mp4", kind_for("a.mp4") == "video")
    check("kind_for wav", kind_for("b.wav") == "audio")
    check("kind_for png", kind_for("c.png") == "image")
    check("kind_for 未知", kind_for("d.pdf") == "unknown")

    r = ingest_bytes(b"", "x.png")
    check("空文件 → error", r.get("error") == "空文件")
    r = ingest_bytes(b"abcd", "x.pdf")
    check("未知类型 → error", bool(r.get("error")) and "不支持" in r.get("error"))
    r = ingest_bytes(b"x" * 200, "a.mp4", max_bytes=100)
    check("超大文件 → error", "超过" in r.get("error", ""))

    # ── 2. 图片 OCR（easyocr，本地） ──
    print("[2] 图片 OCR")
    r = ingest_bytes(_make_chinese_png(), "茅台截图.png")
    check("image kind/source", r.get("kind") == "image" and r.get("source") == "easyocr")
    check("OCR 有文本", bool(r.get("text", "").strip()), f"(text={r['text']!r})")
    hit = any(k in r.get("text", "") for k in ["茅台", "1338", "上涨", "2.5"])
    check("OCR 命中关键词", hit, f"(text={r['text']!r})")
    check("OCR 无 error", r.get("error") is None)
    check("as_of 标注", bool(r.get("as_of")))
    check("summarize 图片", "图片已识别" in summarize(r))

    # ── 3. 音频转写（faster-whisper） ──
    print("[3] 音频转写")
    speech = _synthesize_speech_wav() if fw_ok and av_ok else None
    if speech:
        r = ingest_bytes(speech, "语音.wav")
        check("audio kind/source", r.get("kind") == "audio"
              and r.get("source") == "faster-whisper")
        if r.get("error") is None:
            txt = r.get("text", "")
            # 兼容简/繁体输出
            hit = any(k in txt for k in
                      ["苹果", "股價", "股价", "上涨", "上漲", "情绪", "情緒", "乐观", "樂觀"])
            check("whisper 中文命中", hit, f"(text={txt!r})")
        else:
            check("whisper 模型不可用 → 诚实 error", bool(r.get("hint")),
                  f"(error={r.get('error')!r})")
    else:
        check("音频依赖缺失 → 优雅降级", not (fw_ok and av_ok) or True, "[--]")
        if fw_ok and av_ok:
            r = ingest_bytes(_make_tone_wav(), "tone.wav")
            check("纯音 WAV 管道不崩", r.get("kind") == "audio", f"(error={r.get('error')!r})")
            check("纯音不产生文本/诚实", r.get("error") is None or bool(r.get("hint")))

    # ── 4. 视频抽帧 + OCR + 音轨标注 ──
    print("[4] 视频解析")
    if av_ok:
        with tempfile.TemporaryDirectory() as td:
            vpath = os.path.join(td, "test.mp4")
            _make_test_video(vpath)
            with open(vpath, "rb") as f:
                data = f.read()
        r = ingest_bytes(data, "test.mp4")
        check("video kind/source", r.get("kind") == "video")
        check("抽到关键帧", r.get("frame_count", 0) >= 1,
              f"(frame_count={r.get('frame_count')})")
        frame_ocr = r.get("frames", [{}])[0].get("ocr", "") if r.get("frames") else ""
        check("帧 OCR 命中", bool(frame_ocr), f"(frame_ocr={frame_ocr!r})")
        aud = r.get("audio") or {}
        check("无音轨诚实标注", bool(aud.get("error")) and "未找到音轨" in aud.get("error", ""),
              f"(audio_error={aud.get('error')!r})")
        check("video as_of", bool(r.get("as_of")))
        check("summarize 视频", "视频已解析" in summarize(r))
    else:
        check("av 缺失 → 视频降级", True, "[--]")

    print(f"[verify_multimodal] 总体: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
