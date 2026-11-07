"""PDF → 纯文本。巨潮与披露易共用。

PyMuPDF 快、排版还原好；pdfplumber 兜底；两者都失败返回 ""（由 finalize 判
empty_text）。扫描件没有文本层，这里也不做 OCR —— 那是另一条链路。
"""
from __future__ import annotations

import re


def pdf_to_text(body: bytes) -> str:
    text = ""
    try:
        import fitz

        with fitz.open(stream=body, filetype="pdf") as doc:
            text = "\n".join(page.get_text("text") for page in doc)
    except Exception:
        try:
            import io

            import pdfplumber

            with pdfplumber.open(io.BytesIO(body)) as pdf:
                text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        except Exception:
            return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()
