"""中国政府网 · 国务院政策文件库适配器。

发现走站点自己的列表数据 ZUIXINZHENGCE.json（页面 JS 就是从它渲染的），
比解析 DOM 稳定；正文走文章页 #UCAP-CONTENT。

政策文件属《著作权法》第五条排除对象，可全文入库。ticker 留空（宏观）。
"""
from __future__ import annotations

import hashlib
import json
import re

from app.rag.acquire.adapters.base import Adapter, html_to_text
from app.rag.acquire.types import Candidate, Document

LIST_URL = "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"
ARTICLE_RE = re.compile(r"content_(\d+)\.htm")
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


class GovCnAdapter(Adapter):
    source_id = "gov_cn"
    kind = "政策"
    doc_type = "政策文件"
    lang = "zh"

    def candidates(self, fetcher, limit: int) -> list[Candidate]:
        res = fetcher.get(LIST_URL, source_id=self.source_id, expect="json")
        items = json.loads(res.text())
        out: list[Candidate] = []
        for it in items:
            url = (it.get("URL") or "").strip()
            title = (it.get("TITLE") or "").strip()
            if not url or not title:
                continue
            m = ARTICLE_RE.search(url)
            uid = m.group(1) if m else hashlib.sha1(url.encode()).hexdigest()[:16]
            out.append(Candidate(
                doc_uid=uid,
                url=url,
                title=title,
                published=(it.get("DOCRELPUBTIME") or "")[:10],
                extra={"sub_title": (it.get("SUB_TITLE") or "").strip()},
            ))
            if len(out) >= limit:
                break
        return out

    def parse(self, cand: Candidate, res, fetcher) -> Document:
        from bs4 import BeautifulSoup

        html = res.text()
        soup = BeautifulSoup(html, "lxml")

        title = cand.title
        for sel in ("h1", ".pages_title", ".article_title"):
            node = soup.select_one(sel)
            if node and node.get_text(strip=True):
                title = node.get_text(strip=True)
                break

        published = cand.published
        meta = soup.find("meta", attrs={"name": "firstpublishedtime"})
        if meta and meta.get("content"):
            m = DATE_RE.search(meta["content"])
            if m:
                published = m.group(0)

        text = html_to_text(html, "#UCAP-CONTENT")
        if not text:
            text = html_to_text(html, ".pages_content")

        return Document(
            source_id=self.source_id,
            doc_uid=cand.doc_uid,
            url=cand.url,
            title=title,
            published=published,
            ticker="",
            doc_type=self.doc_type,
            lang=self.lang,
            text=text,
            kind=self.kind,
        )
