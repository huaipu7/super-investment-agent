"""arXiv 适配器 —— q-fin / econ 预印本（开放获取）。

robots 实测（2026-11-05），这条链路因此不走官方 API：
  arxiv.org/robots.txt     Crawl-delay: 15；Allow /list /abs /pdf；Disallow /api /search
  export.arxiv.org/robots  Disallow: /（整个导出主机禁止）
官方 Atom API 正落在被禁路径上，所以发现改走 /list/<分类>/recent，
元数据取自 /abs/<id>（题名/作者/日期/摘要/许可），正文取自 /pdf/<id>。
代价是每篇两次请求，间隔按 Crawl-delay 取 15s。

许可逐篇记录：abs 页 .abs-license 指向具体协议（默认 arXiv 非独占许可，
部分作者选 CC BY 等）。取不到就留空，不拿来源级 license_class 冒充单篇许可。

带 NC（非商业）限制的论文不收：abs 页一认出许可就判 not_collected(license_denied)，
不再为它请求 PDF。
"""
from __future__ import annotations

import re

from app.rag.acquire.adapters.base import Adapter
from app.rag.acquire.fetcher import NotCollected
from app.rag.acquire.pdf import pdf_to_text
from app.rag.acquire.types import Candidate, Document

LIST_URL = "https://arxiv.org/list/{cat}/recent"
ABS_URL = "https://arxiv.org/abs/{uid}"
PDF_URL = "https://arxiv.org/pdf/{uid}"

# 顺序即优先级：与投研关系最近的排前面（组合管理、统计金融、公司金融…）
CATEGORIES = (
    "q-fin.PM", "q-fin.ST", "q-fin.GN", "q-fin.RM", "q-fin.TR", "q-fin.MF",
    "q-fin.CP", "q-fin.PR", "q-fin.EC", "econ.EM", "econ.GN", "econ.TH",
)

# 分类 recent 页通常 16 条上下，据此估算要翻几个分类页才能凑够 limit
LIST_PAGE_HINT = 12

_LICENSE_LABEL = {
    "licenses/nonexclusive-distrib/1.0": "arXiv perpetual non-exclusive",
    "publicdomain/zero/1.0": "CC0 1.0",
    "publicdomain/1.0": "Public Domain",
    "licenses/by/4.0": "CC BY 4.0",
    "licenses/by-sa/4.0": "CC BY-SA 4.0",
    "licenses/by-nc-sa/4.0": "CC BY-NC-SA 4.0",
    "licenses/by-nc-nd/4.0": "CC BY-NC-ND 4.0",
}

_DESCRIPTOR = re.compile(r"^(Title|Authors|Subjects|Abstract):\s*")
_DATE = re.compile(r"^\d{4}/\d{2}/\d{2}$")
_PRIMARY = re.compile(r"\(([a-z-]+\.[A-Z]{2})\)")
# 认得的许可都写成 CC BY-NC… 这种可读名，未认得的原样留 URL —— 两种都能命中 -nc
_NC = re.compile(r"-nc", re.I)


def _clean(raw: str) -> str:
    return _DESCRIPTOR.sub("", re.sub(r"\s+", " ", raw or "")).strip()


def _license_label(href: str) -> str:
    """认得的协议给人读的名字；认不得就原样留 URL，不猜。"""
    probe = href.rstrip("/")
    for tail, label in _LICENSE_LABEL.items():
        if probe.endswith(tail):
            return label
    return href


def _meta(soup) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in soup.find_all("meta"):
        name = (m.get("name") or "").strip()
        if name.startswith("citation_"):
            out.setdefault(name, []).append((m.get("content") or "").strip())
    return out


def _interleave(buckets: list[list[Candidate]]) -> list[Candidate]:
    out: list[Candidate] = []
    i = 0
    while True:
        added = False
        for b in buckets:
            if i < len(b):
                out.append(b[i])
                added = True
        if not added:
            return out
        i += 1


