"""采集层的数据契约：候选条目、文档、采集结果。

Document.status 只有两种取值，语义必须分清：
    ok          抓到并解析出正文
    empty_text  抓到了（HTTP 200）但解析不出正文
抓都没抓到的，一律是 fetcher.NotCollected，不进 Document —— 不允许拿空文档冒充。
"""
from __future__ import annotations

from dataclasses import dataclass, field

MIN_TEXT_CHARS = 120


@dataclass
class Candidate:
    """列表页/索引里的一条待取条目。"""

    doc_uid: str
    url: str
    title: str = ""
    published: str = ""
    expect: str = "html"     # 决定 Accept 头：html | pdf | json | atom | text
    extra: dict = field(default_factory=dict)


@dataclass
class Document:
    source_id: str
    doc_uid: str
    url: str
    title: str
    published: str          # YYYY-MM-DD，取不到留空，不编日期
    ticker: str             # canonical；宏观/政策为 ""
    doc_type: str
    lang: str
    text: str
    kind: str = ""          # 政策 | 披露文件 | 论文
    status: str = "ok"      # ok | empty_text
    # 逐篇许可（arXiv 逐篇不同）；来源级许可看 sources.yaml 的 license_class
    license: str = ""
    # 源特有元数据（作者、分类、摘要…），写进 manifest/meta.json，入库不使用
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and len(self.text.strip()) >= MIN_TEXT_CHARS

    def to_dict(self) -> dict:
        out = {
            "source_id": self.source_id,
            "doc_uid": self.doc_uid,
            "url": self.url,
            "title": self.title,
            "published": self.published,
            "ticker": self.ticker,
            "doc_type": self.doc_type,
            "lang": self.lang,
            "kind": self.kind,
            "status": self.status,
            "chars": len(self.text),
            "license": self.license,
        }
        if self.extra:
            out["extra"] = self.extra
        return out


def finalize(doc: Document) -> Document:
    """按正文长度定 status —— 只有这里能判 empty_text。"""
    doc.status = "ok" if len(doc.text.strip()) >= MIN_TEXT_CHARS else "empty_text"
    return doc
