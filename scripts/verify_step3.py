"""Step 3 验证：检索器 + 管线接入。

运行: D:\\python\\python.exe scripts\\verify_step3.py [--e2e]
  --e2e  追加真实跑 600519 深度研究（验证 [1.5/7] 进度 + result.knowledge）
"""
from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

# RAG 分支验证需启用；get_settings 为进程内 lru_cache 单例，须在 import app 前设置
os.environ["RAG_ENABLED"] = "true"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402

from app.rag.retriever import KnowledgeRetriever  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def unit() -> None:
    r = KnowledgeRetriever()

    # 1. 检索器可用性
    check("知识库检索器可用", r.available)

    # 2. invoke 返回结构（对齐 build_rag_context 期望）
    res = r.invoke("贵州茅台 白酒 营收 净利润 增长", where={"ticker": "600519.SS"}, top_k=3)
    ok_struct = (
        bool(res)
        and hasattr(res[0], "page_content")
        and bool(res[0].page_content)
        and res[0].metadata.get("score") is not None
    )
    meta_keys = {"score", "section", "source", "ticker", "date", "kind"}
    ok_meta = ok_struct and meta_keys.issubset(res[0].metadata.keys())
    check("invoke 返回 SimpleNamespace 结构", ok_struct,
          f"n={len(res)}")
    check("metadata 含 score/section/source/ticker/date/kind", ok_meta,
          f"keys={sorted(res[0].metadata.keys())}")

    # 3. query_for_ticker 多路融合：五路各自取到候选，最终榜是这些候选的子集。
    #    外部三类（披露/政策/论文）预留 1 个位置：它们的语义距离天然更远，按分数
    #    排序会被存量研报整体挤掉（实测 AAPL 披露路最高 0.5573 < 第 5 名研报 0.6002），
    #    所以这两件事分开断言：结果必须是候选子集，外部三类候选存在时必须各占 1 位。
    from app.rag.retriever import (  # noqa: E501
        _DISCLOSURE_QUERY, _MACRO_DATA_QUERY, _MACRO_NEWS_QUERY,
        _PAPER_QUERY, _POLICY_QUERY, _RESERVED_KINDS,
    )

    union: dict[str, object] = {}
    for _q, _w, _k in [
        ("600519.SS 白酒 财务状况 行业分析 新闻 公告", {"ticker": "600519.SS"}, 5),
        (f"白酒 {_MACRO_DATA_QUERY}", {"kind": "宏观数据"}, 2),
        (_MACRO_NEWS_QUERY, {"kind": "宏观新闻"}, 2),
        (f"600519.SS 白酒 {_DISCLOSURE_QUERY}",
         {"ticker": "600519.SS", "kind": "披露文件"}, 1),
        (f"白酒 {_POLICY_QUERY}", {"kind": "政策"}, 1),
        (f"白酒 {_PAPER_QUERY}", {"kind": "论文"}, 1),
    ]:
        for d in r.invoke(_q, where=_w, top_k=_k):
            union[d.id or d.page_content] = d
    union_kinds = sorted({d.metadata.get("kind") for d in union.values()})
    check("各路等值路由各自取到候选（≥2 类 kind）", len(union_kinds) >= 2,
          f"kinds={union_kinds} n={len(union)}")

    docs = r.query_for_ticker("600519.SS", "白酒", top_k=5)
    got = {d.metadata.get("kind") for d in docs}
    check("query_for_ticker 结果是各路候选的子集",
          bool(docs) and len(docs) <= 5
          and all((d.id or d.page_content) in union for d in docs),
          f"n={len(docs)} kinds={sorted(got)}")
    have = {k for k in _RESERVED_KINDS if k in union_kinds}
    check("外部三类候选存在时各占 1 位", have <= got,
          f"应有={sorted(have)} 实得={sorted(got)}")

    # 4. 结果按 score 降序
    scores = [d.metadata.get("score") for d in docs]
    ok_desc = scores == sorted(scores, reverse=True)
    check("结果按 score 降序", ok_desc, f"scores={scores}")

    # 5. 时间衰减：近文档加分，远文档不加分
    today = datetime.date.today()
    recent = KnowledgeRetriever._apply_time_decay(0.5, today.isoformat())
    old = KnowledgeRetriever._apply_time_decay(0.5, (today - datetime.timedelta(days=400)).isoformat())
    invalid = KnowledgeRetriever._apply_time_decay(0.5, "")
    check("时间衰减: 近180天加分/更早不加分/空日期不加分",
          recent > 0.5 and old == 0.5 and invalid == 0.5,
          f"recent={recent:.4f} old={old} invalid={invalid}")

    # 6. 外部语料两路（政策 / 论文）：离线替身，不碰真库
    _check_external_lanes()

    # 7. pipeline 集成静态检查
    src = Path(__file__).resolve().parent.parent / "app" / "analysis" / "pipeline.py"
    text = src.read_text(encoding="utf-8")
    check("pipeline 含 [1.5/7] 知识库检索步骤", '"1.5/7"' in text and "知识库检索" in text)
    check("pipeline result 追加 knowledge 字段", '"knowledge": knowledge' in text)
    check("pipeline SWOT 注入用 rag_retriever", "rag_retriever" in text and "build_rag_context" in text)


