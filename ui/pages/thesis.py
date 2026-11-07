"""🧾 论点台账 —— 为什么买 / 什么情况下认错，以及事实是否已经推翻了它。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import streamlit as st

from ui import api_client as api

STANCE_LABELS = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
STATUS_LABELS = {"active": "跟踪中", "confirmed": "已验证",
                 "falsified": "已证伪", "abandoned": "已放弃"}
VERDICT_LABELS = {"falsified": "🔴 证伪条件已触发",
                  "holding": "🟢 证伪条件未触发",
                  "inconclusive": "⚪ 无法判定"}
VERDICT_HELP = {
    "falsified": "至少一条证伪条件被真实事实命中",
    "holding": "全部条件都取到了真实值且均未命中",
    "inconclusive": "有条件取不到值，或没写条件 —— 「查不到」不等于「没出事」",
}
STATE_LABELS = {"breached": "🔴 命中", "holding": "🟢 未命中", "unknown": "⚪ 取不到值"}
REASON_LABELS = {
    "invalid_threshold": "阈值非法",
    "no_ledger": "没有可用产物台账",
    "path_not_in_ledger": "台账里没有这个路径",
    "ledger_unknown": "台账标记为 UNKNOWN",
    "non_finite": "值不是有限数",
    "unknown_falsifier": "存在取不到值的条件",
    "no_falsifiers": "没写证伪条件",
}
OPS = ["<", "<=", ">", ">=", "==", "!="]


@st.cache_data(ttl=60)
def _list(ticker: str, status: str, with_check: bool) -> list[dict]:
    return api.thesis_list(ticker=ticker, status=status, with_check=with_check)


@st.cache_data(ttl=300)
def _artifacts() -> list[dict]:
    return api.thesis_artifacts()


def _refresh() -> None:
    _list.clear()
    st.rerun()


def _badge(check: dict | None) -> str:
    if not check:
        return "⚫ 未检查"
    return VERDICT_LABELS.get(check.get("verdict"), "⚫ 未检查")


# ── 详情 ──────────────────────────────────────────────────────

def _render_detail(thesis_id: str) -> None:
    try:
        t = api.thesis_get(thesis_id)
    except api.ApiError as e:
        st.error(str(e))
        return

    st.divider()
    st.markdown(f"### 📌 {t['statement']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(f"**标的**　{t['ticker'] or '—'}")
    c2.markdown(f"**状态**　{STATUS_LABELS.get(t['status'], t['status'])}")
    c3.markdown(f"**看法**　{STANCE_LABELS.get(t['stance'], t['stance'])}")
    c4.markdown(f"**把握**　{'●' * t['conviction']}{'○' * (5 - t['conviction'])}")
    st.caption(f"周期：{t['horizon'] or '—'}　·　依据产物："
               f"{t['artifact_ref'] or '未关联'}　·　更新：{t['updated_at']}")

    if t["key_assumptions"]:
        st.markdown("**关键假设**")
        for a in t["key_assumptions"]:
            st.markdown(f"- {a}")

    st.markdown("**证伪条件**")
    if t["falsifiers"]:
        st.dataframe(pd.DataFrame([
            {"指标路径": f.get("metric", ""), "比较": f.get("op", ""),
             "阈值": f.get("value"), "备注": f.get("note", "")}
            for f in t["falsifiers"]
        ]), use_container_width=True, hide_index=True)
        st.caption("路径以 `[]` 结尾表示取最新一期，换新产物不会对错年份。")
    else:
        st.warning("这条论点没写证伪条件 —— 它无法被机器检查，也无法被证伪。")

    # ── 证伪检查 ──
    if st.button("🔍 运行证伪检查", key=f"chk_{thesis_id}"):
        try:
            st.session_state[f"chk_res_{thesis_id}"] = api.thesis_check(thesis_id)
        except api.ApiError as e:
            st.error(str(e))

    res = st.session_state.get(f"chk_res_{thesis_id}")
    if res:
        verdict = res.get("verdict")
        text = f"{VERDICT_LABELS.get(verdict, verdict)} · {VERDICT_HELP.get(verdict, '')}"
        (st.error if verdict == "falsified" else
         st.success if verdict == "holding" else st.info)(text)
        c = res.get("counts") or {}
        st.caption(f"共 {c.get('total', 0)} 条条件：命中 {c.get('breached', 0)}"
                   f" · 未命中 {c.get('holding', 0)} · 取不到值 {c.get('unknown', 0)}"
                   f"　｜　依据：{res.get('artifact') or res.get('artifact_reason', '—')}"
                   f"　·　检查于 {res.get('checked_at', '')}")
        if res.get("results"):
            st.dataframe(pd.DataFrame([
                {"指标": r["metric"], "比较": r["op"], "阈值": r["threshold"],
                 "实际值": r["observed"] if r["observed"] is not None else "—",
                 "期间": r.get("as_of") or "—", "出处": r.get("provenance") or "—",
                 "判定": STATE_LABELS.get(r["state"], r["state"]),
                 "原因": REASON_LABELS.get(r.get("reason"), "") if r.get("reason") else "",
                 "实际路径": r.get("resolved_path") or "—"}
                for r in res["results"]
            ]), use_container_width=True, hide_index=True)
        if any(r["state"] == "unknown" for r in res["results"]):
            st.caption("⚠️ 有条款取不到值：这不等于条件未触发，只说明这条判断没有依据。")

    # ── 证据 ──
    with st.expander(f"📎 证据（{len(t['evidence'])}）", expanded=False):
        if not t["evidence"]:
            st.caption("暂无证据。")
        for e in t["evidence"]:
            cols = st.columns([1.2, 5, 1])
            cols[0].markdown("✅ 支持" if e["direction"] == "supports" else "❌ 反对")
            cols[1].markdown(f"`{e['kind']}` {e['ref']}　{e['note']}")
            if cols[2].button("✕", key=f"ev_del_{e['id']}"):
                api.thesis_delete_evidence(e["id"])
                _refresh()
        with st.form(f"ev_form_{thesis_id}", clear_on_submit=True):
            c1, c2 = st.columns([3, 1])
            ref = c1.text_input("引用（台账路径 / 片段 id / URL）")
            kind = c2.selectbox("类型", ["fact", "snippet", "artifact", "doc"])
            c3, c4 = st.columns([1, 3])
            direction = c3.selectbox("方向", ["supports", "contradicts"],
                                     format_func=lambda x: "支持" if x == "supports" else "反对")
            note = c4.text_input("备注")
            if st.form_submit_button("添加证据"):
                if not ref.strip():
                    st.warning("引用不能为空")
                else:
                    try:
                        api.thesis_add_evidence(thesis_id, ref.strip(), kind=kind,
                                                direction=direction, note=note)
                        _refresh()
                    except api.ApiError as e:
                        st.error(str(e))

    # ── 复盘 ──
    with st.expander(f"🗒 复盘记录（{len(t['reviews'])}）", expanded=False):
        if not t["reviews"]:
            st.caption("暂无复盘记录。复盘只追加，不改历史。")
        for r in t["reviews"]:
            label = {"holds": "维持", "weakens": "转弱", "broken": "被推翻"}.get(r["verdict"])
            st.markdown(f"- **{label}**　{r['note']}　`{r['reviewed_at']}`")
        with st.form(f"rv_form_{thesis_id}", clear_on_submit=True):
            c1, c2 = st.columns([1, 3])
            verdict = c1.selectbox("结论", ["holds", "weakens", "broken"],
                                   format_func=lambda x: {"holds": "维持",
                                                          "weakens": "转弱",
                                                          "broken": "被推翻"}[x])
            note = c2.text_input("备注")
            snap = st.text_input("指标快照（可选，JSON）", placeholder='{"fair_value": 366.85}')
            if st.form_submit_button("记录复盘"):
                import json
                snapshot = None
                if snap.strip():
                    try:
                        snapshot = json.loads(snap)
                    except json.JSONDecodeError:
                        st.warning("快照不是合法 JSON，已忽略该字段")
                try:
                    api.thesis_add_review(thesis_id, verdict, snapshot=snapshot, note=note)
                    _refresh()
                except api.ApiError as e:
                    st.error(str(e))

    # ── 状态 / 删除 ──
    st.divider()
    c1, c2 = st.columns([2, 1])
    new_status = c1.selectbox(
        "调整状态", list(STATUS_LABELS),
        index=list(STATUS_LABELS).index(t["status"]),
        format_func=lambda x: STATUS_LABELS[x], key=f"st_{thesis_id}")
    if c1.button("保存状态"):
        try:
            api.thesis_update(thesis_id, {"status": new_status})
            _refresh()
        except api.ApiError as e:
            st.error(str(e))
    if c2.button("🗑 删除论点", type="secondary"):
        st.session_state[f"confirm_{thesis_id}"] = True
    if st.session_state.get(f"confirm_{thesis_id}"):
        st.warning("删除会同时删掉它的证据与复盘记录，且不可恢复。")
        if st.button("确认删除", type="primary", key=f"del_{thesis_id}"):
            if api.thesis_delete(thesis_id):
                st.session_state.pop("thesis_sel", None)
                _refresh()
            else:
                st.error("删除失败")


# ── 列表 ──────────────────────────────────────────────────────

def _list_tab() -> None:
    c1, c2, c3 = st.columns([2, 2, 1])
    ticker = c1.text_input("按标的过滤", key="th_filter_ticker", placeholder="如 600519.SS")
    status = c2.selectbox("按状态过滤", [""] + list(STATUS_LABELS),
                          format_func=lambda x: "全部" if not x else STATUS_LABELS[x],
                          key="th_filter_status")
    with_check = c3.toggle("跑证伪检查", value=True, key="th_with_check",
                           help="逐条对照关联产物的台账；证伪的排在最前")

    try:
        items = _list(ticker.strip(), status, with_check)
    except api.ApiError as e:
        st.error(str(e))
        return
    if not items:
        st.info("还没有论点。到「➕ 新建论点」写一条 —— 顺便写下什么情况下你会认错。")
        return

    breached = sum(1 for i in items if (i.get("check") or {}).get("verdict") == "falsified")
    if breached:
        st.error(f"🔴 {breached} 条论点的证伪条件已被真实事实触发，排在最前。")
    else:
        st.success("当前没有论点的证伪条件被触发。")

    for it in items:
        check = it.get("check")
        cols = st.columns([1.5, 6, 1.6, 1.4, 1.1, 1])
        v = (check or {}).get("verdict")
        cols[0].markdown(("🔴" if v == "falsified" else
                          "🟢" if v == "holding" else "⚪"))
        cols[1].markdown(f"**{it['statement']}**")
        cols[1].caption(f"{it['ticker'] or '—'}　{STATUS_LABELS.get(it['status'], it['status'])}"
                        f"　{STANCE_LABELS.get(it['stance'], it['stance'])}"
                        f"　{'●' * it['conviction']}")
        cols[2].caption(_badge(check))
        if check and check.get("counts"):
            c = check["counts"]
            cols[3].caption(f"命中 {c.get('breached', 0)}"
                            f" / 取不到 {c.get('unknown', 0)} / 共 {c.get('total', 0)}")
        cols[4].caption(it["updated_at"][:10])
        if cols[5].button("详情", key=f"sel_{it['id']}"):
            st.session_state["thesis_sel"] = it["id"]
            st.rerun()

    selected = st.session_state.get("thesis_sel")
    if selected:
        _render_detail(selected)


# ── 新建 ──────────────────────────────────────────────────────

def _apply_draft() -> None:
    draft = st.session_state.pop("thesis_draft", None)
    if not draft:
        return
    st.session_state["tf_statement"] = draft.get("statement", "")
    st.session_state["tf_ticker"] = draft.get("ticker", "")
    st.session_state["tf_stance"] = draft.get("stance", "neutral")
    st.session_state["tf_horizon"] = draft.get("horizon", "")
    st.session_state["tf_conviction"] = int(draft.get("conviction") or 3)
    st.session_state["tf_assumptions"] = "\n".join(draft.get("key_assumptions") or [])
    st.session_state["tf_artifact_pick"] = draft.get("artifact_ref", "")
    rows = draft.get("falsifiers") or []
    st.session_state["tf_falsifiers"] = pd.DataFrame(
        [{"metric": f.get("metric", ""), "op": f.get("op", "<"),
          "value": f.get("value"), "note": f.get("note", "")} for f in rows]
    ) if rows else _empty_falsifiers()
    st.session_state["thesis_draft_notes"] = draft.get("notes") or []
    st.session_state["thesis_draft_warns"] = draft.get("warnings") or []


def _cell(v) -> str:
    """编辑器单元格 → 字符串。pandas 把空值变成 NaN，直接 str() 会得到 "nan"。"""
    if v is None:
        return ""
    if isinstance(v, float) and v != v:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "<na>", "none") else s


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _rows_to_falsifiers(df: pd.DataFrame) -> list[dict]:
    """编辑器里的行 → 接口入参。空指标行丢弃；阈值非数字记为 None 由调用方拦下。"""
    out = []
    for _, row in df.iterrows():
        metric = _cell(row.get("metric"))
        if not metric:
            continue
        out.append({"metric": metric, "op": _cell(row.get("op")) or "<",
                    "value": _num(row.get("value")), "note": _cell(row.get("note"))})
    return out


def _empty_falsifiers() -> pd.DataFrame:
    # 显式 dtype：空表若全是 object，data_editor 的 NumberColumn 会认不出数值列
    return pd.DataFrame({
        "metric": pd.Series(dtype="string"),
        "op": pd.Series(dtype="string"),
        "value": pd.Series(dtype="float64"),
        "note": pd.Series(dtype="string"),
    })


def _new_tab() -> None:
    _apply_draft()

    st.markdown("##### ① 从已有产物生成草稿（可选）")
    st.caption("草稿只抄产物里的真实数字：证伪条件阈值默认取当前值，"
               "论点本身永远留空 —— 那只能你自己写。")
    try:
        arts = _artifacts()
    except api.ApiError as e:
        st.warning(str(e))
        arts = []
    if arts:
        refs = [a["ref"] for a in arts]
        labels = {a["ref"]: f"{a['ticker']} · {a['name']} · {a['modified']}"
                  for a in arts}
        ref = st.selectbox("选择产物", refs, format_func=lambda r: labels[r],
                           key="tf_artifact_pick")
        if st.button("📄 生成草稿"):
            try:
                st.session_state["thesis_draft"] = api.thesis_draft(ref)
                st.rerun()
            except api.ApiError as e:
                st.error(str(e))
    else:
        st.caption("data/artifacts 下还没有产物，先跑一次深度研究。")
        ref = ""

    for n in st.session_state.get("thesis_draft_notes") or []:
        st.caption(f"💡 {n}")
    for w in st.session_state.get("thesis_draft_warns") or []:
        st.warning(w)

    st.markdown("##### ② 写论点与认错线")
    with st.form("thesis_new_form"):
        statement = st.text_area(
            "一句话论点 *", key="tf_statement", height=90,
            placeholder="如：茅台提价周期未结束，2026 年营收增速仍可维持两位数")
        c1, c2, c3, c4 = st.columns([1.2, 1, 1.4, 1])
        ticker = c1.text_input("标的", key="tf_ticker", placeholder="600519.SS")
        stance = c2.selectbox("看法", list(STANCE_LABELS), key="tf_stance",
                              format_func=lambda x: STANCE_LABELS[x])
        horizon = c3.text_input("周期", key="tf_horizon", placeholder="12-18 个月")
        conviction = c4.slider("把握 1-5", 1, 5, key="tf_conviction")

        assumptions = st.text_area(
            "关键假设（一行一条）", key="tf_assumptions", height=90,
            placeholder="飞天出厂价不再上调\n经销商库存低于 1 个月")

        st.markdown("**证伪条件**")
        st.caption("指标写台账路径；末尾 `[]` 表示取最新一期。"
                   "例：`valuation_analysis.dcf.fair_value`、"
                   "`financial_analysis.ratios.GROSS_MARGIN[].value`")
        edited = st.data_editor(
            st.session_state.get("tf_falsifiers", _empty_falsifiers()),
            num_rows="dynamic", use_container_width=True, key="tf_falsifiers",
            column_config={
                "metric": st.column_config.TextColumn("指标路径", width="large",
                                                      required=True),
                "op": st.column_config.SelectboxColumn("比较", options=OPS, required=True),
                "value": st.column_config.NumberColumn("阈值", format="%.6f"),
                "note": st.column_config.TextColumn("备注", width="medium"),
            },
        )

        submitted = st.form_submit_button("保存论点", type="primary")

    if submitted:
        falsifiers = _rows_to_falsifiers(edited)
        bad = [f["metric"] for f in falsifiers if f["value"] is None]
        if bad:
            st.error("这些证伪条件没填数字阈值，无法保存：" + "、".join(bad))
            return
        try:
            tid = api.thesis_create({
                "statement": statement,
                "ticker": ticker,
                "stance": stance,
                "horizon": horizon,
                "key_assumptions": [x.strip() for x in assumptions.splitlines() if x.strip()],
                "falsifiers": falsifiers,
                "conviction": conviction,
                "artifact_ref": ref,
            })
        except api.ApiError as e:
            st.error(str(e))
            return
        for k in ("tf_statement", "tf_ticker", "tf_horizon", "tf_assumptions",
                  "tf_falsifiers", "thesis_draft_notes", "thesis_draft_warns"):
            st.session_state.pop(k, None)
        _list.clear()
        st.session_state["thesis_sel"] = tid
        st.success("已保存，正在打开详情。")
        st.rerun()


def page() -> None:
    st.header("🧾 论点台账")
    st.caption("记录「为什么买」和「什么情况下认错」。证伪条件用真实产物台账做判定，"
               "取不到值就明说取不到 —— 不把「查不到」当成「没事」。")
    t1, t2 = st.tabs(["📋 论点列表", "➕ 新建论点"])
    with t1:
        _list_tab()
    with t2:
        _new_tab()
