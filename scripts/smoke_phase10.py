"""
Phase 10 冒烟测试：RAG 知识库（Chroma + bge-small-zh-v1.5）接入深度研究。

覆盖：
  - 知识库：store.available / 总量 > 0 / query 命中且 metadata 含六键
  - 三类源入库计数：研报 / 宏观数据 / 宏观新闻 非空（其余 kind 打印观测值）
  - KnowledgeRetriever.invoke 返回 SimpleNamespace(page_content + metadata{score,section,source,ticker,date,kind})
  - 结果按 score 降序；时间衰减近文档加分/远文档不加分
  - SWOT RAG 上下文：build_rag_context 含「[片段」「(来源: 」标记
  - 深度研究：SSE 进度含 [1.5/7] 知识库检索；result.knowledge.available=True 且 snippets 结构完整
  - 报告：归档 md 含 ## 九、知识库参考；一~八章仍在（smoke_phase9 兼容）

运行: PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\smoke_phase10.py
"""
import datetime
import json
import os
import sys
from pathlib import Path

# RAG 分支验证需启用；get_settings 为进程内 lru_cache 单例，须在 import app 前设置
os.environ["RAG_ENABLED"] = "true"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from app.analysis.swot import SwotAnalyzer  # noqa: E402
from app.rag.retriever import KnowledgeRetriever  # noqa: E402
from app.rag.store import get_knowledge_store  # noqa: E402

_results: list[dict] = []
_META_KEYS = {"score", "section", "source", "ticker", "date", "kind"}
# store.query 原始 metadata（score 为顶层字段，由 retriever 注入 metadata）
_STORE_META_KEYS = {"kind", "ticker", "date", "source", "section", "url"}


def record(name: str, ok: bool, detail: str = ""):
    _results.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def parse_sse(lines) -> list[dict]:
    """解析 SSE 行流为 [{event, data}]（事件间以空行分隔）"""
    blocks = []
    buf = None
    for line in lines:
        line = line.strip()
        if line.startswith("event:"):
            buf = {"event": line.split(":", 1)[1].strip()}
        elif line.startswith("data:") and buf is not None:
            try:
                buf["data"] = json.loads(line.split(":", 1)[1].strip())
            except Exception:
                buf["data"] = line.split(":", 1)[1].strip()
        elif not line and buf is not None:
            blocks.append(buf)
            buf = None
    if buf is not None:
        blocks.append(buf)
    return blocks


client = TestClient(app)


# ── 知识库 ─────────────────────────────────────────────────────

def check_knowledge_store():
    print("\n=== 知识库（store） ===")
    store = get_knowledge_store()
    record("store.available", store.available())

    total = store.count()
    record("知识库总量 > 0", total > 0, f"count={total}")

    res = store.query("贵州茅台 白酒 营收 净利润 增长", top_k=3)
    ok = (
        bool(res)
        and bool(res[0].get("page_content"))
        and res[0].get("score") is not None
        and _STORE_META_KEYS.issubset(res[0].get("metadata", {}).keys())
    )
    record("query 命中且 metadata 含六键 + score", ok, f"n={len(res)}")


def check_kind_counts():
    print("\n=== 三类源入库计数 ===")
    store = get_knowledge_store()
    counts = {k: store.count({"kind": k}) for k in
              ("研报", "宏观数据", "宏观新闻", "新闻", "公告", "股东大会")}
    ok = counts["研报"] > 0 and counts["宏观数据"] > 0 and counts["宏观新闻"] > 0
    record("研报/宏观数据/宏观新闻 均非空", ok, f"研报={counts['研报']} "
          f"宏观数据={counts['宏观数据']} 宏观新闻={counts['宏观新闻']}")
    print("   其余 kind 观测:", {k: v for k, v in counts.items() if k not in ("研报", "宏观数据", "宏观新闻")})


# ── 检索器 ─────────────────────────────────────────────────────