class _FakeStore:
    """离线替身：记录每次 where 过滤，只返回预置行，用来证明路由真发出去了。"""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.filters: list[dict] = []

    def available(self) -> bool:
        return True

    def count(self, where=None) -> int:
        return len(self.rows)

    def all_docs(self, limit: int | None = 10000,
                 where: dict | None = None) -> list[dict]:
        return []

    def query(self, text: str, where: dict | None = None, top_k: int = 5) -> list[dict]:
        self.filters.append(dict(where or {}))
        return [
            {"id": r["id"], "score": r["score"],
             "page_content": r["page_content"], "metadata": dict(r["metadata"])}
            for r in self.rows
            if all(r["metadata"].get(k) == v for k, v in (where or {}).items())
        ]


def _row(rid: str, kind: str, ticker: str, score: float,
         corpus: str = "", uid: str = "") -> dict:
    meta = {"kind": kind, "ticker": ticker, "source": "test",
            "date": "2026-01-01", "section": "正文"}
    if corpus:
        meta["corpus"] = corpus
    if uid:
        meta["doc_uid"] = uid
    return {"id": rid, "score": score, "page_content": f"{kind}正文 {rid}",
            "metadata": meta}


def _check_external_lanes() -> None:
    """政策/论文两路：等值路由发出、各只出 1 条、无对应语料时不报错。"""
    rows = [
        _row("t1", "研报", "600519.SS", 0.90),
        _row("p1", "政策", "", 0.30),
        _row("p2", "政策", "", 0.29),
        _row("p3", "政策", "", 0.28),
        _row("a1", "论文", "", 0.20),
    ]
    fake = _FakeStore(rows)
    docs = KnowledgeRetriever(store=fake, top_k=5).query_for_ticker("600519.SS", "白酒", top_k=5)
    kinds = [d.metadata.get("kind") for d in docs]
    check("政策/论文两路各发出等值 where",
          {"kind": "政策"} in fake.filters and {"kind": "论文"} in fake.filters,
          f"filters={fake.filters}")
    check("政策/论文各只出 1 条", kinds.count("政策") == 1 and kinds.count("论文") == 1,
          f"kinds={kinds}")
    check("两路并入后仍按 score 降序且不超 top_k",
          [d.metadata["score"] for d in docs]
          == sorted([d.metadata["score"] for d in docs], reverse=True) and len(docs) <= 5,
          f"n={len(docs)}")

    # 语料里没有政策/论文时（v1 快照即此状态）：两路取空，不能影响其余结果
    bare = _FakeStore([_row("t1", "研报", "600519.SS", 0.90)])
    only = KnowledgeRetriever(store=bare, top_k=5).query_for_ticker("600519.SS", "白酒", top_k=5)
    check("无政策/论文语料时两路取空不报错",
          len(only) == 1 and only[0].id == "t1",
          f"n={len(only)}")

    # 预留位：政策候选分很低（0.1），top_k=2 时本来是 0.9/0.8 两条研报，
    # 必须由它替换掉最低位那条研报，而不是被分数切掉
    cramped = _FakeStore([
        _row("t1", "研报", "600519.SS", 0.90),
        _row("t2", "研报", "600519.SS", 0.80),
        _row("p1", "政策", "", 0.10),
    ])
    got2 = KnowledgeRetriever(store=cramped, top_k=2).query_for_ticker(
        "600519.SS", "白酒", top_k=2)
    ids2 = [d.id for d in got2]
    check("低分外部类仍占 1 位（替换最低位非外部条目）",
          "p1" in ids2 and len(got2) == 2 and ids2[0] == "t1",
          f"ids={ids2}")

    # 配额 A：一份年报切出几百块，个股路最多留 2 条，余位让给别的文档
    flood = [_row(f"e{i}", "披露文件", "600519.SS", 0.90 - i * 0.01,
                  corpus="external", uid="1225114741") for i in range(4)]
    flood.append(_row("e9", "披露文件", "600519.SS", 0.50,
                      corpus="external", uid="1222993920"))
    got = KnowledgeRetriever(store=_FakeStore(flood), top_k=5).query_for_ticker(
        "600519.SS", "白酒", top_k=5)
    uids = [d.metadata.get("doc_uid") for d in got]
    check("单篇外部文档最多占 2 个位置",
          uids.count("1225114741") == 2 and "1222993920" in uids and len(got) == 3,
          f"uids={uids}")


def e2e() -> None:
    print("\n-- e2e: 真实跑 600519 深度研究 --")
    from app.analysis.pipeline import AnalysisOrchestrator

    steps: list[dict] = []

    def cb(p: dict):
        steps.append(p)
        print(f"  [{p.get('step')}] {p.get('label')}: {p.get('message')}")

    result = AnalysisOrchestrator().run("600519.SS", progress_cb=cb)
    step_ids = [s.get("step") for s in steps]
    check("进度含 [1.5/7] 知识库检索", "1.5/7" in step_ids,
          f"steps={step_ids}")
    know = result.get("knowledge", {})
    check("knowledge.available=True", know.get("available") is True,
          f"reason={know.get('reason')}")
    check("knowledge 含 snippets", bool(know.get("snippets")),
          f"count={know.get('count')} source={know.get('source')}")
    top = know.get("snippets", [{}])[0]
    print("  [top snippet] kind=%s ticker=%s source=%s"
          % (top.get("metadata", {}).get("kind"),
             top.get("metadata", {}).get("ticker"),
             top.get("metadata", {}).get("source")))
    print("  ", str(top.get("content", ""))[:80].replace("\n", " "))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--e2e", action="store_true", help="追加真实深度研究验证")
    args = ap.parse_args()

    unit()
    if args.e2e:
        try:
            e2e()
        except Exception as ex:
            check("e2e 真实研究", False, f"异常: {ex}")

    print("=" * 50)
    print(f"Step 3 验证: {passed}/{passed + failed} 通过")
    sys.exit(0 if failed == 0 else 1)
