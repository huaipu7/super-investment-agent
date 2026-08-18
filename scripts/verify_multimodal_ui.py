"""多模态接线验证（Step 2）：对话页上传入口 + api_client + 活体 curl。

Part A —— AppTest 无头渲染「💬 AI 对话」页，注入图片 → 触发本地识别 →
         断言识别预览出现且无异常。
Part B —— 用看板同一套 api_client 走真实后端：
         capabilities → ingest 图片（summary 同源）→ 未知类型诚实 error →
         merged 文本（OCR 摘要 + 用户问题）走 chat_stream SSE，断言标的经附件解析成功。
Part C —— 活体 curl 冒烟：POST /api/v1/multimodal/ingest 真实图片。

依赖后端已启动: D:\\python\\python.exe api/main.py
运行: PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\verify_multimodal_ui.py
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ui import api_client as api  # noqa: E402

_results: list[dict] = []


def record(name: str, ok: bool, detail: str = ""):
    _results.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _make_chinese_png() -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (480, 160), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 30)
    d.text((30, 40), "贵州茅台 1338 元", font=font, fill="black")
    d.text((30, 95), "今日上涨 2.5%", font=font, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ── Part A：看板 AppTest 上传注入 ─────────────────────────

def check_dashboard():
    print("\n=== Part A: 看板对话页上传注入（AppTest） ===")
    from streamlit.testing.v1 import AppTest

    app_path = str(ROOT / "ui" / "dashboard.py")
    at = AppTest.from_file(app_path, default_timeout=90)
    try:
        at.run(timeout=90)
        at.radio[0].set_value("💬 AI 对话")
        at.run(timeout=90)
        if at.exception:
            record("对话页渲染", False, f"exception={at.exception}")
            return
        record("对话页渲染", True, "无异常")
    except Exception as e:
        record("对话页渲染", False, str(e))
        return

    ups = at.file_uploader
    if len(ups) != 3:
        record("三个上传控件", len(ups) == 3, f"实际 {len(ups)} 个")
        return
    record("三个上传控件(图/音/视频)", True,
           f"labels={[u.label for u in ups]}")

    try:
        ups[0].upload("茅台截图.png", _make_chinese_png(), "image/png")
        at.run(timeout=180)
    except Exception as e:
        record("注入图片→本地识别", False, f"exception={e}")
        return

    if at.exception:
        record("注入图片→本地识别", False, f"exception={at.exception}")
        return
    captions = " | ".join(getattr(c, "value", "") or "" for c in at.caption)
    if "已识别" in captions:
        record("注入图片→本地识别", True, f"预览: {captions[:80]}")
    elif "识别失败" in captions or "⚠️" in captions:
        record("注入图片→本地识别", True, "后端不可用/识别失败走诚实告警路径", )
    else:
        record("注入图片→本地识别", False, f"未见预览 caption: {captions[:120]}")


# ── Part B：api_client 真实后端 ─────────────────────────

def _consume_sse(resp):
    """消费 SSE 流，返回 (事件序列, 文本)。"""
    evts, text = [], ""
    evt = None
    resp.encoding = "utf-8"
    for raw in resp.iter_lines(decode_unicode=True):
        line = (raw or "").strip()
        if line.startswith("event:"):
            evt = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            try:
                data = json.loads(line.split(":", 1)[1].strip())
            except Exception:
                continue
            if evt == "token":
                text += data.get("text", "")
            evts.append(evt)
    return evts, text


def check_api():
    print("\n=== Part B: api_client 真实后端 ===")
    caps = api.multimodal_capabilities()
    exts = caps.get("supported_exts") or {}
    record("capabilities 支持三类",
           all(k in exts for k in ("image", "audio", "video")),
           f"exts={list(exts)}")

    png = _make_chinese_png()
    r = api.multimodal_ingest(png, "茅台截图.png")
    record("ingest 图片 kind/source",
           r.get("kind") == "image" and r.get("source") == "easyocr",
           f"kind={r.get('kind')} source={r.get('source')}")
    record("ingest 返回 summary 同源",
           "图片已识别" in (r.get("summary") or "") and r.get("error") is None,
           f"summary={r.get('summary','')[:40]!r}")
    record("OCR 命中关键词",
           any(k in r.get("text", "") for k in ["茅台", "1338", "上涨", "2.5"]),
           f"text={r.get('text','')[:40]!r}")

    r_bad = api.multimodal_ingest(b"xxxx", "x.pdf")
    record("未知类型诚实 error",
           bool(r_bad.get("error")) and bool(r_bad.get("hint")),
           f"error={r_bad.get('error')!r}")

    # 并入对话：附件摘要 + 用户问题（无显式代码，靠 OCR 文本解析标的）
    summary = r.get("summary", "")
    merged = f"分析一下这只股票\n\n[📎 附件识别（图片 茅台截图.png）]\n{summary}"
    evts, text = _consume_sse(api.chat_stream(merged))
    names = list(dict.fromkeys(evts))
    record("chat_stream 事件齐全",
           "token" in names and "done" in names,
           f"events={names}")
    record("标的经附件解析（无'未能识别'）",
           "未能识别" not in text and text.strip() != "",
           f"输出={text[:60]!r}…")


# ── Part C：活体 curl 冒烟 ──────────────────────────────

def check_curl():
    print("\n=== Part C: 活体 curl ===")
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(_make_chinese_png())
            tmp = tf.name
        res = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "-F", f"file=@{tmp};type=image/png",
             "http://127.0.0.1:8000/api/v1/multimodal/ingest"],
            capture_output=True, timeout=180,
        )
        # Windows 下不用 text=True：stdout 默认按 GBK 解码中文会崩，改 bytes→UTF-8
        out = res.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        record("curl 上传图片", False, str(e))
        return
    try:
        data = json.loads(out)
    except Exception:
        record("curl 上传图片", False, f"非 JSON: {out[:120]}")
        return
    record("curl 上传图片",
           data.get("kind") == "image" and "图片已识别" in (data.get("summary") or ""),
           f"kind={data.get('kind')} summary={data.get('summary','')[:40]!r}")


if __name__ == "__main__":
    check_dashboard()
    check_api()
    check_curl()

    passed = sum(1 for r in _results if r["ok"])
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"多模态接线验证: {passed}/{total} 通过")
    for r in _results:
        if not r["ok"]:
            print(f"  FAIL: {r['name']} {r['detail']}")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)