def check_retriever():
    print("\n=== 检索器 ===")
    r = KnowledgeRetriever()
    record("retriever.available", r.available)

    docs = r.invoke("贵州茅台 白酒 行业 财务 增长", where={"ticker": "600519.SS"}, top_k=3)
    ok = (
        bool(docs)
        and hasattr(docs[0], "page_content")
        and bool(docs[0].page_content)
        and _META_KEYS.issubset(docs[0].metadata.keys())
        and docs[0].metadata.get("score") is not None
    )
    record("invoke 返回 SimpleNamespace 结构", ok, f"n={len(docs)}")

    scores = [d.metadata.get("score") for d in docs]
    record("结果按 score 降序", scores == sorted(scores, reverse=True), f"scores={scores}")

    today = datetime.date.today()
    recent = KnowledgeRetriever._apply_time_decay(0.5, today.isoformat())
    old = KnowledgeRetriever._apply_time_decay(0.5,
        (today - datetime.timedelta(days=400)).isoformat())
    invalid = KnowledgeRetriever._apply_time_decay(0.5, "")
    record("时间衰减: 近180天加分/更早不加分/空日期不加分",
          recent > 0.5 and old == 0.5 and invalid == 0.5,
          f"recent={recent:.4f} old={old} invalid={invalid}")

    mixed = r.query_for_ticker("600519.SS", "白酒", top_k=5)
    kinds = {d.metadata.get("kind") for d in mixed}
    record("query_for_ticker 混合检索（≥2 类 kind）", len(kinds) >= 2,
          f"kinds={sorted(kinds)} n={len(mixed)}")

    ctx = SwotAnalyzer().build_rag_context(r, "贵州茅台 白酒 财务状况 行业分析")
    ok_ctx = ctx and "来源:" in ctx and "[片段 1]" in ctx
    record("SWOT RAG 上下文含来源标记", ok_ctx,
          f"len={len(ctx)} chars" if ctx else "空")


# ── 深度研究 + SSE + 报告（约 1-2 分钟）────────────────────────

def check_pipeline_sse():
    print("\n=== 深度研究：SSE 进度 + knowledge + 报告九章（600519） ===")
    r = client.post("/api/v1/analysis/run/600519")
    record("分析启动 pending", r.status_code == 200 and r.json().get("status") == "pending",
           str(r.json()))

    with client.stream("GET", "/api/v1/analysis/stream/600519?timeout=600") as resp:
        events = parse_sse(resp.iter_lines())
    ev_names = [e["event"] for e in events]
    prog = [e["data"] for e in events if e["event"] == "progress"]
    steps = [p.get("step") for p in prog]
    labels = [p.get("label") for p in prog]

    ok_events = ("progress" in ev_names and "status" in ev_names and events[-1]["event"] == "done")
    ok_steps = "1.5/7" in steps and "done" in steps
    status_ev = next((e["data"] for e in events if e["event"] == "status"), {})
    ok_term = status_ev.get("status") == "completed"
    record("SSE 事件序列 + [1.5/7] 知识库检索", ok_events and ok_steps and ok_term,
          f"步骤={steps}")
    print("   阶段:", " → ".join(labels))

    r = client.get("/api/v1/analysis/result/600519")
    j = r.json()
    know = j.get("knowledge") or {}
    ok_avail = know.get("available") is True
    record("knowledge.available=True", ok_avail, f"reason={know.get('reason')}")
    snips = know.get("snippets") or []
    ok_snips = (
        len(snips) > 0
        and all(isinstance(s, dict) and s.get("content") for s in snips)
        and all(_META_KEYS.issubset((s.get("metadata") or {}).keys()) for s in snips)
    )
    record("knowledge.snippets 结构完整", ok_snips, f"count={know.get('count')} "
          f"source={know.get('source')}")
    kkinds = {s.get("metadata", {}).get("kind") for s in snips}
    print("   片段 kinds:", sorted(kkinds))
    top = snips[0] if snips else {}
    print("   top 片段:", str(top.get("metadata", {}).get("source")),
          f"score={top.get('metadata', {}).get('score')}")
    print("   ", str(top.get("content", ""))[:70].replace("\n", " "))

    # 报告：归档 md 含九章，且一~八章仍在（smoke_phase9 兼容）
    r = client.post("/api/v1/analysis/report/600519")
    rep = r.json()
    md_path = rep.get("report", {}).get("markdown")
    record("报告生成", r.status_code == 200 and md_path,
          f"{rep.get('charts_count')} 图, 格式={list(rep.get('report', {}).keys())}")
    if md_path:
        name = Path(md_path).name
        r = client.get("/api/v1/reports/download/" + name)
        content = r.content.decode("utf-8", errors="replace")
        ok9 = "## 九、知识库参考" in content
        ok_ch = all(f"## {c}、" in content for c in ("一", "二", "三", "四", "五", "六", "七", "八"))
        record("归档 md 含九章 + 一~八章完整", ok9 and ok_ch, f"九={ok9} 一~八={ok_ch}")
        seg = content[content.index("## 九、知识库参考"):]
        print("   ── 九章样例 ──")
        print(seg[:400].strip())


if __name__ == "__main__":
    check_knowledge_store()
    check_kind_counts()
    check_retriever()
    check_pipeline_sse()

    passed = sum(1 for r in _results if r["ok"])
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"Phase 10 结果: {passed}/{total} 通过")
    for r in _results:
        if not r["ok"]:
            print(f"  FAIL: {r['name']} {r['detail']}")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)
