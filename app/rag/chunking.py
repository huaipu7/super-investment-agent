"""文本/研报切块 —— 输出统一 [{page_content, metadata}]，供 KnowledgeStore.upsert。

- 研报（chunk_markdown / chunk_markdown_text）：按 `## ` 标题分节，超长节滑窗。
- 普通文本（chunk_text）：按段落粗切 + 滑窗补长段落。
- 滑窗：CHUNK_SIZE 字/重叠 CHUNK_OVERLAP 字，避免语义在边界被硬切。
- 逐页版式（normalize_paged_text）：剔除每页重复的页眉和「N / 总页数」页脚，
  同时给出每页起始偏移，供切片标注页码。
"""
from __future__ import annotations

import re
from collections import Counter
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


_PAGE_FOOTER = re.compile(r"^(\d+)\s*/\s*(\d+)$")
_HEADER_MIN_REPEAT = 3


def normalize_paged_text(text: str) -> tuple[str, list[int]]:
    """剔除逐页重复的页眉与页脚，返回 (清洗后正文, 每页起始偏移)。

    只在页脚构成完整的 1..N 序列、且总页数一致时才认定这是逐页版式。
    实测巨潮年报每页版式是「公司名 + 报告名」紧接「N / 总页数」，
    110/110、143/143 全部命中；港交所公告里也有 "5/5" 这类行，
    但凑不成 1..N 序列，那些文档原样返回，一个字不删。

    页眉只认「页脚紧邻的前一行，且全篇重复 ≥3 次」，避免误删只出现一次的正文行。
    页数对不上时同样原样返回 —— 清洗宁可少做，也不能吞正文。
    """
    lines = text.split("\n")
    foots = [
        (i, int(m.group(1)), int(m.group(2)))
        for i, line in enumerate(lines)
        if (m := _PAGE_FOOTER.match(line.strip()))
    ]
    if len(foots) < 3:
        return text, [0]
    total = foots[0][2]
    if any(n != k or seen != total for k, (_, n, seen) in enumerate(foots, 1)):
        return text, [0]

    counts = Counter(line.strip() for line in lines if line.strip())
    header_idx: set[int] = set()
    for i, _, _ in foots:
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j >= 0 and counts[lines[j].strip()] >= _HEADER_MIN_REPEAT:
            header_idx.add(j)

    footer_no = {i: n for i, n, _ in foots}
    # 版式是「页眉 → 页脚 → 正文」，所以第 k 个页脚之后的内容属于第 k 页；
    # 第一个页脚之前只有封面碎片，并入第 1 页
    pre: list[str] = []
    pages: list[str] = []
    buf: list[str] = []
    seen_footer = False
    for i, line in enumerate(lines):
        if i in header_idx:
            continue
        if i in footer_no:
            if not seen_footer:
                pre, seen_footer = buf, True
            else:
                pages.append("\n".join(buf).strip())
            buf = []
            continue
        buf.append(line)
    if buf:
        pages.append("\n".join(buf).strip())
    if len(pages) != total:
        return text, [0]
    if pre:
        cover = "\n".join(pre).strip()
        pages[0] = f"{cover}\n{pages[0]}".strip()

    starts: list[int] = []
    pos = 0
    for p in pages:
        starts.append(pos)
        pos += len(p) + 1
    return "\n".join(pages), starts


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
