"""适配器基类：候选发现 → 逐篇抓取解析 → Document/NotCollected。

子类只需实现 candidates() 与 parse()。抓取失败一律抛 NotCollected，
由 collect() 收进结果列表，不做静默跳过。
"""
from __future__ import annotations

import re

from app.rag.acquire.fetcher import Fetcher, NotCollected
from app.rag.acquire.types import Candidate, Document, finalize

_BLOCK_TAGS = ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table", "section")


def _pick_node(html: str, selector: str | None, drop: tuple[str, ...] = ()):
    """lxml → html.parser 逐个试。

    gov.cn 的文章页有错位嵌套（<br/> 与 table 交错），libxml2 会把整段正文
    丢掉，而纯 Python 的 html.parser 能完整解析 —— 实测同一页 163 字 vs 4670 字。
    drop 是按标签名摘除的节点（如 SEC 内联 XBRL 的 ix:header）。用 find_all 而
    不用 CSS 选择器：ix:header 里的冒号会被 soupsieve 当成伪类。
    """
    import warnings

    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

    for parser in ("lxml", "html.parser"):
        with warnings.catch_warnings():
            # SEC 内联 XBRL 是 XHTML，用 HTML 解析器解析是刻意为之，告警是噪音
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(html, parser)
        node = soup.select_one(selector) if selector else (soup.body or soup)
        if node is None:
            continue
        for tag in node.find_all(list(drop)):
            tag.decompose()
        if node.get_text(strip=True):
            return node
    return None


def html_to_text(html: str, selector: str | None = None,
                 drop: tuple[str, ...] = ()) -> str:
    """从 HTML 抽正文：优先容器选择器，失败则退回 body。"""
    node = _pick_node(html, selector, drop)
    if node is None:
        return ""
    for tag in node.find_all(["script", "style", "noscript"]):
        tag.decompose()
    for br in node.find_all("br"):
        br.replace_with("\n")
    for tag in node.find_all(_BLOCK_TAGS):
        tag.append("\n")
    text = node.get_text("", strip=False)
    text = re.sub(r"[ \t　\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


class Adapter:
    source_id: str = ""
    kind: str = ""
    doc_type: str = ""
    lang: str = "zh"

    def __init__(self, source_id: str | None = None):
        if source_id:
            self.source_id = source_id

    def candidates(self, fetcher: Fetcher, limit: int) -> list[Candidate]:
        raise NotImplementedError

    def parse(self, cand: Candidate, res, fetcher: Fetcher) -> Document:
        raise NotImplementedError

    def collect(self, fetcher: Fetcher, limit: int,
                retry_shell: bool = True) -> list[Document | NotCollected]:
        out: list[Document | NotCollected] = []
        for cand in self.candidates(fetcher, limit):
            try:
                doc = self.parse(cand, fetcher.get(cand.url, source_id=self.source_id,
                                                  expect=cand.expect), fetcher)
                if retry_shell and not doc.ok and doc.status == "empty_text":
                    # 站点偶尔返回无正文的壳页；强制重取一次，仍空才判 empty_text
                    res = fetcher.get(cand.url, source_id=self.source_id,
                                      expect=cand.expect, force=True)
                    doc = self.parse(cand, res, fetcher)
                out.append(finalize(doc))
            except NotCollected as e:
                out.append(e)
        return out
