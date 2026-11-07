"""Step 5 验证：看板层数据链路（Phase 10 无需改 UI）。

看板消费的两条数据路径：
  1. SSE 进度：routes_analysis 逐条转发 task.progress（含 [1.5/7] 知识库检索），
     ui/pages/research.py 用 progress 事件的 label 作为进度条文本 → 「知识库检索」自动显示。
  2. 报告渲染：result_to_markdown → ReportGenerator 归档 md → research.py 读取归档文件
     st.markdown 渲染 → 九章自动出现。

运行: D:\\python\\python.exe scripts\\verify_step5.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.report.generator import ReportGenerator  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _render_head(text: str, n: int = 20) -> str:
    """片段落到归档 md 后开头长什么样（对齐 markdown.py 的 strip + 换行转空格）。"""
    return str(text or "").strip().replace("\n", " ")[:n]


def code_checks() -> None:
    ui = (ROOT / "ui" / "pages" / "research.py").read_text(encoding="utf-8")
    ra = (ROOT / "api" / "routes_analysis.py").read_text(encoding="utf-8")
    pl = (ROOT / "app" / "analysis" / "pipeline.py").read_text(encoding="utf-8")

    check("UI 进度条用 SSE label 文本（步骤名自动显示）", "data.get('label'" in ui)
    check("UI 报告读取归档 md 渲染（九章自动出现）",
          "st.markdown(md)" in ui and "REPORT_DIR" in ui)
    check("SSE 逐条转发 task.progress", 'yield _sse_event("progress", ev)' in ra)
    check("管线发射 [1.5/7] 知识库检索",
          '"1.5/7"' in pl and "知识库检索" in pl)
    check("管线 result 含 knowledge 字段", '"knowledge": knowledge' in pl)


def archive_with_real_rag() -> None:
    """用真实检索数据走 ReportGenerator 归档，断言归档 md 含九章。"""
    from app.rag.retriever import get_knowledge_retriever

    retriever = get_knowledge_retriever()
    check("真实检索器可用（Chroma + bge）", retriever.available)
    if not retriever.available:
        return

    docs = retriever.query_for_ticker("600519.SS", "白酒", top_k=5)
    check("真实检索到片段", bool(docs), f"n={len(docs)}")
    if not docs:
        return

    result = {
        "ticker": "600519.SS",
        "company_info": {"name": "贵州茅台", "industry": "白酒",
                         "country": "中国", "currency": "CNY"},
        "financial_analysis": {},
        "valuation_analysis": {},
        "sotp_analysis": {},
        "market_analysis": {},
        "competitive_analysis": {},
        "news_analysis": {"available": False},
        "meeting_analysis": {"available": False},
        "swot_analysis": {},
        "geopolitical_analysis": {},
        "supply_chain_analysis": {},
        "fact_checks": {},
        "knowledge": {
            "available": True,
            "source": "RAG 知识库 (Chroma + bge-small-zh-v1.5)",
            "count": len(docs),
            "snippets": [{"content": d.page_content, "metadata": d.metadata} for d in docs],
        },
    }

    with tempfile.TemporaryDirectory() as tmp:
        gen = ReportGenerator(output_dir=tmp)
        paths = gen.generate(result, fmt="md")
        md_path = Path(paths["markdown"])
        md = md_path.read_text(encoding="utf-8")

        check("归档 md 文件生成", md_path.is_file(), str(md_path.name))
        check("归档 md 含 ## 九、知识库参考", "## 九、知识库参考" in md)
        # 渲染端会把片段里的换行压成空格（markdown.py 的 _section_knowledge），
        # 所以比对前要做同样的变换；且用 any 覆盖全部片段，不赌某一条排第一。
        check("归档 md 含真实片段内容",
              any(_render_head(d.page_content) in md for d in docs),
              f"n={len(docs)}")
        check("归档 md 片段标注相关度", "相关度" in md)
        kinds = {d.metadata.get("kind") for d in docs}
        print(f"  [真实片段] n={len(docs)} kinds={sorted(kinds)}")
        print(f"  [top 片段] {str(docs[0].page_content)[:60].replace(chr(10), ' ')}")


def main() -> None:
    print("-- Part A: 静态代码检查（看板消费路径）--")
    code_checks()

    print("\n-- Part B: 真实 RAG 数据走归档渲染 --")
    archive_with_real_rag()

    print("=" * 50)
    print(f"Step 5 验证: {passed}/{passed + failed} 通过")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
