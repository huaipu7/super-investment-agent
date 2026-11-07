"""SEC EDGAR 适配器 —— 美股法定披露文件。

发现走 submissions API（data.sec.gov），正文取 Archives 里的 primaryDocument。
doc_uid = accessionNumber（保留连字符），跨次采集稳定，重抓覆盖同一 id。

SEC 属美国联邦公开记录，《Web 抓取须知》允许抓取，但要求声明性 UA 且 ≤10 req/s；
本采集器在 sources.yaml 压到 2 req/s。伪装浏览器 UA 会被 403。
"""
from __future__ import annotations

import json
import re

from app.rag.acquire.adapters.base import Adapter, html_to_text
from app.rag.acquire.fetcher import NotCollected
from app.rag.acquire.tickers import canonical_ticker
from app.rag.acquire.types import Candidate, Document

# 计划只覆盖现有 3 标的里的两只美股
TICKERS = ("AAPL", "SE")
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{flat}/{doc}"

# 顺序即采集优先级；每表单封顶，免得 8-K/6-K 这类短文件挤掉年报
FORMS = ("10-K", "20-F", "10-Q", "6-K", "DEF 14A", "8-K")
PER_FORM_CAP = {"10-K": 2, "20-F": 2, "10-Q": 4, "6-K": 6, "DEF 14A": 1, "8-K": 4}
FORM_LABEL = {
    "10-K": "10-K 年报",
    "20-F": "20-F 年报",
    "10-Q": "10-Q 季报",
    "6-K": "6-K 临时报告",
    "DEF 14A": "委托书",
    "8-K": "8-K 重大事项",
}


def _at(recent: dict, key: str, i: int) -> str:
    """submissions.recent 是列式数组，逐列取值；越界/空值一律返回 ""。"""
    arr = recent.get(key) or []
    if i >= len(arr) or arr[i] is None:
        return ""
    return str(arr[i]).strip()


def _plain(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


class SecEdgarAdapter(Adapter):
    source_id = "sec_edgar"
    kind = "披露文件"
    doc_type = "10-K"
    lang = "en"

    def __init__(self, source_id: str | None = None, tickers=None):
        super().__init__(source_id)
        self.tickers = tuple(tickers or TICKERS)
        self._cik_map: dict[str, str] | None = None

    # ── 发现 ─────────────────────────────────────────────────────────

    def candidates(self, fetcher, limit: int) -> list[Candidate]:
        out: list[Candidate] = []
        for ticker in self.tickers:
            cik = self._cik_for(fetcher, ticker)
            out.extend(self._distribute(self._recent_by_form(fetcher, ticker, cik)))
        return out[:limit]

    def _cik_for(self, fetcher, ticker: str) -> str:
        if self._cik_map is None:
            res = fetcher.get(TICKER_MAP_URL, source_id=self.source_id, expect="json")
            raw = json.loads(res.text())
            self._cik_map = {
                str(v.get("ticker", "")).upper(): f"{int(v['cik_str']):010d}"
                for v in raw.values()
                if v.get("ticker") and v.get("cik_str") is not None
            }
        cik = self._cik_map.get(ticker.upper())
        if not cik:
            raise NotCollected(self.source_id, TICKER_MAP_URL, "not_found", None,
                               f"{ticker} 不在 SEC company_tickers.json 里")
        return cik

    def _recent_by_form(self, fetcher, ticker: str, cik: str) -> dict[str, list[Candidate]]:
        res = fetcher.get(SUBMISSIONS_URL.format(cik=cik), source_id=self.source_id,
                          expect="json")
        recent = ((json.loads(res.text()).get("filings") or {}).get("recent") or {})
        by_form: dict[str, list[Candidate]] = {f: [] for f in FORMS}
        for i, form in enumerate(recent.get("form") or []):
            if form not in PER_FORM_CAP:
                continue
            acc = _at(recent, "accessionNumber", i)
            doc = _at(recent, "primaryDocument", i)
            if not acc or not doc:
                continue
            by_form[form].append(Candidate(
                doc_uid=acc,
                url=ARCHIVE_URL.format(cik=str(int(cik)), flat=acc.replace("-", ""), doc=doc),
                published=_at(recent, "filingDate", i),
                extra={
                    "ticker": ticker,
                    "form": form,
                    "report_date": _at(recent, "reportDate", i),
                    "description": _at(recent, "primaryDocDescription", i),
                },
            ))
        for form, items in by_form.items():
            items.sort(key=lambda c: c.published, reverse=True)
            del items[PER_FORM_CAP[form]:]
        return by_form

    @staticmethod
    def _distribute(by_form: dict[str, list[Candidate]]) -> list[Candidate]:
        """按表单轮转，小 limit 下也能同时拿到年报与临时报告。"""
        out: list[Candidate] = []
        i = 0
        while True:
            added = False
            for form in FORMS:
                items = by_form.get(form) or []
                if i < len(items):
                    out.append(items[i])
                    added = True
            if not added:
                return out
            i += 1

    # ── 解析 ─────────────────────────────────────────────────────────

    def parse(self, cand: Candidate, res, fetcher) -> Document:
        body = res.text()
        if "html" in (res.content_type or "").lower() or body.lstrip()[:1] == "<":
            # ix:header 里是内联 XBRL 的隐藏上下文，直接 get_text 会把一堆
            # 无上下文的数字当正文抓进来，必须整块摘掉
            text = html_to_text(body, None, drop=("ix:header", "ix:hidden"))
        else:
            text = _plain(body)

        ticker = canonical_ticker(cand.extra.get("ticker", ""))
        form = cand.extra.get("form", "")
        period = cand.extra.get("report_date") or ""
        title = f"{ticker} {FORM_LABEL.get(form, form)}" + (f"（报告期 {period}）" if period else "")

        return Document(
            source_id=self.source_id,
            doc_uid=cand.doc_uid,
            url=cand.url,
            title=title,
            published=cand.published,
            ticker=ticker,
            doc_type=form,
            lang=self.lang,
            text=text,
            kind=self.kind,
        )
