"""知识库检索 —— 直接查切片，而不是只能从报告里间接看引用。

两万多个切片进了库，之前唯一的入口是「跑一次深度研究，看它引用了什么」。
这一页让检索本身可查：分数拆开显示（dense / 词法 / 重排），来源与日期摆在明面上，
重排到底有没有生效也写清楚——排序悄悄变了而不说，比不重排更糟。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import streamlit as st

from ui import api_client as api

KIND_OPTIONS = ["全部", "研报", "披露文件", "论文", "政策",
                "宏观新闻", "宏观数据", "新闻", "公告", "股东大会"]
RERANK_LABELS = {
    "auto": "跟随配置（RERANK_ENABLED）",
    "on": "强制开（v1 只读快照仍会旁路）",
    "off": "关闭",
}
RERANK_NOTE_LABELS = {
    "enabled": "已重排",
    "disabled_by_config": "未重排：配置里关着",
    "disabled_by_request": "未重排：本次请求选了关闭",
    "v1_snapshot": "未重排：v1 只读快照强制旁路（L2 基线不许动）",
    "model_missing": "未重排：本地没有重排模型",
    "score_error": "未重排：打分出错，已保留原顺序",
    "unknown": "未重排：原因未知",
}


def _fmt_score(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.4f}"
    except (TypeError, ValueError):
        return str(v)


def _cell(v) -> str:
    """pandas 会把空值变成 NaN，直接 str() 会得到 "nan"。"""
    if v is None:
        return ""
    if isinstance(v, float) and v != v:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "<na>", "none") else s


def _rows(items: list[dict]) -> list[dict]:
    return [{
        "#": i + 1,
        "类型": _cell((it.get("metadata") or {}).get("kind")) or "—",
        "标的": _cell((it.get("metadata") or {}).get("ticker")) or "—",
        "日期": _cell((it.get("metadata") or {}).get("date")) or "—",
        "融合分": _fmt_score(it.get("score")),
        "Dense": _fmt_score(it.get("dense_score")),
        "词法": _fmt_score(it.get("lex_score")),
        "重排分": _fmt_score(it.get("rerank_score")),
    } for i, it in enumerate(items)]


def _rerank_badge(res: dict) -> str:
    if res.get("reranked"):
        return "🔀 已重排（cross-encoder）"
    note = res.get("rerank_note")
    return "↕️ " + RERANK_NOTE_LABELS.get(note, f"未重排（{note or '原因未知'}）")


@st.cache_data(ttl=60, show_spinner=False)
def _stats() -> dict:
    return api.kb_stats()


def _render_results(res: dict) -> None:
    items = res.get("items") or []
    st.markdown(f"#### 命中 {len(items)} 条")
    st.caption(f"{_rerank_badge(res)}　查询：`{res.get('query')}`　"
               f"过滤：`{res.get('where') or '无'}`")
    if not items:
        st.info("这条查询在库里找不到匹配切片。换关键词，或把「类型」放宽到全部。")
        return

    st.dataframe(pd.DataFrame(_rows(items)), width="stretch", hide_index=True)

    for i, it in enumerate(items):
        meta = it.get("metadata") or {}
        title = (f"#{i + 1}　[{_cell(meta.get('kind')) or '—'}]　"
                 f"{_cell(meta.get('ticker')) or '宏观'}　"
                 f"{_cell(meta.get('date')) or '—'}　"
                 f"融合 {_fmt_score(it.get('score'))}")
        with st.expander(title):
            src = _cell(meta.get("source"))
            url = _cell(meta.get("url"))
            st.caption(f"来源：{src or '—'}　章节：{_cell(meta.get('section')) or '—'}")
            if url:
                st.caption(f"原文：{url}")
            st.caption(f"分数：融合 {_fmt_score(it.get('score'))}　"
                       f"dense {_fmt_score(it.get('dense_score'))}　"
                       f"词法 {_fmt_score(it.get('lex_score'))}　"
                       f"重排 {_fmt_score(it.get('rerank_score'))}")
            st.text(it.get("content") or "")


def page() -> None:
    st.markdown("### 🔍 知识库检索")
    st.caption("直接查库里的切片：分数拆成 dense / 词法 / 重排三列，来源与日期都在。"
               "检索不到就是检索不到，不补默认内容。")

    try:
        s = _stats()
    except api.ApiError as e:
        st.error(f"知识库不可用：{e}")
        return
    if not s.get("available"):
        st.warning("嵌入后端不可用：检索会退化成纯词法匹配（分数里 dense 恒为空）。")

    kinds = s.get("kinds") or {}
    detail = "　".join(f"{k} {v}" for k, v in kinds.items() if v) or "—"
    st.caption(f"共 **{s.get('count', '—')}** 个切片　后端：{s.get('backend', '—')}")
    st.caption(f"构成：{detail}")

    with st.form("kb_search"):
        q = st.text_input("查询", placeholder="例如：茅台 提价 毛利率 / 10-K risk factors")
        c1, c2, c3, c4 = st.columns(4)
        kind = c1.selectbox("类型", KIND_OPTIONS, index=0)
        ticker = c2.text_input("标的（可空）", placeholder="600519.SS / AAPL")
        top_k = c3.slider("返回条数", 1, 50, 10)
        mode = c4.radio("重排", list(RERANK_LABELS), index=0,
                        format_func=lambda k: RERANK_LABELS[k])
        submitted = st.form_submit_button("检索", type="primary")

    if submitted:
        if not q.strip():
            st.error("查询不能为空。")
            return
        try:
            st.session_state["kb_res"] = api.kb_search(
                q.strip(), kind="" if kind == "全部" else kind,
                ticker=ticker.strip(), top_k=int(top_k), rerank=mode)
        except api.ApiError as e:
            st.error(f"检索失败：{e}")
            return

    res = st.session_state.get("kb_res")
    if res:
        st.divider()
        _render_results(res)
    else:
        st.caption("输入关键词后点「检索」。")
