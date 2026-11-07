# -*- coding: utf-8 -*-
"""自主深度调查看板 —— 假设驱动的取证链可视化。

展示「真实信号 → 播种成因族 → 多轮取证 → 证据分级 → 报告」全链路，
并**把 barrier 显示出来**（档位标签里直接写清轮次/工具/时长上限）。

诚实边界（约束#7）：
  - 数值一律来自真实数据源或真实计算，缺失渲染为 "—"，绝不填 0。
  - 因果结论按证据强度分级（已验证/部分验证/未验证/证据不足）；
    未采信的证据单独列出并标明"计零"，让人看得见闸门在起作用。
  - 上游/下游关系无真实数据源，只来自真实文本的推断，标为「模型推断」低置信。

启动：D:\\python\\python.exe -m streamlit run ui/dashboard.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from app.config import get_settings
from app.investigation.report import STATUS_DISPLAY
from ui import api_client as api
from ui import components

_DEPTH_LIMITS = {
    "standard": ("INVESTIGATION_STANDARD_MAX_ROUNDS",
                 "INVESTIGATION_STANDARD_MAX_TOOL_CALLS",
                 "INVESTIGATION_STANDARD_MAX_LLM_CALLS",
                 "INVESTIGATION_STANDARD_DEADLINE_SECONDS"),
    "deep": ("INVESTIGATION_DEEP_MAX_ROUNDS",
             "INVESTIGATION_DEEP_MAX_TOOL_CALLS",
             "INVESTIGATION_DEEP_MAX_LLM_CALLS",
             "INVESTIGATION_DEEP_DEADLINE_SECONDS"),
}


def _fmt(v) -> str:
    """None / NaN → "—"，否则原样展示（缺失就是缺失，不填 0）。"""
    try:
        if v is None or pd.isna(v):
            return "—"
    except (TypeError, ValueError):
        pass
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _depth_labels() -> tuple[dict[str, str], dict[str, str]]:
    """档位标签里直接写出真实上限——让 barrier 对用户可见。"""
    s = get_settings()
    labels, back = {}, {}
    for key, (mr, mt, ml, dl) in _DEPTH_LIMITS.items():
        text = (f"{'标准' if key == 'standard' else '深度'}"
                f"（≤{getattr(s, mr)} 轮 / ≤{getattr(s, mt)} 次工具 / "
                f"≤{getattr(s, ml)} 次 LLM / ≤{getattr(s, dl)} 秒）")
        labels[key] = text
        back[text] = key
    return labels, back


def _consume_stream(run_id: str, bar, live: dict) -> str:
    """消费 SSE：progress → 进度条 + 实时面板；返回终态（completed/failed/error）。"""
    try:
        resp = api.investigation_stream(run_id)
    except api.ApiError as e:
        st.error(f"SSE 连接失败：{e}")
        return "error"
    resp.encoding = "utf-8"
    evt = None
    for raw in resp.iter_lines(decode_unicode=True):
        line = (raw or "").strip()
        if line.startswith("event:"):
            evt = line.split(":", 1)[1].strip()
            continue
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line.split(":", 1)[1].strip())
        except Exception:                       # noqa: BLE001
            continue
        if evt == "progress":
            pct = max(0.0, min(1.0, float(data.get("pct", 0)) / 100.0))
            bar.progress(pct, text=f"{data.get('label', '')} — {data.get('message', '')}")
            spent = data.get("spent") or {}
            if data.get("tool"):
                live["tools"] = (live.get("tools") or []) + [data["tool"]]
            with live["box"].container():
                st.caption(f"阶段：{data.get('phase')}　第 {data.get('round')}/"
                           f"{data.get('max_rounds')} 轮")
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("轮次", f"{spent.get('rounds', 0)}")
                c2.metric("工具调用", f"{spent.get('tool_calls', 0)}")
                c3.metric("LLM 调用", f"{spent.get('llm_calls', 0)}")
                c4.metric("去重命中", f"{spent.get('duplicates', 0)}")
                c5.metric("剩余秒", _fmt(spent.get("remaining")))
                if live["tools"]:
                    st.caption("工具链：" + " → ".join(live["tools"][-8:]))
                hyps = data.get("hypotheses") or []
                if hyps:
                    st.caption("假设状态：" + "　".join(
                        f"{h['id']}={h.get('status') or '—'}" for h in hyps))
        elif evt == "error":
            bar.empty()
            st.error(f"调查异常：{data.get('message', '')}")
            return "failed"
        elif evt == "status":
            if data.get("status") == "completed":
                bar.progress(1.0, text="调查完成")
                return "completed"
            bar.empty()
            return "failed"
        elif evt == "done":
            break
    return "completed"


def _render_report(rep: dict, run_id: str) -> None:
    st.divider()
    st.markdown("#### 📋 调查报告")

    spent = rep.get("spent") or {}
    limits = rep.get("limits") or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("轮次", f"{spent.get('rounds', 0)} / {limits.get('max_rounds', '—')}")
    c2.metric("工具调用", f"{spent.get('tool_calls', 0)} / {limits.get('max_tool_calls', '—')}")
    c3.metric("LLM 调用", f"{spent.get('llm_calls', 0)} / {limits.get('max_llm_calls', '—')}")
    c4.metric("去重命中", f"{spent.get('duplicates', 0)}")
    st.caption(f"停止原因：{rep.get('stop_reason_label')}（`{rep.get('stop_reason')}`）")
    over = rep.get("budget_overshoot_seconds") or 0
    if over > 1:
        st.warning(f"墙钟超出上限 {round(over, 1)} 秒——在途 LLM 请求无法中断，"
                   f"如实记录而非假装硬停。")

    if rep.get("prose"):
        st.markdown(rep["prose"])
    if rep.get("prose_note"):
        st.info(rep["prose_note"])
    if rep.get("degrade_note"):
        st.warning(f"降级说明：{rep['degrade_note']}")
    if rep.get("data_note"):
        st.info(f"数据说明：{rep['data_note']}")

    # ── 触发的真实信号 ──
    mats = rep.get("material_signals") or []
    with st.expander(f"触发的真实财务信号（{len(mats)} 条达到阈值）", expanded=bool(mats)):
        if not mats:
            st.caption("无达到阈值的异常信号 → 按设计不生成任何假设。")
        else:
            st.dataframe(pd.DataFrame([{
                "信号": s.get("label"), "值": _fmt(s.get("value")),
                "相对变化": (f"{_fmt(s.get('rel_value'))}%"
                            if s.get("rel_value") is not None else "—"),
                "单位": s.get("unit") or "—", "方向": s.get("direction"),
                "阈值": s.get("threshold") or "—",
            } for s in mats]), width="stretch", hide_index=True)

    # ── 假设表 ──
    rows = rep.get("hypotheses") or []
    if not rows:
        st.info("本次未生成假设。")
    else:
        st.dataframe(pd.DataFrame([{
            "编号": r["id"], "成因族": r.get("family_label"),
            "假设": r.get("statement"), "状态": r.get("status_label"),
            "支持": r.get("support_count", 0), "反驳": r.get("contradict_count", 0),
            "未采信": r.get("rejected_count", 0),
            "来源类型": "、".join(r.get("support_kinds") or []) or "—",
        } for r in rows]), width="stretch", hide_index=True)

        counts = {}
        for r in rows:
            counts[r.get("status_label")] = counts.get(r.get("status_label"), 0) + 1
        st.caption("状态分布：" + "　".join(
            f"**{k}** {v}" for k, v in counts.items()))

    # ── 逐假设的证据台账 ──
    for r in rows:
        meta = STATUS_DISPLAY.get(r.get("status"), {})
        icon = {"green": "✅", "orange": "🟠", "gray": "⬜", "red": "🚫"}.get(
            meta.get("color"), "•")
        with st.expander(f"{icon} {r['id']}　{r.get('status_label')}　"
                         f"{r.get('family_label')}　—　{r.get('statement')}"):
            st.caption(f"判定依据：{r.get('status_reason')}")
            ev = r.get("evidence") or []
            if ev:
                st.dataframe(pd.DataFrame([{
                    "来源类型": e.get("source_kind"), "强度": e.get("strength"),
                    "关系": e.get("relation"), "轮次": e.get("round"),
                    "出处": e.get("source_ref") or "—",
                    "片段": (e.get("snippet") or "—")[:60],
                } for e in ev]), width="stretch", hide_index=True)
            rej = r.get("rejected_evidence") or []
            if rej:
                st.caption("未采信（计零，仅留档）——闸门确实拦下了这些指认：")
                for e in rej:
                    st.caption(f"　· {e.get('tool')}：{e.get('reject_reason')}")

    # ── 红队：只在深度档有 ──
    components.render_redteam(rep.get("redteam"))

    # ── 未验证原因 ──
    unv = rep.get("unverified_causes") or []
    if unv:
        st.markdown("##### ⚠ 未验证原因（不得当作结论）")
        for u in unv:
            st.caption(f"**{u['id']}**（{u['status']}）{u['statement']} —— {u['status_reason']}")

    # ── 模型推断台账 ──
    ledger = rep.get("llm_hypothesis_ledger") or []
    if ledger:
        st.markdown("##### 🧪 模型推断台账（无原始来源，低置信）")
        st.dataframe(pd.DataFrame(ledger), width="stretch", hide_index=True)

    st.markdown("##### 免责与口径")
    st.caption(rep.get("disclaimer") or "")
    st.caption("诚实边界：财报快照非 point-in-time；上游/下游关系无真实数据源，"
               "仅来自真实文本的推断；免费源不提供一致预期/目标价，"
               "因此不存在「业绩不及预期」类假设。")

    try:
        md = api.investigation_report_md(run_id)
        st.download_button("⬇ 下载确定性 Markdown 报告", md,
                           file_name=f"investigation_{rep.get('ticker')}.md",
                           mime="text/markdown")
    except api.ApiError as e:
        st.caption(f"报告导出不可用：{e}")


def page() -> None:
    st.markdown("### 🧭 自主深度调查")
    st.caption("拿到真实财报 → 识别异常信号 → 形成成因假设 → 自己一轮轮查新闻/公告/"
               "同业/语料去找证据 → 按证据强度给结论。全程有硬预算，且**每一步都能指回真实来源**。")
    st.caption("⚠ 因果结论是**假设**而非定论；证据不足时如实说证据不足，绝不编造成因。")

    if not get_settings().check_llm_ready():
        st.warning("未配置 LLM_API_KEY——调查无法生成假设与选择调查方向，"
                   "将只能给出规则族回退结果。请在 .env 配置后再试。")

    labels, back = _depth_labels()
    with st.form("investigation_form"):
        col1, col2 = st.columns([1, 2])
        with col1:
            ticker = st.text_input("股票代码", value="600519.SS",
                                   help="如 600519.SS / 000858.SZ / AAPL / 0700.HK")
        with col2:
            depth_label = st.radio("调查档位（括号内是硬上限）",
                                   list(labels.values()), horizontal=False)
        period = st.selectbox("报表口径", ["annual", "quarterly"], index=0)
        submitted = st.form_submit_button("🔎 开始调查", type="primary")

    if submitted:
        t = (ticker or "").strip().upper()
        if not t:
            st.error("请填写股票代码。")
            return
        try:
            with st.spinner("正在启动后台调查…"):
                started = api.investigation_run(t, depth=back[depth_label],
                                                period=period)
        except api.ApiError as e:
            st.error(f"启动失败：{e}")
            return
        st.session_state["inv_run_id"] = started.get("run_id")
        st.session_state["inv_ticker"] = t
        st.session_state["inv_report"] = None

    run_id = st.session_state.get("inv_run_id")
    if not run_id:
        st.info("填写代码并选择档位后开始。调查期间可实时看到轮次/工具/去重/剩余时间，"
                "以及每条假设的状态变化。")
        return

    ticker_shown = st.session_state.get("inv_ticker", "")
    st.caption(f"本次运行：{ticker_shown}　run_id={run_id}")

    if st.session_state.get("inv_report") is None:
        bar = st.progress(0.0, text="等待后端…")
        live = {"box": st.empty(), "tools": []}
        state = _consume_stream(run_id, bar, live)
        if state != "completed":
            st.error("调查未正常完成，请查看后端日志或重试。")
            return
        try:
            st.session_state["inv_report"] = api.investigation_result(run_id)
        except api.ApiError as e:
            st.error(f"取报告失败：{e}")
            return

    rep = st.session_state.get("inv_report")
    if rep:
        _render_report(rep, run_id)

    if st.button("🔄 清除本次结果"):
        st.session_state["inv_report"] = None
        st.session_state["inv_run_id"] = None
        st.rerun()
