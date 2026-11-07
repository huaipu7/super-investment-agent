"""💬 AI 对话（股票 / 基金双模式，SSE 逐 token 流式 + 多模态附件）"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from ui import api_client as api

_IMAGE_TYPES = ["png", "jpg", "jpeg", "bmp", "webp", "tiff", "gif"]
_AUDIO_TYPES = ["mp3", "wav", "m4a", "aac", "flac", "ogg", "wma"]
_VIDEO_TYPES = ["mp4", "avi", "mov", "mkv", "webm", "flv", "ts", "wmv"]
_KIND_LABEL = {"image": "图片(OCR)", "audio": "音频转写", "video": "视频解析"}
_UPLOAD_KEYS = ("up_img", "up_aud", "up_vid")


def _stream_into(prompt: str, resp, placeholder, status_ph) -> tuple[str, dict]:
    """消费后端 SSE 响应：status→阶段进度，token→逐字追加渲染。

    返回 (完整文本, {msg_id, request_id})：反馈要靠这两个 id 才能归因，
    流式文本本身没有可关联的标识。
    """
    text = ""
    evt = None
    ids = {"request_id": resp.headers.get("X-Request-Id") or "", "msg_id": ""}
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
            if evt == "status":
                status_ph.caption("⏳ " + data.get("message", ""))
            elif evt == "intent":
                tickers = data.get("tickers") or data.get("fund_codes") or []
                status_ph.caption(f"🎯 意图: {data.get('intent')} · {tickers}")
            elif evt == "token":
                text += data.get("text", "")
                placeholder.markdown(text)
            elif evt == "error":
                text += "\n\n⚠️ " + data.get("message", "")
                placeholder.markdown(text)
                break
            elif evt == "done":
                ids["msg_id"] = data.get("msg_id") or ids["msg_id"]
                ids["request_id"] = data.get("request_id") or ids["request_id"]
                break
    status_ph.empty()
    return text, ids


# ── 反馈（数据飞轮入口）─────────────────────────────────────

_ISSUES = ["数字不对", "来源不可信", "没有回答我的问题", "内容过时"]


def can_feedback(m: dict) -> bool:
    """只有拿到关联键的消息才给反馈入口；历史消息缺 id 就不显示（不摆坏按钮）。"""
    return bool((m.get("msg_id") or "").strip() and (m.get("request_id") or "").strip())


def _submit_feedback(rid: str, mid: str, label: str, *, scope: str = "message",
                     fact_id: str | None = None, options: list[str] | None = None,
                     note: str | None = None) -> bool:
    try:
        api.feedback(rid, mid, label, scope=scope, fact_id=fact_id,
                     options=options, note=note)
    except api.ApiError as e:
        st.error(f"反馈提交失败：{e}")
        return False
    st.session_state.setdefault("fb_done", {})[mid] = label
    return True


def _feedback_row(m: dict) -> None:
    mid, rid = m["msg_id"], m["request_id"]
    done = (st.session_state.get("fb_done") or {}).get(mid)
    if done:
        st.caption("✅ 已记录 · 👍 有帮助" if done == "up"
                   else "✅ 已记录 · 👎 进入待审队列，需人工审阅（不会自动进回归集）")
        return

    st.caption("这条回答有帮助吗？")
    c1, c2 = st.columns([1, 1])
    if c1.button("👍 有帮助", key=f"fb_up_{mid}"):
        if _submit_feedback(rid, mid, "up"):
            st.rerun()
    if c2.button("👎 有问题", key=f"fb_down_{mid}"):
        st.session_state["fb_open"] = mid
        st.rerun()

    if st.session_state.get("fb_open") != mid:
        return
    with st.form(f"fb_form_{mid}"):
        picks = st.multiselect("哪里不对？（可多选）", _ISSUES, key=f"fb_opts_{mid}")
        note = st.text_input("补充说明（选填）", key=f"fb_note_{mid}")
        if st.form_submit_button("提交反馈"):
            if _submit_feedback(rid, mid, "down", options=picks, note=note):
                st.session_state.pop("fb_open", None)
                st.rerun()


def _process_upload(f, widget_key: str) -> None:
    """上传文件 → 后端本地识别一次；结果存 session_state.attachment。

    以 (widget_key, 文件名, 大小) 为签名去重：同一文件在控件内重复 rerun 不重复识别。
    """
    if f is None:
        return
    sig = f"{widget_key}:{f.name}:{f.size}"
    if st.session_state.get("_up_proc") == sig:
        return
    st.session_state["_up_proc"] = sig
    try:
        with st.spinner(f"🔍 正在本地识别 {f.name}（OCR/转写，约需数秒）..."):
            r = api.multimodal_ingest(f.getvalue(), f.name)
    except api.ApiError as e:
        st.session_state.attachment = {"filename": f.name, "error": str(e)}
        return
    if r.get("error"):
        st.session_state.attachment = {
            "filename": f.name, "error": r["error"], "hint": r.get("hint") or "",
        }
    else:
        st.session_state.attachment = {
            "filename": f.name, "kind": r.get("kind"),
            "summary": r.get("summary") or "", "as_of": r.get("as_of"),
        }


def _attachment_context() -> str | None:
    """构建可并入对话的附件文本；无附件或识别失败 → None。"""
    att = st.session_state.get("attachment")
    if not att or att.get("error"):
        return None
    label = _KIND_LABEL.get(att.get("kind"), "附件")
    return (f"[📎 附件识别（{label} {att.get('filename')}）]\n"
            f"{att.get('summary', '')}")


def _render_upload() -> None:
    """多模态上传区：三个上传控件 + 识别预览 + 移除。"""
    st.caption("📎 可上传 图片/音频/视频：本地识别后并入对话，可对附件内容追问"
               "（识别文本均来自真实 OCR/转写，缺失字段标 —）")
    c1, c2, c3 = st.columns(3)
    with c1:
        f_img = st.file_uploader("📷 图片(OCR)", type=_IMAGE_TYPES, key="up_img")
    with c2:
        f_aud = st.file_uploader("🎙 音频(转写)", type=_AUDIO_TYPES, key="up_aud")
    with c3:
        f_vid = st.file_uploader("🎬 视频(抽帧+音轨)", type=_VIDEO_TYPES, key="up_vid")

    _process_upload(f_img, "up_img")
    _process_upload(f_aud, "up_aud")
    _process_upload(f_vid, "up_vid")

    att = st.session_state.get("attachment")
    if not att:
        return
    if att.get("error"):
        st.warning(f"📎 {att.get('filename')}：识别失败 — {att['error']}"
                   + (f"（{att.get('hint')}）" if att.get("hint") else ""))
    else:
        with st.container(border=True):
            st.caption(f"📎 已识别：{_KIND_LABEL.get(att.get('kind'), '附件')} · "
                       f"{att.get('filename')} · as_of {att.get('as_of')}")
            st.text(att.get("summary", ""))
    if st.button("🗑 移除附件", key="att_remove"):
        for k in _UPLOAD_KEYS:
            st.session_state.pop(k, None)
        st.session_state.pop("_up_proc", None)
        st.session_state.attachment = None
        st.rerun()


def page() -> None:
    st.header("💬 AI 对话")
    mode = st.segmented_control("对话模式", ["📈 股票", "📦 基金"], default="📈 股票",
                                key="chat_mode")
    is_fund = mode.startswith("📦")

    if is_fund:
        st.caption("支持: 查询净值 / 分析 / 持仓 / 多基金对比。例: 查询 005827 净值")
    else:
        st.caption("支持: 股价 / 走势 / 财务 / 对比 / 综合分析。例: 苹果股价")

    if st.button("🗑️ 清空对话", key="chat_clear"):
        st.session_state.messages = []
        st.rerun()

    if "messages" not in st.session_state:
        st.session_state.messages = []

    _render_upload()

    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m["role"] == "assistant" and can_feedback(m):
                _feedback_row(m)

    if prompt := st.chat_input("问点什么…"):
        attach_ctx = _attachment_context()
        merged = f"{prompt}\n\n{attach_ctx}" if attach_ctx else prompt
        if attach_ctx:
            st.session_state.attachment = None  # 附件随本条消息消费，避免重复并入
        st.session_state.messages.append({"role": "user", "content": merged})
        with st.chat_message("user"):
            st.markdown(merged)
        try:
            resp = api.fund_stream(merged) if is_fund else api.chat_stream(merged)
        except api.ApiError as e:
            with st.chat_message("assistant"):
                st.error(str(e))
            st.session_state.messages.append({"role": "assistant", "content": f"⚠️ {e}"})
            st.stop()
        with st.chat_message("assistant"):
            placeholder = st.empty()
            status_ph = st.empty()
            text, ids = _stream_into(prompt, resp, placeholder, status_ph)
        if not text.strip():
            text = "（无输出）"
        st.session_state.messages.append({"role": "assistant", "content": text, **ids})
