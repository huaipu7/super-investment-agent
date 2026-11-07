"""HKEXnews 披露易适配器 —— 港股法定披露文件（本轮只取 00700 腾讯控股）。

发现走两个接口（都受 Fetcher 的白名单/robots/限速约束）：
  prefix.do           证券代码 → stockId（披露易内部 id，直接用代码查不到）
  titleSearchServlet  stockId + 分类码 → 公告清单，FILE_LINK 指向 PDF
doc_uid = NEWS_ID，跨次采集稳定。

2026-11-05 实测的四个接口特性：
1. prefix.do 返回 JSONP（`callback({...});`），要剥壳；
2. titleSearchServlet 的 result 是「JSON 字符串」而非数组，要二次 loads；
3. 分类码 t1code=40000&t2code=40100 精确命中年度报告；但公告类（t1code=10000）
   的子类码不生效——任意 t2code 都返回同一批，只能按标题筛「表決結果」「大會通告」；
4. 单页 100 条按时间倒序，配额 10 够用，因此不翻页。

只取 FILE_TYPE=PDF 的条目：正文解析依赖文本层，别的格式不在本链路内。
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from app.rag.acquire.adapters.base import Adapter
from app.rag.acquire.fetcher import NotCollected
from app.rag.acquire.pdf import pdf_to_text
from app.rag.acquire.tickers import canonical_ticker
from app.rag.acquire.types import Candidate, Document

PREFIX_URL = ("https://www1.hkexnews.hk/search/prefix.do"
              "?callback=callback&lang=ZH&type=A&market=SEHK&name={code}")
SEARCH_URL = ("https://www1.hkexnews.hk/search/titleSearchServlet.do?sortDir=0"
              "&sortByOptions=DateTime&category=0&market=SEHK&stockId={stock_id}"
              "&documentType=-1&fromDate={from_date}&toDate={to_date}"
              "&searchType=0&rowRange=100&lang=ZH"
              "&title=&t2Gcode=-2&t1code={t1code}&t2code={t2code}")
PDF_BASE = "https://www1.hkexnews.hk"

TICKERS = ("700",)
YEARS_BACK = 6

# (t1code, t2code, 文档类型, 条数上限, 关键词) —— 顺序即优先级，组间轮转取。
GROUPS = (
    ("40000", "40100", "年报", 3, ()),
    ("10000", "-2", "股东会表决结果", 4, ("表決結果",)),
    ("10000", "-2", "股东大会通告", 3, ("大會通告",)),
    ("20000", "-2", "股东大会通函", 2, ("大會通告",)),
)

_TAG = re.compile(r"<[^>]+>")


def _text(raw) -> str:
    """披露易的标题带 <br/> 与 &#x2f; 实体，统一还原成纯文本。"""
    return html.unescape(_TAG.sub(" ", str(raw or ""))).strip()


def _jsonp(raw: str) -> dict:
    i, j = raw.find("("), raw.rfind(")")
    if i < 0 or j <= i:
        raise ValueError("不是 JSONP")
    return json.loads(raw[i + 1:j])


