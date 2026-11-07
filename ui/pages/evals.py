"""🧪 评测看板 —— 五层评测结果可视化，只读 data/eval/*.json。

三条硬规则（对应计划书「可信层与数据飞轮」的诚实性要求）：
  1 未建的层显示「未建」，不显示 0 —— 0 会被读成「跑过了、结果为零」
  2 口径必须可见：labels=human/synthetic 做成徽章，合成夹具永远标「仅烟囱测试」
  3 缺失指标显示 —，不插补 0

不调后端 API：评测结果是离线落盘的，后端没起也能看。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

EVAL_DIR = ROOT / "data" / "eval"
SCRIPTS_DIR = ROOT / "scripts"

TIERS = ("l0", "l1", "l2", "l3", "l4")
TIER_TITLE = {"l0": "台账闭合性", "l1": "数值接地", "l2": "检索质量",
              "l3": "生成质量", "l4": "端到端任务"}
UNBUILT = "未建"
DASH = "—"

LABEL_TEXT = {"human": "人工标注", "synthetic": "合成夹具"}
LABEL_WARN = {"synthetic": "仅烟囱测试，不构成质量结论"}


# ── 纯函数（守卫脚本直接测这些，不依赖 streamlit 运行时）─────────

def _read(name: str) -> dict | None:
    p = EVAL_DIR / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_all() -> dict:
    return {
        "latest": _read("latest.json"),
        "baseline": _read("baseline.json"),
        "reports": {t: _read(f"{t}_report.json") for t in TIERS},
        "labels": _read("l2_labels.json"),
    }


def _num(v, digits: int = 4) -> str:
    if v is None:
        return DASH
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _pct(v, digits: int = 2) -> str:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return DASH
    return f"{v * 100:.{digits}f}%"


def _badge(labels: str | None) -> str:
    return LABEL_TEXT.get(labels or "", DASH)


def tier_cards(reports: dict) -> list[dict]:
    """每层一张卡。没报告的层 → state=未建，绝不显示 0。"""
    cards = []
    for t in TIERS:
        r = reports.get(t)
        if not r:
            cards.append({"tier": t, "title": TIER_TITLE[t], "state": UNBUILT,
                          "status": None, "checks": DASH, "lines": [],
                          "labels": None})
            continue
        m = r.get("metrics") or {}
        if t == "l0":
            lines = [f"未知路径 {_num(m.get('unknown_count'))}",
                     f"计算占比 {_pct(m.get('computed_rate'))}",
                     f"出处占比 {_pct(m.get('source_rate'))}"]
        elif t == "l1":
            lines = [f"接地率 {_pct(m.get('grounding_rate'))}",
                     f"未接地 {_num(m.get('unmatched'))} 项",
                     f"覆盖 {_num(m.get('prose_fields'))} 字段"]
        elif t == "l2":
            lines = [f"nDCG {_num(m.get('mean_ndcg'))}",
                     f"召回率 {_num(m.get('mean_recall'))}",
                     f"MRR {_num(m.get('mrr'))}"]
        elif t == "l3":
            lines = [f"{_num(m.get('rules'))} 条规则 × 产物",
                     f"通过 {_num(m.get('rules_passed'))}/{_num(m.get('rules_total'))}",
                     f"通过率 {_pct(m.get('pass_rate'), 1)}"]
        else:  # l4 一次性基线：成功 / 耗时 / 降级（token 与明细在报告里）
            lines = [f"成功 {_pct(m.get('success_rate'), 0)}",
                     f"墙钟均值 {_num(m.get('wall_seconds_mean'), 1)}s",
                     f"降级 {_num(m.get('degraded_facts'))} 条"]
        passed, total = r.get("checks_passed"), r.get("checks_total")
        cards.append({
            "tier": t, "title": TIER_TITLE[t],
            "state": (r.get("status") or "unknown").upper(),
            "status": r.get("status"),
            "checks": f"{_num(passed)}/{_num(total)}" if total is not None else DASH,
            "lines": lines[:3],
            "labels": r.get("labels"),
        })
    return cards


def l2_rows(report: dict | None) -> list[dict]:
    """逐查询明细，按召回率升序（最差在前）。"""
    if not report:
        return []
    ndcg = report.get("per_query") or {}
    recall = report.get("per_query_recall") or {}
    mrr = report.get("per_query_mrr") or {}
    retrieved = report.get("retrieved") or {}
    rows = []
    for q in ndcg:
        rows.append({
            "查询": q,
            "nDCG": ndcg.get(q, DASH),
            "Recall": recall.get(q, DASH),
            "RR": mrr.get(q, DASH),
            "召回片段": len(retrieved.get(q) or []),
        })
    rows.sort(key=lambda r: (r["Recall"] if isinstance(r["Recall"], (int, float)) else 9,
                            r["nDCG"] if isinstance(r["nDCG"], (int, float)) else 9))
    return rows


def doc_sources() -> dict[str, str]:
    """真实 id → 来源文件。标注表底稿里有短号与来源，借它还原，只读不改。"""
    meta = _read("l2_label_sheet.meta.json")
    if not meta:
        return {}
    short_map = meta.get("short_map") or {}
    out: dict[str, str] = {}
    for item in meta.get("items") or []:
        for row in item.get("rows") or []:
            real = short_map.get(row.get("short") or "")
            if real:
                out[real] = row.get("source") or ""
    return out


def missed_rows(report: dict | None, labels: dict | None,
                sources: dict[str, str] | None = None) -> list[dict]:
    """判为相关（≥2 分）却没进 top-k 的文档 —— 漏召回的实证。"""
    if not report or not labels:
        return []
    sources = sources or {}
    retrieved = report.get("retrieved") or {}
    rows = []
    for item in labels.get("items") or []:
        q = item.get("query") or ""
        got = set(retrieved.get(q) or [])
        for doc_id, grade in (item.get("relevance") or {}).items():
            if grade >= 2 and doc_id not in got:
                rows.append({"查询": q, "文档": doc_id[:8],
                             "来源": sources.get(doc_id) or DASH,
                             "人工分": grade})
    rows.sort(key=lambda r: -r["人工分"])
    return rows


def history_series() -> dict[str, list[dict]]:
    """history.jsonl → 逐层时间序列。metrics 缺失的指标不补点。"""
    path = EVAL_DIR / "history.jsonl"
    out: dict[str, list[dict]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.setdefault(rec.get("tier") or "?", []).append({
                "ts": rec.get("generated_at"),
                "labels": rec.get("labels"),
                "metrics": rec.get("metrics") or {},
            })
    return out


def drift_rows(latest: dict | None, baseline: dict | None) -> list[dict] | None:
    """复用 run_eval 的比对逻辑（单一事实来源），None = 缺文件无法比对。"""
    if not latest or not baseline:
        return None
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import run_eval
    return run_eval._compare(latest.get("tiers") or {}, baseline)


# 护栏指标：键 → 展示标题。口径与 /api/v1/metrics 完全一致（同一函数算）。
GUARDRAILS = (("grounding_rate", "接地率"), ("degraded_rate", "降级率"),
              ("llm_error_rate", "LLM 错误率"),
              ("feedback_negative_rate", "👎 占比"),
              ("feedback_coverage", "反馈覆盖率"))


def flywheel_rows() -> dict:
    """5 个护栏指标。LLM 错误率来自后端进程内计数：后端没起就取不到，如实显示 —。"""
    from app.observability.guardrails import snapshot

    counters: dict = {}
    try:
        from ui import api_client as api
        counters = api.metrics().get("counters") or {}
    except Exception:  # noqa: BLE001 后端不可用不影响离线指标与反馈统计
        counters = {}
    return snapshot(counters)


def label_switch(series: dict[str, list[dict]]) -> str | None:
    """检索层第一条 labels=human 的时间戳（口径切换点）。"""
    for pt in series.get("l2") or []:
        if (pt.get("labels") or "").lower() == "human":
            return pt.get("ts")
    return None


# ── 渲染 ────────────────────────────────────────────────────────

def _status_icon(state: str) -> str:
    return {"PASS": "✅", "FAIL": "❌", UNBUILT: "⚪"}.get(state, "⚠️")


def _render_cards(cards: list[dict]) -> None:
    cols = st.columns(len(cards))
    for col, c in zip(cols, cards):
        with col:
            st.markdown(f"**{c['tier'].upper()} {c['title']}**")
            if c["state"] == UNBUILT:
                st.markdown(f"⚪ **{UNBUILT}**")
                st.caption("尚未构建，不计入总体")
                continue
            st.markdown(f"{_status_icon(c['state'])} **{c['state']}**  {c['checks']}")
            for line in c["lines"]:
                st.caption(line)
            if c["tier"] == "l2":
                st.caption(f"口径：{_badge(c['labels'])}")


def _render_flywheel() -> None:
    """飞轮健康度条：3 个质量护栏 + 2 个飞轮健康度。"""
    g = flywheel_rows()
    st.markdown("#### 🌀 飞轮健康度")
    cols = st.columns(len(GUARDRAILS))
    for col, (key, title) in zip(cols, GUARDRAILS):
        col.metric(title, _pct(g.get(key)))
    meta = g.get("meta") or {}
    as_of = (meta.get("grounding_rate") or {}).get("as_of") or DASH
    st.caption(f"口径：接地率 / 降级率 = 离线评测（{as_of}）；LLM 错误率 / 👎 占比 / "
               "反馈覆盖率 = 实时（进程重启不丢反馈，LLM 计数会清零）。"
               "后端未起时 LLM 错误率显示 —。")
    st.caption("分母为 0 显示 —，不显示 0%：「一次都没评」和「评了但全坏」是两回事。"
               "本机单用户、样本个位数，这些值只说明闭环有没有在转，不构成质量结论。")


def _to_dt(ts):
    """ISO 字符串 → datetime。plotly 画竖线时会对字符串 x 取平均而报错。"""
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return ts


def _trend_chart(series: dict[str, list[dict]], metric: str, title: str,
                 switch: str | None) -> None:
    import plotly.graph_objects as go

    fig = go.Figure()
    drew = False
    for tier, pts in sorted(series.items()):
        xs = [_to_dt(p["ts"]) for p in pts
              if isinstance(p["metrics"].get(metric), (int, float))]
        ys = [p["metrics"][metric] for p in pts
              if isinstance(p["metrics"].get(metric), (int, float))]
        if not xs:
            continue
        drew = True
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines+markers", name=tier.upper()))
    if switch and drew:
        # 不用 add_vline：它对非数值轴会先求均值，日期轴直接抛 TypeError
        x = _to_dt(switch)
        fig.add_shape(type="line", x0=x, x1=x, yref="paper", y0=0, y1=1,
                      line=dict(dash="dash", color="#999"))
        fig.add_annotation(x=x, y=1, yref="paper", showarrow=False, yshift=8,
                           text="口径切换：合成→人工", font=dict(size=10))
    fig.update_layout(title=title, height=280, margin=dict(l=10, r=10, t=40, b=10),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))
    if drew:
        st.plotly_chart(fig, width="stretch")
    else:
        st.info(f"history.jsonl 里没有 {metric} 的真实记录，不画空图")


def _render_l2(report: dict | None, labels: dict | None) -> None:
    if not report:
        st.info("还没有 L2 报告：先跑 scripts/eval_retrieval.py")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("nDCG@4", _num(report.get("mean_ndcg")))
    c2.metric("召回率 Recall@4", _num(report.get("mean_recall")))
    c3.metric("MRR", _num(report.get("mrr")))
    c4.metric("漏召回", f"{_num(report.get('missed_relevant', 0))} 篇")

    lbl = (report.get("labels") or "").lower()
    st.markdown(f"**标注口径：{_badge(lbl)}**  ·  {_num(report.get('queries'))} 条查询"
                f"  ·  top-{_num(report.get('top_k'))}")
    if lbl == "synthetic":
        st.warning(LABEL_WARN["synthetic"])
    if report.get("recall_saturated"):
        st.warning("召回率已饱和（k 覆盖整个语料），该值不度量任何东西")
    if report.get("note"):
        st.caption(report["note"])
    if lbl == "human":
        st.caption("召回率为池化下界估计：标注池 = top-k + 盲抽 2 篇，语料里未标到的按 0 计，"
                   "只会低估不会高估。")
        st.caption(f"语料为只读快照 {report.get('db') or 'data/knowledge_db_v1'}"
                   f"（{_num(report.get('corpus_total'))} 切片，其中 1,907 为管线自产"
                   "历史报告）—— 本测评度量的是「能否找回自己的历史分析」，不是"
                   "「能否找到外部研报」；外部语料只进正式库，不入 v1 快照。")

    rows = l2_rows(report)
    if rows:
        st.markdown("#### 逐查询明细（召回率升序，最差在前）")
        st.dataframe(rows, width="stretch", hide_index=True)

    miss = missed_rows(report, labels, doc_sources())
    if miss:
        st.markdown(f"#### 漏召回清单（{len(miss)} 篇）")
        st.caption("判为相关（≥2 分）却没进 top-k —— 这是检索真实的失败，不是出题错误。")
        st.dataframe(miss, width="stretch", hide_index=True)
    elif labels:
        st.success("没有漏召回：标注为相关的文档全部进了 top-k")


def _render_tier_detail(reports: dict) -> None:
    built = [t for t in TIERS[:4] if reports.get(t)]
    if not built:
        st.info("还没有任何分层报告")
        return
    tier = st.selectbox("层", built, format_func=lambda t: f"{t.upper()} {TIER_TITLE[t]}")
    report = reports[tier]
    arts = report.get("artifacts") or []
    if not arts:
        st.json({k: v for k, v in (report.get("metrics") or {}).items()})
        return
    idx = st.selectbox(
        "产物", range(len(arts)),
        format_func=lambda i: f"{arts[i].get('ticker') or '?'} · {arts[i].get('run_id') or ''}"
                              f" · {arts[i].get('status')}")
    art = arts[idx]
    m = art.get("metrics") or {}
    if m:
        st.dataframe([{"指标": k, "值": _num(v)} for k, v in m.items()],
                     width="stretch", hide_index=True)
    checks = art.get("checks") or []
    bad = [c for c in checks if not c.get("passed")]
    good = [c for c in checks if c.get("passed")]
    st.markdown(f"#### 断言 {len(good)} 通过 / {len(bad)} 失败")
    if bad:
        st.error("失败项")
        st.dataframe([{"规则": c.get("name"), "明细": c.get("detail") or ""} for c in bad],
                     width="stretch", hide_index=True)
    if good:
        with st.expander(f"通过项（{len(good)}）"):
            st.dataframe([{"规则": c.get("name"), "明细": c.get("detail") or ""}
                          for c in good], width="stretch", hide_index=True)
    misses = art.get("misses") or []
    if misses:
        st.markdown("#### 未接地明细")
        st.dataframe(misses, width="stretch", hide_index=True)


def _render_baseline(latest: dict | None, baseline: dict | None) -> None:
    if not baseline:
        st.info("还没有基线：跑 scripts/run_eval.py --save-baseline")
        return
    st.caption(f"基线建于 {baseline.get('created_at') or DASH} · "
               f"git {baseline.get('git_sha') or DASH} · "
               f"比率容差 {baseline.get('rate_tolerance', DASH)}")
    drifts = drift_rows(latest, baseline)
    if drifts is None:
        st.info("缺少 latest.json：跑 scripts/run_eval.py --json")
        return
    if not drifts:
        st.success("无退化：所有受保护指标都在容差内")
    else:
        st.error(f"检出 {len(drifts)} 条指标退化")
        st.dataframe([{"层": d["tier"].upper(), "指标": d["metric"],
                       "基线": d["baseline"], "当前": d["current"],
                       "变化": f"{d['delta']:+}", "规则": d["rule"]} for d in drifts],
                     width="stretch", hide_index=True)
    st.caption("受保护指标：所有 *_rate 比率、unknown_count（不得增长），"
               "以及 L2 的 mean_ndcg / mean_recall / mrr")


def _render_history() -> None:
    series = history_series()
    if not series:
        st.info("还没有 history.jsonl")
        return
    switch = label_switch(series)
    if switch:
        st.caption(f"口径切换点：{switch}（之前是合成夹具，之后是人工标注）")
    _trend_chart(series, "mean_recall", "检索召回率 Recall@k", switch)
    _trend_chart(series, "mean_ndcg", "检索排序质量 nDCG@k", switch)
    _trend_chart(series, "grounding_rate", "数值接地率", None)
    n = sum(len(v) for v in series.values())
    st.caption(f"共 {n} 条历史记录。缺失的指标不插补成 0，直接断开。")


def page() -> None:
    st.markdown("## 🧪 可信层与五层评测")
    data = load_all()
    latest = data["latest"]
    if latest:
        st.caption("全部离线跑在落盘产物上 · "
                   f"生成于 {latest.get('generated_at') or DASH} · "
                   f"git {latest.get('git_sha') or DASH} · "
                   f"模型 {latest.get('model') or DASH}")
        l4 = (data["reports"] or {}).get("l4")
        l4_note = (f"L4 一次性基线 {(l4.get('status') or 'unknown').upper()}（不进日常门禁）"
                   if l4 else "L4 端到端未建成基线")
        st.caption(f"总体 **{(latest.get('status') or 'unknown').upper()}** · "
                   f"请求层 {', '.join(latest.get('tier_requested') or []) or DASH} · "
                   f"{l4_note}")
    else:
        st.warning("还没有 latest.json：跑 scripts/run_eval.py --json")

    cards = tier_cards(data["reports"])
    _render_cards(cards)
    st.divider()
    _render_flywheel()
    st.divider()

    tab1, tab2, tab3, tab4 = st.tabs(
        ["🔍 检索质量", "📋 分层明细", "📉 基线对比", "📈 历史趋势"])
    with tab1:
        _render_l2(data["reports"].get("l2"), data["labels"])
    with tab2:
        _render_tier_detail(data["reports"])
    with tab3:
        _render_baseline(latest, data["baseline"])
    with tab4:
        _render_history()
