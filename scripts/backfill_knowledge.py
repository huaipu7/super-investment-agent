"""知识库回填脚本 —— 三类内容源入库（历史研报 / 宏观 / 个股新闻公告）。

用法（解释器 D:\\python\\python.exe，在项目根目录运行）:
    D:/python/python.exe scripts/backfill_knowledge.py
    D:/python/python.exe scripts/backfill_knowledge.py --tickers 600519.SS,AAPL
    D:/python/python.exe scripts/backfill_knowledge.py --skip-reports --skip-macro
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag.ingest.macro import index_macro  # noqa: E402
from app.rag.ingest.news import index_ticker  # noqa: E402
from app.rag.ingest.reports import index_all_reports  # noqa: E402
from app.rag.store import get_knowledge_store  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="知识库回填：研报 + 宏观 + 个股新闻")
    ap.add_argument("--tickers", default="600519.SS,AAPL",
                    help="逗号分隔的股票代码（默认 600519.SS,AAPL）")
    ap.add_argument("--skip-reports", action="store_true", help="跳过历史研报")
    ap.add_argument("--skip-macro", action="store_true", help="跳过宏观新闻/数据")
    args = ap.parse_args()

    store = get_knowledge_store()
    print(f"知识库目录: {store._persist_dir}")
    print(f"嵌入模型可用: {store.available()}")

    if not args.skip_reports:
        r = index_all_reports()
        print(f"[研报] 扫描 {r['scanned']} 个文件, 入库 {r['indexed']} 块")

    if not args.skip_macro:
        m = index_macro()
        print(f"[宏观] 新闻 {m['macro_news']} 条, 数据快照 {m['macro_data']} 条")

    for tk in [t.strip() for t in args.tickers.split(",") if t.strip()]:
        r = index_ticker(tk)
        print(f"[个股 {tk}] 新闻/公告 {r['news']} 条, 股东大会 {r['meetings']} 条")

    print("=" * 50)
    print(f"知识库总量: {store.count()}")
    print(f"  其中 研报: {store.count(where={'kind': '研报'})}")
    print(f"  宏观新闻: {store.count(where={'kind': '宏观新闻'})}")
    print(f"  宏观数据: {store.count(where={'kind': '宏观数据'})}")
    print(f"  新闻/公告: {store.count(where={'kind': '新闻'}) + store.count(where={'kind': '公告'})}")


if __name__ == "__main__":
    main()
