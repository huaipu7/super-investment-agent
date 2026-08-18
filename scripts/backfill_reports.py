"""知识库主动填充 —— 深度研报 md 增量回写 RAG（解决「九、知识库参考常为空」）。

把 data/reports/*.md 按章节切块后 upsert 到知识库；同内容文档按内容哈希天然去重，
增量模式先读取库内已有 id，跳过已入库报告，避免重复嵌入（每次只嵌入新增块）。

用法（解释器 D:\\python\\python.exe，项目根目录）:
    D:/python/python.exe scripts/backfill_reports.py             # 增量回填全部研报
    D:/python/python.exe scripts/backfill_reports.py --ticker AAPL
    D:/python/python.exe scripts/backfill_reports.py --full      # 强制重建全部

仅做入库编排，不修改原始报告文件；缺失/空内容报告跳过并计数。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.rag.chunking import chunk_markdown  # noqa: E402
from app.rag.ingest.reports import _parse_fname  # noqa: E402
from app.rag.store import doc_id, get_knowledge_store  # noqa: E402
from app.utils.logger import logger  # noqa: E402


def _existing_ids(store) -> set[str]:
    try:
        return {d["id"] for d in store.all_docs()}
    except Exception:
        return set()


def backfill_reports(ticker: str | None = None, full: bool = False) -> dict:
    s = get_settings()
    store = get_knowledge_store()
    report_dir = Path(s.base_dir / "data" / "reports")
    if not report_dir.is_dir():
        return {"scanned": 0, "indexed": 0, "skipped": 0,
                "available": store.available(), "total": 0}

    known = set() if full else _existing_ids(store)
    scanned = indexed = skipped = 0
    for md in sorted(report_dir.glob("*.md")):
        f_ticker, f_date = _parse_fname(md.name)
        if ticker and f_ticker and f_ticker.upper() != ticker:
            continue
        rel = md.relative_to(s.base_dir).as_posix()
        meta = {
            "kind": "研报",
            "ticker": f_ticker,
            "date": f_date,
            "source": rel,
            "section": "",
            "url": "",
        }
        try:
            docs = chunk_markdown(md, meta)
        except Exception as e:
            logger.warning("研报切块失败 %s: %s", md.name, e)
            continue
        scanned += 1
        new_docs = []
        for d in docs:
            d["id"] = doc_id("研报", rel, d["page_content"])
            if full or d["id"] not in known:
                new_docs.append(d)
            else:
                skipped += 1
        n = store.upsert(new_docs)
        indexed += n
    try:
        total = store.count(where={"kind": "研报"})
    except Exception:
        total = None
    return {"scanned": scanned, "indexed": indexed, "skipped": skipped,
            "available": store.available(), "total": total}


def main():
    ap = argparse.ArgumentParser(description="知识库主动填充：研报 md 增量回写 RAG")
    ap.add_argument("--ticker", default="", help="只回写该 ticker 的研报（如 AAPL）")
    ap.add_argument("--full", action="store_true", help="强制重建全部（忽略库内已有 id）")
    args = ap.parse_args()

    r = backfill_reports(ticker=args.ticker.strip().upper() or None, full=args.full)
    print(f"扫描 {r['scanned']} 份研报 | 入库 {r['indexed']} 块 | 跳过(已入库) {r['skipped']} 块")
    print(f"嵌入后端可用: {r['available']} | 知识库研报总量: {r['total']}")


if __name__ == "__main__":
    main()
