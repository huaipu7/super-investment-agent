"""文档入库脚本 —— 文本/PDF → 切块 → 知识库 upsert（带 source/date/ticker metadata）。

用法:
  D:\\python\\python.exe scripts/ingest_docs.py <文件或目录> [--kind 研报|宏观新闻|新闻|公告] [--ticker 600519.SS] [--date 2026-08-14] [--source 自定义来源]

支持的输入：
  .md   按 `## ` 章节切块（chunk_markdown）
  .txt  普通文本切块（chunk_text）
  .pdf  提取文本后切块（依赖 pdfplumber 或 pypdf，可选）

诚实边界：不联网拉取，只入库你给的本地文件；缺字段显式留空，不伪造。
"""
from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.rag.chunking import chunk_markdown, chunk_text  # noqa: E402
from app.rag.store import doc_id, get_knowledge_store  # noqa: E402


def _extract_pdf_text(path: Path) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except ImportError:
        pass
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except ImportError:
        pass
    raise RuntimeError(
        "提取 PDF 文本需要 pdfplumber 或 pypdf（可选依赖），当前均未安装")


def _chunk_file(path: Path, meta: dict) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return chunk_markdown(path, meta)
    if suffix == ".txt":
        return chunk_text(path.read_text(encoding="utf-8", errors="replace"), meta)
    if suffix == ".pdf":
        return chunk_text(_extract_pdf_text(path), meta)
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="文档入库（RAG 知识库）")
    ap.add_argument("path", help="文件或目录")
    ap.add_argument("--kind", default="研报",
                    help="文档类型（研报|宏观新闻|新闻|公告）")
    ap.add_argument("--ticker", default="", help="关联股票代码（可为空）")
    ap.add_argument("--date", default="", help="YYYY-MM-DD，默认今天")
    ap.add_argument("--source", default="", help="来源说明，默认文件名")
    args = ap.parse_args()

    date = args.date or datetime.date.today().isoformat()
    src = Path(args.path)
    files = [src] if src.is_file() else sorted(src.glob("*.md")) + \
        sorted(src.glob("*.txt")) + sorted(src.glob("*.pdf"))
    if not files:
        print(f"错误: 没有可入库的文件（{src}）")
        return 1

    store = get_knowledge_store()
    print(f"嵌入后端: {store.backend_label} | available={store.available()}")
    if not store.available():
        print("警告: 当前无可用嵌入后端（未配置 EMBEDDING_API_KEY 且本地 bge 不可加载）。"
              "可先本地启动 mock 后端，或配置远程嵌入后再入库。")

    scanned = indexed = 0
    for f in files:
        meta = {
            "kind": args.kind,
            "ticker": args.ticker,
            "date": date,
            "source": args.source or f.name,
            "section": "",
            "url": "",
        }
        try:
            docs = _chunk_file(f, meta)
        except Exception as e:
            print(f"  ! {f.name}: {e}")
            continue
        for d in docs:
            d["id"] = doc_id(args.kind, meta["source"], d["page_content"])
        n = store.upsert(docs)
        scanned += 1
        indexed += n
        print(f"  [{f.name}] 切块 {len(docs)} → 入库 {n}")

    print(f"\n完成: {scanned} 个文件，共入库 {indexed} 个片段。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