def _published(raw: str) -> str:
    """'13/05/2026 20:58' → '2026-05-13'；解析不了留空，不猜日期。"""
    try:
        return datetime.strptime(str(raw).strip()[:10], "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _window() -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=365 * YEARS_BACK + 2)
    return start.strftime("%Y%m%d"), today.strftime("%Y%m%d")


def _interleave(groups: list[list[Candidate]]) -> list[Candidate]:
    """组间轮转，小 limit 下也能同时拿到年报与股东会文件。"""
    out: list[Candidate] = []
    i = 0
    while True:
        added = False
        for g in groups:
            if i < len(g):
                out.append(g[i])
                added = True
        if not added:
            return out
        i += 1


class HkexnewsAdapter(Adapter):
    source_id = "hkexnews"
    kind = "披露文件"
    doc_type = "年报"
    lang = "zh"

    def __init__(self, source_id: str | None = None, tickers=None):
        super().__init__(source_id)
        self.tickers = tuple(tickers or TICKERS)

    def collect(self, fetcher, limit: int) -> list:
        # PDF 没有壳页问题，重取一次只是重复下载整本年报
        return super().collect(fetcher, limit, retry_shell=False)

    # ── 发现 ─────────────────────────────────────────────────────────

    def candidates(self, fetcher, limit: int) -> list[Candidate]:
        out: list[Candidate] = []
        seen: set[str] = set()
        for ticker in self.tickers:
            code = str(ticker).zfill(5)
            stock_id = self._stock_id(fetcher, code)
            groups: list[list[Candidate]] = [[] for _ in GROUPS]
            for gi, (t1code, t2code, label, cap, keywords) in enumerate(GROUPS):
                for cand in self._query(fetcher, code, stock_id, t1code, t2code,
                                        label, keywords, cap):
                    if cand.doc_uid in seen:
                        continue
                    seen.add(cand.doc_uid)
                    groups[gi].append(cand)
            out.extend(_interleave(groups))
        return out[:limit]

    def _stock_id(self, fetcher, code: str) -> str:
        url = PREFIX_URL.format(code=quote(code))
        res = fetcher.get(url, source_id=self.source_id, expect="json")
        try:
            payload = _jsonp(res.text())
        except ValueError:
            raise NotCollected(self.source_id, url, "api_unavailable", res.status,
                               "prefix.do 未返回 JSONP") from None
        sid = next((str(r["stockId"]) for r in payload.get("stockInfo") or []
                    if str(r.get("code")) == code and r.get("stockId")), "")
        if not sid:
            raise NotCollected(self.source_id, url, "not_found", None,
                               f"{code} 在披露易 prefix.do 里没有 stockId")
        return sid

    def _query(self, fetcher, code: str, stock_id: str, t1code: str, t2code: str,
               label: str, keywords: tuple[str, ...], cap: int) -> list[Candidate]:
        from_date, to_date = _window()
        url = SEARCH_URL.format(stock_id=stock_id, from_date=from_date,
                                to_date=to_date, t1code=t1code, t2code=t2code)
        res = fetcher.get(url, source_id=self.source_id, expect="json")
        try:
            payload = json.loads(res.text())
        except json.JSONDecodeError:
            raise NotCollected(self.source_id, url, "api_unavailable", res.status,
                               "titleSearchServlet 未返回 JSON") from None
        rows = payload.get("result") or []
        if isinstance(rows, str):
            rows = json.loads(rows or "[]")

        out: list[Candidate] = []
        for r in rows:
            uid = str(r.get("NEWS_ID") or "").strip()
            link = str(r.get("FILE_LINK") or "").strip()
            title = _text(r.get("TITLE"))
            if not uid or not link or not title:
                continue
            if str(r.get("FILE_TYPE") or "").upper() != "PDF":
                continue
            blob = f"{title} {_text(r.get('LONG_TEXT'))}"
            if keywords and not all(k in blob for k in keywords):
                continue
            out.append(Candidate(
                doc_uid=uid,
                url=PDF_BASE + link,
                title=title,
                published=_published(r.get("DATE_TIME")),
                expect="pdf",
                extra={"ticker": code, "doc_type": label},
            ))
            if len(out) >= cap:
                break
        return out

    # ── 解析 ─────────────────────────────────────────────────────────

    def parse(self, cand: Candidate, res, fetcher) -> Document:
        return Document(
            source_id=self.source_id,
            doc_uid=cand.doc_uid,
            url=cand.url,
            title=cand.title,
            published=cand.published,
            ticker=canonical_ticker(cand.extra.get("ticker", "")),
            doc_type=cand.extra.get("doc_type", self.doc_type),
            lang=self.lang,
            text=pdf_to_text(res.body),
            kind=self.kind,
        )
