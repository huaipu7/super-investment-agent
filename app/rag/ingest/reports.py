"""历史研报入库 —— 扫描 data/reports/*.md，按章节切块后写入知识库。

文件名形如 {ticker}_{YYYYMMDD}_{HHMMSS}.md（兼容 600519.SS_xxx 与 "AAPL 对话分析_xxx"）。
id = sha1("研报"|相对路径|章节内容)，同一文件重复扫描天然去重。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config import get_settings
from app.rag.chunking import chunk_markdown
from app.rag.store import doc_id, get_knowledge_store
from app.utils.logger import logger

# {ticker}_{YYYYMMDD}_{HHMMSS}.md 或 {ticker}_{YYYYMMDD}_{HHMMSS}_xxx.md
_FNAME = re.compile(r"^(.+?)_(\d{8})_\d{6}")


def _parse_fname(name: str) -> tuple[str, str]:
    m = _FNAME.match(name)
    if not m:
        return "", ""
    ticker = m.group(1).split()[0]  # "AAPL 对话分析" → "AAPL"
    y, mo, d = m.group(2)[:4], m.group(2)[4:6], m.group(2)[6:]
    return ticker, f"{y}-{mo}-{d}"


def index_all_reports() -> dict:
    """扫描全部历史研报 md 并入库。返回 {scanned, indexed}。"""
    s = get_settings()
    store = get_knowledge_store()
    report_dir = Path(s.base_dir / "data" / "reports")
    if not report_dir.is_dir():
        logger.warning("报告目录不存在: %s", report_dir)
        return {"scanned": 0, "indexed": 0}

    scanned = indexed = 0
    for md in sorted(report_dir.glob("*.md")):
        rel = md.relative_to(s.base_dir).as_posix()
        ticker, date = _parse_fname(md.name)
        meta = {
            "kind": "研报",
            "ticker": ticker,
            "date": date,
            "source": rel,
            "section": "",
            "url": "",
        }
        try:
            docs = chunk_markdown(md, meta)
        except Exception as e:
            logger.warning("研报切块失败 %s: %s", md.name, e)
            continue
        for d in docs:
            d["id"] = doc_id("研报", rel, d["page_content"])
        n = store.upsert(docs)
        scanned += 1
        indexed += n
    return {"scanned": scanned, "indexed": indexed}
