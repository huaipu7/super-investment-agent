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

    # 3. query_for_ticker 混合检索（研报 + 宏观）
    docs = r.query_for_ticker("600519.SS", "白酒", top_k=5)
    kinds = {d.metadata.get("kind") for d in docs}
    ok_mix = (
        bool(docs)
        and "研报" in kinds
        and any(k in ("宏观数据", "宏观新闻") for k in kinds)
    )
    check("query_for_ticker 混合检索（研报+宏观）", ok_mix,
          f"kinds={sorted(kinds)} n={len(docs)}")

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

    # 6. pipeline 集成静态检查
    src = Path(__file__).resolve().parent.parent / "app" / "analysis" / "pipeline.py"
    text = src.read_text(encoding="utf-8")
    check("pipeline 含 [1.5/7] 知识库检索步骤", '"1.5/7"' in text and "知识库检索" in text)
    check("pipeline result 追加 knowledge 字段", '"knowledge": knowledge' in text)
    check("pipeline SWOT 注入用 rag_retriever", "rag_retriever" in text and "build_rag_context" in text)


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
