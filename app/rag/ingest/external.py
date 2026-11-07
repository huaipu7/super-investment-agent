"""外部语料入库 —— data/external/text/<source>/ 下的正文切块后写入知识库。

id = sha1(kind|source_id|doc_uid|c{idx})：同一篇重抓时块序稳定，写的还是那批 id；
重新切块后块数变少的，多出来的旧 id 按 orphans 删掉，不留孤儿。
顺序固定为「先 upsert 成功，再删 old-new」；切块为空、或写入有批次失败时
直接跳过（只记 warning），库里该篇的旧数据一个不动。

PDF 逐页版式（巨潮年报：页眉 + 「N / 总页数」页脚）在切块前清洗，
清洗后的每页起始偏移用来给切片标 page。
"""
from __future__ import annotations

import bisect
import json
from pathlib import Path

from app.rag.chunking import chunk_text, normalize_paged_text
from app.rag.store import KnowledgeStore, doc_id
from app.utils.logger import logger

MIN_TEXT_CHARS = 120


def load_records(base: Path, sources: list[str] | None = None,
                 limit: int = 0) -> list[dict]:
    """读 text/<source>/<uid>.md + 同名 .meta.json，只收 status=ok 的文档。"""
    root = Path(base) / "text"
    out: list[dict] = []
    per_source: dict[str, int] = {}
    for meta_path in sorted(root.glob("*/*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        sid = str(meta.get("source_id") or "")
        if sources and sid not in sources:
            continue
        if meta.get("status") != "ok":
            continue
        if limit and per_source.get(sid, 0) >= limit:
            continue
        md = meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".md")
        if not md.is_file():
            logger.warning("外部语料缺正文文件: %s", md)
            continue
        text = _strip_h1(md.read_text(encoding="utf-8", errors="replace"))
        per_source[sid] = per_source.get(sid, 0) + 1
        out.append({**meta, "text": text})
    return out


def _strip_h1(raw: str) -> str:
    """collect_corpus 落盘格式是「# 标题\\n\\n正文」，标题由 meta 提供。"""
    head, sep, rest = raw.partition("\n")
    if head.startswith("# ") and sep:
        return rest.lstrip("\n")
    return raw


def build_chunks(rec: dict) -> list[dict]:
    """一篇文档 → 切片列表；正文太短返回空列表。"""
    text, page_starts = normalize_paged_text(rec.get("text", ""))
    if len(text.strip()) < MIN_TEXT_CHARS:
        return []
    meta = {
        "kind": rec.get("kind", ""),
        "ticker": rec.get("ticker", ""),
        "date": rec.get("published", ""),
        "source": rec.get("source_id", ""),
        "section": "正文",
        "url": rec.get("url", ""),
        "corpus": "external",
        "license": rec.get("license", ""),
        "doc_type": rec.get("doc_type", ""),
        "lang": rec.get("lang", ""),
        "doc_uid": rec.get("doc_uid", ""),
    }
    chunks = chunk_text(text, meta)
    cursor = 0
    for i, chunk in enumerate(chunks):
        pos = text.find(chunk["page_content"], cursor)
        if pos < 0:
            pos = cursor
        cursor = pos
        chunk["metadata"]["page"] = bisect.bisect_right(page_starts, pos)
        chunk["id"] = doc_id(meta["kind"], meta["source"], meta["doc_uid"], f"c{i}")
    return chunks


def ingest_records(store: KnowledgeStore, records: list[dict]) -> dict:
    stats = {"docs": 0, "skipped": 0, "chunks": 0, "written": 0, "orphans_deleted": 0}
    for rec in records:
        tag = f"{rec.get('source_id')}/{rec.get('doc_uid')}"
        chunks = build_chunks(rec)
        if not chunks:
            logger.warning("外部语料切块为空，跳过 %s", tag)
            stats["skipped"] += 1
            continue
        where = {
            "corpus": "external",
            "source": rec.get("source_id", ""),
            "doc_uid": rec.get("doc_uid", ""),
        }
        old = set(store.ids_where(where))
        new_ids = {c["id"] for c in chunks}
        written = store.upsert(chunks)
        if written != len(chunks):
            logger.warning("外部语料写入不完整（%d/%d），保留旧切片 %s",
                           written, len(chunks), tag)
            stats["skipped"] += 1
            continue
        orphans = sorted(old - new_ids)
        stats["orphans_deleted"] += store.delete_ids(orphans)
        stats["docs"] += 1
        stats["chunks"] += len(chunks)
        stats["written"] += written
        logger.info("外部语料入库 %s · %d 块 · 清理旧块 %d", tag, len(chunks), len(orphans))
    return stats
