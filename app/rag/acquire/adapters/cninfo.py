"""巨潮资讯网适配器 —— 沪深交易所法定披露文件（本轮只取 600519 贵州茅台）。

发现走 hisAnnouncement/query（POST 表单，返回 JSON），正文走 static 的 PDF。
doc_uid = announcementId，跨次采集稳定。

两个实测约束（2026-11-05）：
1. stock 参数必须带 orgId（`600519,gssh0600519`），只传代码返回 0 条；
2. 年报 category 里混着「摘要」与「（英文版）」，是同一份报告的删节/译本，
   按标题剔除，免得占掉配额。

解析不出文字的 PDF（扫描件）交给 finalize 判 empty_text，不当失败。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from app.rag.acquire.adapters.base import Adapter
from app.rag.acquire.fetcher import NotCollected
from app.rag.acquire.pdf import pdf_to_text
from app.rag.acquire.tickers import canonical_ticker
from app.rag.acquire.types import Candidate, Document

QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
TOPSEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
PDF_BASE = "https://static.cninfo.com.cn/"

TICKERS = ("600519.SS",)

# (category 代码, 文档类型, 条数上限)
CATEGORIES = (
    ("category_ndbg_szsh", "年度报告", 4),
    ("category_bndbg_szsh", "半年度报告", 4),
    ("category_gddh_szsh", "股东会决议", 8),
    ("category_dshgg_szsh", "董事会决议", 9),
)

EXCLUDE_TITLE = ("摘要", "英文版", "（英文）", "(英文)")

_ADJUNCT_DATE = re.compile(r"/(\d{4}-\d{2}-\d{2})/")


def _column(ticker: str) -> str:
    """交易所栏位：沪市 sse、深市 szse；其余不猜。"""
    if ticker.endswith(".SS"):
        return "sse"
    if ticker.endswith(".SZ"):
        return "szse"
    return ""


def _published(adjunct: str, ms) -> str:
    """归档路径里的日期是站点口径，优先用它；取不到再退回时间戳。"""
    m = _ADJUNCT_DATE.search(adjunct)
    if m:
        return m.group(1)
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


class CninfoAdapter(Adapter):
    source_id = "cninfo"
    kind = "披露文件"
    doc_type = "年度报告"
    lang = "zh"

    def __init__(self, source_id: str | None = None, tickers=None):
        super().__init__(source_id)
        self.tickers = tuple(tickers or TICKERS)
        self._org_ids: dict[str, str] = {}

    def collect(self, fetcher, limit: int) -> list:
        # PDF 没有「壳页」问题，重取一次只是重复下载几百页
        return super().collect(fetcher, limit, retry_shell=False)

    # ── 发现 ─────────────────────────────────────────────────────────

    def candidates(self, fetcher, limit: int) -> list[Candidate]:
        out: list[Candidate] = []
        for ticker in self.tickers:
            code = ticker.split(".")[0]
            stock = f"{code},{self._org_id(fetcher, code)}"
            for category, label, cap in CATEGORIES:
                out.extend(self._query(fetcher, code, stock, category, label, cap))
                if len(out) >= limit:
                    return out[:limit]
        return out[:limit]

    def _org_id(self, fetcher, code: str) -> str:
        if code not in self._org_ids:
            res = fetcher.post(TOPSEARCH_URL, source_id=self.source_id,
                               data={"keyWord": code, "maxNum": "10"})
            org = next((str(r.get("orgId")) for r in json.loads(res.text())
                        if str(r.get("code")) == code and r.get("orgId")), "")
            if not org:
                raise NotCollected(self.source_id, TOPSEARCH_URL, "not_found", None,
                                   f"{code} 在巨潮 topSearch 里没有 orgId")
            self._org_ids[code] = org
        return self._org_ids[code]

    def _query(self, fetcher, code: str, stock: str, category: str,
               label: str, cap: int) -> list[Candidate]:
        res = fetcher.post(QUERY_URL, source_id=self.source_id, data={
            "pageNum": "1", "pageSize": "30", "column": _column(
                canonical_ticker(code) or code),
            "tabName": "fulltext", "plate": "", "stock": stock, "searchkey": "",
            "secdate": "", "tdate": "", "type": "", "category": category,
            "trade": "", "seDate": "", "sortName": "", "sortType": "",
            "isHLtitle": "true",
        })
        out: list[Candidate] = []
        for a in json.loads(res.text()).get("announcements") or []:
            uid = str(a.get("announcementId") or "").strip()
            adjunct = str(a.get("adjunctUrl") or "").strip()
            title = str(a.get("announcementTitle") or "").strip()
            if not uid or not adjunct or not title:
                continue
            if any(x in title for x in EXCLUDE_TITLE):
                continue
            out.append(Candidate(
                doc_uid=uid,
                url=PDF_BASE + adjunct.lstrip("/"),
                title=title,
                published=_published(adjunct, a.get("announcementTime")),
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
