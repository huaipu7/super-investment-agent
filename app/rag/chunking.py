"""文本/研报切块 —— 输出统一 [{page_content, metadata}]，供 KnowledgeStore.upsert。

- 研报（chunk_markdown / chunk_markdown_text）：按 `## ` 标题分节，超长节滑窗。
- 普通文本（chunk_text）：按段落粗切 + 滑窗补长段落。
- 滑窗：CHUNK_SIZE 字/重叠 CHUNK_OVERLAP 字，避免语义在边界被硬切。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

CHUNK_SIZE = 400
CHUNK_OVERLAP = 80


def chunk_markdown(md_path: str | Path, meta: dict) -> list[dict]:
    """读取 md 文件并按章节切块。"""
    text = Path(md_path).read_text(encoding="utf-8", errors="replace")
    return chunk_markdown_text(text, meta)


def chunk_markdown_text(text: str, meta: dict) -> list[dict]:
    """按 `## ` 二级标题分割，超长节滑窗切块。"""
    if not text.strip():
        return []
    sections = re.split(r"(?m)^##\s+", text)
    docs: list[dict] = []
    section_no = 0
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        section_no += 1
        lines = sec.split("\n", 1)
        heading = lines[0].strip() if lines else ""
        body = lines[1].strip() if len(lines) > 1 else ""
        section_name = heading or f"章节{section_no}"
        full = f"{heading}\n{body}".strip() if heading else body
        for chunk in _sliding_chunks(full):
            m = dict(meta)
            m["section"] = section_name[:50]
            docs.append({"page_content": chunk, "metadata": m})
    return docs


def chunk_text(text: str, meta: dict) -> list[dict]:
    """普通文本切块（段落优先，长段落滑窗）。"""
    docs: list[dict] = []
    for chunk in _sliding_chunks(text):
        m = dict(meta)
        m.setdefault("section", "正文")
        docs.append({"page_content": chunk, "metadata": m})
    return docs


def _sliding_chunks(
    text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """段落粗切 + 长段落滑窗，返回等宽块列表。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 1 <= size:
            buf = f"{buf}\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)
            if len(p) > size:
                chunks.extend(_split_long(p, size, overlap))
                buf = ""
            else:
                buf = p
    if buf:
        chunks.append(buf)
    return chunks


def _split_long(text: str, size: int, overlap: int) -> list[str]:
    """单个超长段落按滑窗切成多块。"""
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        out.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return out