class ArxivAdapter(Adapter):
    source_id = "arxiv"
    kind = "论文"
    doc_type = "论文"
    lang = "en"

    def __init__(self, source_id: str | None = None, categories=None):
        super().__init__(source_id)
        self.categories = tuple(categories or CATEGORIES)

    def collect(self, fetcher, limit: int) -> list:
        # 正文来自 PDF，abs 壳页重取没有意义；空正文就是空正文
        return super().collect(fetcher, limit, retry_shell=False)

    # ── 发现 ─────────────────────────────────────────────────────────

    def candidates(self, fetcher, limit: int) -> list[Candidate]:
        pages_needed = min(len(self.categories), max(1, -(-limit // LIST_PAGE_HINT)))
        buckets: list[list[Candidate]] = []
        seen: set[str] = set()
        total = 0

        for cat in self.categories:
            if len(buckets) >= pages_needed and total >= limit:
                break
            items = []
            for cand in self._list_page(fetcher, cat):
                if cand.doc_uid in seen:
                    continue
                seen.add(cand.doc_uid)
                items.append(cand)
            if items:
                buckets.append(items)
                total += len(items)

        return _interleave(buckets)[:limit]

    def _list_page(self, fetcher, cat: str) -> list[Candidate]:
        from bs4 import BeautifulSoup

        res = fetcher.get(LIST_URL.format(cat=cat), source_id=self.source_id,
                          expect="html")
        soup = BeautifulSoup(res.text(), "lxml")
        nodes = soup.select("#articles > dt") or soup.find_all("dt")

        out: list[Candidate] = []
        for dt in nodes:
            link = dt.select_one('a[href^="/abs/"]')
            if link is None:
                continue
            uid = (link.get("href") or "").rstrip("/").rsplit("/", 1)[-1]
            if not uid:
                continue
            dd = dt.find_next_sibling("dd")
            out.append(Candidate(
                doc_uid=uid,
                url=ABS_URL.format(uid=uid),
                title=_clean(dd.select_one(".list-title").get_text(" ", strip=True))
                if dd and dd.select_one(".list-title") else "",
                expect="html",
                extra={"category": cat},
            ))
        return out

    # ── 解析 ─────────────────────────────────────────────────────────

    def parse(self, cand: Candidate, res, fetcher) -> Document:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(res.text(), "lxml")
        meta = _meta(soup)

        def first(key: str) -> str:
            vals = meta.get(key) or []
            return vals[0] if vals else ""

        title = first("citation_title") or cand.title
        authors = meta.get("citation_author") or []
        abstract = re.sub(r"\s+", " ", first("citation_abstract")).strip()

        node = soup.select_one(".subjects")
        subjects = _clean(node.get_text(" ", strip=True)) if node else ""
        m = _PRIMARY.search(subjects)
        doc_type = m.group(1) if m else cand.extra.get("category", self.doc_type)

        raw_date = first("citation_date")
        published = raw_date.replace("/", "-") if _DATE.match(raw_date) else ""

        link = soup.select_one(".abs-license a")
        license_ = _license_label(link.get("href") or "") if link else ""
        if _NC.search(license_):
            raise NotCollected(self.source_id, cand.url, "license_denied", None,
                               f"逐篇许可 {license_} 含 NC 限制，按语料策略不收")

        pdf_url = first("citation_pdf_url") or PDF_URL.format(uid=cand.doc_uid)
        pdf = fetcher.get(pdf_url, source_id=self.source_id, expect="pdf")

        return Document(
            source_id=self.source_id,
            doc_uid=cand.doc_uid,
            url=cand.url,
            title=re.sub(r"\s+", " ", title).strip(),
            published=published,
            ticker="",
            doc_type=doc_type,
            lang=self.lang,
            text=pdf_to_text(pdf.body),
            kind=self.kind,
            license=license_,
            extra={
                "arxiv_id": cand.doc_uid,
                "pdf_url": pdf_url,
                "authors": authors,
                "subjects": subjects,
                "abstract": abstract,
            },
        )
