"""外部语料入库 CLI —— data/external/text/** → 切块 → 知识库。

采集与入库分离：本脚本不联网，只读 collect_corpus 落下的 text/<source>/。

  --db        目标库目录（默认 data/knowledge_db；幂等验收用临时库）
  --source    只入库指定源，可重复、逗号分隔
  --limit     每个源最多入库几篇（调试用）
  --dry-run   只统计切块，不写库

写正式库前先停后端（8000 / 8599），别让服务端和脚本并发写同一个 Chroma。

运行: D:/python/python.exe scripts/ingest_corpus.py --db data/knowledge_db_ext_test
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = ROOT / "data" / "external"


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="外部语料入库（不联网）")
    ap.add_argument("--db", default="", help="库目录，默认 data/knowledge_db")
    ap.add_argument("--source", action="append", default=[], help="源 id，可重复")
    ap.add_argument("--limit", type=int, default=0, help="每源最多入库篇数")
    ap.add_argument("--dry-run", action="store_true", help="只统计切块，不写库")
    args = ap.parse_args()

    from app.rag.ingest.external import build_chunks, ingest_records, load_records
    from app.rag.store import KnowledgeStore

    sources = [s.strip() for chunk in args.source for s in chunk.split(",") if s.strip()]
    records = load_records(BASE, sources or None, args.limit)
    if not records:
        raise SystemExit(f"{BASE / 'text'} 下没有可入库文档，先跑 collect_corpus.py")

    if args.dry_run:
        docs = chunks = 0
        for rec in records:
            n = len(build_chunks(rec))
            if not n:
                print(f"  [skip] {rec.get('source_id')}/{rec.get('doc_uid')} 切块为空")
                continue
            docs += 1
            chunks += n
            print(f"  [plan] {rec.get('source_id'):9s} {rec.get('doc_uid'):12s} "
                  f"{n:5d} 块  {rec.get('doc_type', ''):8s} {rec.get('title', '')[:34]}")
        print(f"\n汇总(dry-run): docs={docs} chunks={chunks} db_writes=0")
        return

    store = KnowledgeStore(args.db or None)
    if not store.available():
        raise SystemExit("嵌入后端不可用，拒绝入库（避免写入无向量的切片）")
    before = store.count()
    stats = ingest_records(store, records)
    after = store.count()
    print(f"\n库: {args.db or 'data/knowledge_db'} · 后端: {store.backend_label}")
    print(f"docs={stats['docs']} skipped={stats['skipped']} chunks={stats['chunks']} "
          f"written={stats['written']} orphans_deleted={stats['orphans_deleted']}")
    print(f"count {before} → {after}")


if __name__ == "__main__":
    main()
