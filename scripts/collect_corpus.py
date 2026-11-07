"""外部语料采集 CLI —— 只抓不写库。

采集与入库分离：这里产出 data/external/text/<source>/<uid>.md + meta，
入库是 scripts/ingest_corpus.py 的事，所以本脚本的 db_writes 恒为 0。

  --dry-run   只抓不落盘（text/manifest 都不写；raw 响应缓存照写，避免重复抓）
  --rollback  删掉已有外部语料（where corpus=external），退回只有存量研报的状态

运行: D:/python/python.exe scripts/collect_corpus.py --source gov_cn --limit 2 --dry-run
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = ROOT / "data" / "external"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_text(doc, dry_run: bool) -> Path | None:
    if dry_run:
        return None
    d = BASE / "text" / doc.source_id
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in doc.doc_uid)[:80]
    md = d / f"{safe}.md"
    md.write_text(f"# {doc.title}\n\n{doc.text}\n", encoding="utf-8")
    (d / f"{safe}.meta.json").write_text(
        json.dumps(doc.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return md


def append_manifest(rows: list[dict]) -> None:
    if not rows:
        return
    path = BASE / "manifest.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def rollback() -> None:
    from app.rag.store import get_knowledge_store

    store = get_knowledge_store()
    before = store.count()
    n = store.count(where={"corpus": "external"})
    if n:
        store.delete(where={"corpus": "external"})
    after = store.count()
    print(f"[rollback] 删除外部切片 {n} 个；count {before} → {after}")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="外部语料采集（只抓不写库）")
    ap.add_argument("--source", action="append", default=[],
                    help="源 id，可重复；逗号分隔也接受")
    ap.add_argument("--all", action="store_true", help="采集所有已实现适配器的源")
    ap.add_argument("--limit", type=int, default=0, help="每源条数上限（默认取配额）")
    ap.add_argument("--dry-run", action="store_true", help="不写 text/manifest")
    ap.add_argument("--rollback", action="store_true", help="删除已有外部语料后退出")
    args = ap.parse_args()

    if args.rollback:
        rollback()
        return

    from app.rag.acquire.adapters import available, get_adapter
    from app.rag.acquire.fetcher import Fetcher, NotCollected
    from app.rag.acquire.policy import get_sources

    sources = get_sources()
    if args.all:
        wanted = available()
    else:
        wanted = [s.strip() for chunk in args.source for s in chunk.split(",") if s.strip()]
    if not wanted:
        raise SystemExit("请用 --source <id> 或 --all 指定采集源")

    fetcher = Fetcher(sources=sources, base_dir=BASE)
    manifest: list[dict] = []
    total = {"ok": 0, "empty_text": 0, "not_collected": 0}

    for sid in wanted:
        policy = sources.get(sid)
        limit = args.limit or policy.quota
        limit = min(limit, policy.quota)
        adapter = get_adapter(sid)
        print(f"\n[{sid}] {policy.name} · 上限 {limit} · 间隔 {policy.interval_s}s"
              + ("  (dry-run)" if args.dry_run else ""))
        try:
            results = adapter.collect(fetcher, limit)
        except NotCollected as e:
            print(f"  !! 整源失败: {e.reason_code} {e.detail}")
            total["not_collected"] += 1
            manifest.append({"ts": _now(), "source_id": sid, **e.to_dict()})
            continue

        for r in results:
            if isinstance(r, NotCollected):
                total["not_collected"] += 1
                print(f"  [not_collected] {r.reason_code:14s} {r.url}")
                manifest.append({"ts": _now(), **r.to_dict()})
                continue
            path = write_text(r, args.dry_run)
            if r.ok:
                total["ok"] += 1
                print(f"  [ok] {r.doc_uid:10s} {len(r.text):6d}字 {r.published or '—'}  {r.title[:38]}")
            else:
                total["empty_text"] += 1
                print(f"  [empty_text] {r.doc_uid:10s} {len(r.text):6d}字 {r.title[:38]}")
            manifest.append({
                "ts": _now(), **r.to_dict(),
                "text_path": str(path.relative_to(ROOT)) if path else None,
            })

    if not args.dry_run:
        append_manifest(manifest)

    print(f"\n汇总: ok={total['ok']} empty_text={total['empty_text']} "
          f"not_collected={total['not_collected']} db_writes=0")
    print(f"  manifest: {'(dry-run 未写)' if args.dry_run else 'data/external/manifest.jsonl'}")


if __name__ == "__main__":
    main()
