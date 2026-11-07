"""采集链路的离线合规守卫 —— 不发任何真实网络请求。

全部用假 session 驱动，验证的是「规则有没有被真正执行」，而不是「站点今天
通不通」：白名单、robots、限速间隔、429 退避次数、缓存命中零请求、ticker 归一。

运行: D:/python/python.exe scripts/verify_corpus.py
退出码 0 = 全部通过；1 = 有失败项。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.rag.acquire.fetcher import FetchResult, Fetcher, NotCollected  # noqa: E402
from app.rag.acquire.policy import LICENSE_CLASSES, load_sources  # noqa: E402
from app.rag.acquire.tickers import canonical_ticker  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))


class FakeResp:
    def __init__(self, status: int, body: bytes, content_type: str = "text/html",
                 headers: dict | None = None):
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", errors="replace")
        self.headers = {"Content-Type": content_type, **(headers or {})}


class FakeSession:
    """按 url 返回夹具；记录调用次数，用来断言「缓存命中时一次请求都没有」。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.calls.append(url)
        return self._route(url)

    def post(self, url, data=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append(url)
        return self._route(url)

    def _route(self, url):
        spec = self.routes.get(url)
        if spec is None:
            return FakeResp(404, b"", "text/plain")
        return spec() if callable(spec) else FakeResp(*spec)


def make_fetcher(tmp: Path, routes: dict):
    sleeps: list[float] = []
    fetcher = Fetcher(
        sources=load_sources(),
        base_dir=tmp,
        session=FakeSession(routes),
        sleep=sleeps.append,
    )
    return fetcher, fetcher._session, sleeps


ALLOW_ROBOTS = (200, b"User-agent: *\nDisallow:\n", "text/plain; charset=utf-8")


def main() -> None:
    sources = load_sources()

    # 1. 白名单本身
    check("五源登记齐全", len(sources) == 5, f"实际 {len(sources)}: {sorted(sources.sources)}")
    check("license_class 全在合规枚举内",
          all(p.license_class in LICENSE_CLASSES for p in sources),
          ",".join(sorted({p.license_class for p in sources})))
    check("研报类来源不存在（版权）",
          not any("report" in p.license_class or p.id in {"broker", "research"} for p in sources),
          "白名单无券商研报源")

    arxiv = sources.get("arxiv")
    check("arXiv 遵守 Crawl-delay=15", arxiv.interval_s >= 15.0, f"interval_s={arxiv.interval_s}")
    check("SEC 间隔不低于 5 req/s 的保守值",
          sources.get("sec_edgar").interval_s >= 0.25,
          f"interval_s={sources.get('sec_edgar').interval_s}")

    # 2. URL 白名单
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fetcher, sess, _ = make_fetcher(tmp, {"*": (200, b"ok", "text/html")})
        try:
            fetcher.get("https://www.stats.gov.cn/zcjd/x.htm", source_id="gov_cn")
            check("白名单外 host 被拒", False, "未抛异常")
        except NotCollected as e:
            check("白名单外 host 被拒", e.reason_code == "not_whitelisted", e.reason_code)
        check("白名单外 host 零网络请求", len(sess.calls) == 0, f"calls={sess.calls}")
        try:
            fetcher.get("https://evil.example.com/a.html", source_id="gov_cn")
            check("跨源取 url 被拒", False, "未抛异常")
        except NotCollected as e:
            check("跨源取 url 被拒", e.reason_code == "not_whitelisted", e.reason_code)

    # 3. robots 静态 deny 前缀：必须在发请求前就拒，且不碰 robots.txt
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fetcher, sess, _ = make_fetcher(
            tmp, {"https://www.gov.cn/robots.txt": ALLOW_ROBOTS})
        try:
            fetcher.get("https://www.gov.cn/2016zhengce/index.htm", source_id="gov_cn")
            check("/2016zhengce/ 被拒", False, "未抛异常")
        except NotCollected as e:
            check("/2016zhengce/ 被拒", e.reason_code == "robots_denied", e.reason_code)
        check("/2016zhengce/ 零网络请求", len(sess.calls) == 0, f"calls={sess.calls}")

    # 4. robots 5xx 从严禁止、4xx 视为无限制
    with tempfile.TemporaryDirectory() as td:
        fetcher, sess, _ = make_fetcher(Path(td), {
            "https://www.gov.cn/robots.txt": (503, b"", "text/plain"),
            "*": (200, b"ok", "text/html"),
        })
        try:
            fetcher.get("https://www.gov.cn/zhengce/x.htm", source_id="gov_cn")
            check("robots 5xx → 禁止", False, "未抛异常")
        except NotCollected as e:
            check("robots 5xx → 禁止", e.reason_code == "robots_denied", e.reason_code)
        check("robots 5xx 未发正文请求",
              not any(not u.endswith("/robots.txt") for u in sess.calls), f"calls={sess.calls}")

    with tempfile.TemporaryDirectory() as td:
        fetcher, sess, _ = make_fetcher(Path(td), {
            "https://www.gov.cn/robots.txt": (404, b"", "text/plain"),
            "https://www.gov.cn/zhengce/x.htm": (200, b"<html>ok</html>", "text/html"),
        })
        res = fetcher.get("https://www.gov.cn/zhengce/x.htm", source_id="gov_cn")
        check("robots 404 → 允许", res.status == 200, f"status={res.status}")

    # 5. robots 缓存 7 天：同 host 第二次不再请求 robots
    with tempfile.TemporaryDirectory() as td:
        fetcher, sess, _ = make_fetcher(Path(td), {
            "https://www.gov.cn/robots.txt": ALLOW_ROBOTS,
            "https://www.gov.cn/a.htm": (200, b"A", "text/html"),
            "https://www.gov.cn/b.htm": (200, b"B", "text/html"),
        })
        fetcher.get("https://www.gov.cn/a.htm", source_id="gov_cn")
        n1 = sess.calls.count("https://www.gov.cn/robots.txt")
        fetcher.get("https://www.gov.cn/b.htm", source_id="gov_cn")
        n2 = sess.calls.count("https://www.gov.cn/robots.txt")
        check("robots 缓存生效", n1 == 1 and n2 == 1, f"robots 请求 {n1}→{n2}")

    # 6. gzip 缓存命中：第二次同 url 零请求
    with tempfile.TemporaryDirectory() as td:
        fetcher, sess, _ = make_fetcher(Path(td), {
            "https://www.gov.cn/robots.txt": ALLOW_ROBOTS,
            "https://www.gov.cn/p.htm": (200, b"payload", "text/html; charset=utf-8"),
        })
        url = "https://www.gov.cn/p.htm"
        r1 = fetcher.get(url, source_id="gov_cn")
        calls_after_first = len(sess.calls)
        r2 = fetcher.get(url, source_id="gov_cn")
        check("缓存命中零请求", len(sess.calls) == calls_after_first,
              f"calls {calls_after_first}→{len(sess.calls)}")
        check("缓存命中标记正确", r1.cache_hit is False and r2.cache_hit is True,
              f"{r1.cache_hit}→{r2.cache_hit}")
        check("缓存内容一致", r1.body == r2.body == b"payload", f"{r2.body!r}")

    # 7. 429 退避上限：重试 max_retries 次后 not_collected，且遵守 Retry-After
    with tempfile.TemporaryDirectory() as td:
        fetcher, sess, sleeps = make_fetcher(Path(td), {
            "https://www.gov.cn/robots.txt": ALLOW_ROBOTS,
            "https://www.gov.cn/429.htm":
                (429, b"slow down", "text/plain", {"Retry-After": "2"}),
        })
        try:
            fetcher.get("https://www.gov.cn/429.htm", source_id="gov_cn")
            check("429 → rate_limited", False, "未抛异常")
        except NotCollected as e:
            check("429 → rate_limited", e.reason_code == "rate_limited", e.reason_code)
            check("429 退避次数受限", e.http_status == 429, f"status={e.http_status}")
        body_calls = [u for u in sess.calls if u.endswith("/429.htm")]
        check("429 重试不超过 max_retries", len(body_calls) == 3, f"实际 {len(body_calls)} 次")
        check("遵守 Retry-After=2s", 2.0 in sleeps, f"sleeps={sleeps}")

    # 8. 403/404 不重试
    with tempfile.TemporaryDirectory() as td:
        fetcher, sess, _ = make_fetcher(Path(td), {
            "https://www.gov.cn/robots.txt": ALLOW_ROBOTS,
            "https://www.gov.cn/403.htm": (403, b"", "text/plain"),
        })
        try:
            fetcher.get("https://www.gov.cn/403.htm", source_id="gov_cn")
            check("403 立即失败", False, "未抛异常")
        except NotCollected as e:
            check("403 立即失败", e.reason_code == "http_error", e.reason_code)
        check("403 不重试", len([u for u in sess.calls if u.endswith("/403.htm")]) == 1,
              f"calls={sess.calls}")

    # 9. 空正文仍算采集成功（由适配器决定 empty_text），不能被当作失败
    with tempfile.TemporaryDirectory() as td:
        fetcher, sess, _ = make_fetcher(Path(td), {
            "https://www.gov.cn/robots.txt": ALLOW_ROBOTS,
            "https://www.gov.cn/empty.htm": (200, b"", "text/html"),
        })
        res = fetcher.get("https://www.gov.cn/empty.htm", source_id="gov_cn")
        check("空正文不抛异常（交适配器判 empty_text）", res.status == 200 and res.body == b"",
              f"status={res.status} bytes={len(res.body)}")

    # 10. SSL 根证书
    try:
        import certifi
        check("默认 session 使用 certifi 根证书",
              Fetcher._default_session().verify == certifi.where(), "代理中间签名需 certifi")
    except Exception as e:
        check("默认 session 使用 certifi 根证书", False, str(e))

    # 11. ticker 归一
    cases = {
        "600519": "600519.SS", "600519.SH": "600519.SS", "SH600519": "600519.SS",
        "000001": "000001.SZ", "300750": "300750.SZ", "830799": "830799.BJ",
        "700": "00700.HK", "0700.HK": "00700.HK", "9988.HK": "09988.HK",
        "AAPL": "AAPL", "aapl": "AAPL", "BRK.B": "BRK.B", "": "", "???": "",
    }
    bad = {k: (canonical_ticker(k), v) for k, v in cases.items() if canonical_ticker(k) != v}
    check(f"ticker 归一 {len(cases)} 例", not bad, str(bad) if bad else "全部一致")

    # 12. SEC 适配器：doc_uid=accessionNumber、归档 URL 可复原、10-K 与 8-K 都在
    from app.rag.acquire.adapters.sec_edgar import (
        SUBMISSIONS_URL, TICKER_MAP_URL, SecEdgarAdapter)

    ticker_map = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
    recent = {
        "form": ["10-K", "8-K", "10-Q", "10-K"],
        "accessionNumber": ["0000320193-24-000123", "0000320193-25-000001",
                            "0000320193-25-000002", "0000320193-23-000111"],
        "filingDate": ["2024-11-01", "2025-01-15", "2025-02-01", "2023-11-03"],
        "reportDate": ["2024-09-28", "2025-01-10", "2024-12-28", "2023-09-30"],
        "primaryDocument": ["aapl-20240928.htm", "aapl-8k.htm", "aapl-10q.htm",
                            "aapl-20230930.htm"],
        "primaryDocDescription": ["10-K", "8-K", "10-Q", "10-K"],
    }
    sec_routes = {
        "https://www.sec.gov/robots.txt": ALLOW_ROBOTS,
        "https://data.sec.gov/robots.txt": ALLOW_ROBOTS,
        TICKER_MAP_URL: (200, json.dumps(ticker_map).encode(), "application/json"),
        SUBMISSIONS_URL.format(cik="0000320193"):
            (200, json.dumps({"filings": {"recent": recent}}).encode(), "application/json"),
    }
    with tempfile.TemporaryDirectory() as td:
        fetcher, sess, _ = make_fetcher(Path(td), sec_routes)
        adapter = SecEdgarAdapter(tickers=("AAPL",))
        cands = adapter.candidates(fetcher, 10)
        uids = [c.doc_uid for c in cands]
        forms = [c.extra["form"] for c in cands]
        check("SEC doc_uid=accessionNumber", uids and uids[0] == "0000320193-24-000123",
              str(uids[:2]))
        check("SEC 归档 URL 由 CIK+accession 复原",
              cands and cands[0].url == "https://www.sec.gov/Archives/edgar/data/320193/"
                                         "000032019324000123/aapl-20240928.htm",
              cands[0].url if cands else "无候选")
        check("SEC 同时覆盖 10-K 与 8-K", {"10-K", "8-K"} <= set(forms), str(forms))
        check("SEC 年报按提交日倒序",
              [c.published for c in cands if c.extra["form"] == "10-K"]
              == ["2024-11-01", "2023-11-03"],
              str([c.published for c in cands if c.extra["form"] == "10-K"]))
        check("SEC doc_uid 二次采集稳定",
              [c.doc_uid for c in adapter.candidates(fetcher, 10)] == uids, "两次一致")

        body = (b"<html><body><ix:header><ix:hidden><x>999999</x></ix:hidden></ix:header>"
                b"<p>Item 1. Business. Apple designs and sells consumer devices.</p>"
                b"</body></html>")
        doc = adapter.parse(cands[0], FetchResult(
            source_id="sec_edgar", url=cands[0].url, status=200, body=body,
            content_type="text/html; charset=utf-8"), fetcher)
        check("SEC 剔除内联 XBRL 隐藏上下文",
              "999999" not in doc.text and "Item 1. Business" in doc.text,
              doc.text[:60].replace("\n", " "))
        check("SEC ticker 走归一", doc.ticker == "AAPL" and doc.doc_type == "10-K",
              f"{doc.ticker}/{doc.doc_type}")

    # 13. POST：同样受白名单约束，缓存键按表单体区分
    from app.rag.acquire.adapters.cninfo import QUERY_URL as CNINFO_QUERY
    with tempfile.TemporaryDirectory() as td:
        fetcher, sess, _ = make_fetcher(Path(td), {
            "https://www.cninfo.com.cn/robots.txt": ALLOW_ROBOTS,
            CNINFO_QUERY: (200, b'{"announcements": []}', "application/json"),
        })
        try:
            fetcher.post("https://evil.example.com/x", source_id="cninfo", data={"a": "1"})
            check("POST 同样受白名单约束", False, "未抛异常")
        except NotCollected as e:
            check("POST 同样受白名单约束", e.reason_code == "not_whitelisted", e.reason_code)

        fetcher.post(CNINFO_QUERY, source_id="cninfo", data={"a": "1"})
        n1 = len(sess.calls)
        r2 = fetcher.post(CNINFO_QUERY, source_id="cninfo", data={"a": "1"})
        fetcher.post(CNINFO_QUERY, source_id="cninfo", data={"a": "2"})
        check("POST 缓存按表单体区分",
              r2.cache_hit and len(sess.calls) == n1 + 1,
              f"同体命中={r2.cache_hit}，异体新增请求={len(sess.calls) - n1}")

    # 14. 巨潮适配器：orgId 必填、摘要/英文版被剔除、PDF 解析与失败降级
    from app.rag.acquire.adapters.cninfo import TOPSEARCH_URL, CninfoAdapter
    from app.rag.acquire.pdf import pdf_to_text

    anns = {"announcements": [
        {"announcementId": "100", "announcementTitle": "贵州茅台2025年年度报告",
         "announcementTime": 1776355200000, "adjunctUrl": "finalpage/2026-04-17/100.PDF"},
        {"announcementId": "101", "announcementTitle": "贵州茅台2025年年度报告摘要",
         "announcementTime": 1776355200000, "adjunctUrl": "finalpage/2026-04-17/101.PDF"},
        {"announcementId": "102", "announcementTitle": "贵州茅台2025年年度报告（英文版）",
         "announcementTime": 1776355200000, "adjunctUrl": "finalpage/2026-04-17/102.PDF"},
    ]}
    with tempfile.TemporaryDirectory() as td:
        fetcher, sess, _ = make_fetcher(Path(td), {
            "https://www.cninfo.com.cn/robots.txt": ALLOW_ROBOTS,
            TOPSEARCH_URL: (200, json.dumps(
                [{"code": "600519", "orgId": "gssh0600519"}]).encode(), "application/json"),
            CNINFO_QUERY: (200, json.dumps(anns).encode(), "application/json"),
        })
        adapter = CninfoAdapter(tickers=("600519.SS",))
        org = adapter._org_id(fetcher, "600519")
        check("巨潮 orgId 解析", org == "gssh0600519", org)

        cands = adapter._query(fetcher, "600519", f"600519,{org}",
                               "category_ndbg_szsh", "年度报告", 4)
        check("巨潮剔除摘要与英文版", [c.doc_uid for c in cands] == ["100"],
              str([c.title for c in cands]))
        check("巨潮 PDF URL 与日期取自归档路径",
              cands and cands[0].url == "https://static.cninfo.com.cn/finalpage/2026-04-17/100.PDF"
              and cands[0].published == "2026-04-17" and cands[0].expect == "pdf",
              f"{cands[0].url} / {cands[0].published}" if cands else "无候选")

        try:
            import fitz

            pdf = fitz.open()
            pdf.new_page().insert_text((72, 72), "Guizhou Moutai annual report body")
            blob = pdf.tobytes()
            pdf.close()
        except Exception as e:
            blob = b""
            check("构造 PDF 夹具", False, str(e))
        check("PDF 能抽出正文", "Moutai" in pdf_to_text(blob) if blob else False,
              pdf_to_text(blob)[:40])
        check("非法 PDF 返回空串（→ empty_text）", pdf_to_text(b"not a pdf") == "", "已降级")

    # 15. 披露易适配器：stockId 解析、标题筛选、非 PDF 剔除、日期口径
    from app.rag.acquire.adapters.hkexnews import (PREFIX_URL, SEARCH_URL,
                                                   HkexnewsAdapter, _window)

    def _hkex_url(t1code, t2code):
        f, t = _window()
        return SEARCH_URL.format(stock_id="7609", from_date=f, to_date=t,
                                 t1code=t1code, t2code=t2code)

    prefix_body = ('callback({"more":"1","stockInfo":'
                   '[{"stockId":7609,"code":"00700","name":"騰訊控股"}]});').encode()
    annual_rows = {"result": json.dumps([{
        "NEWS_ID": "12100025", "TITLE": "2025 年報",
        "LONG_TEXT": "財務報表 - [年報]", "FILE_TYPE": "PDF",
        "DATE_TIME": "09/04/2026 17:21",
        "FILE_LINK": "/listedco/listconews/sehk/2026/0409/2026040901232_c.pdf"}])}
    ann_rows = {"result": json.dumps([
        {"NEWS_ID": "12400000", "TITLE": "二零二六年股東週年大會投票表決結果",
         "LONG_TEXT": "公告及通告 - [股東周年大會的結果]", "FILE_TYPE": "PDF",
         "DATE_TIME": "13/05/2026 20:58",
         "FILE_LINK": "/listedco/listconews/sehk/2026/0513/2026051301258_c.pdf"},
        {"NEWS_ID": "12400001", "TITLE": "股東週年大會通告",
         "LONG_TEXT": "公告及通告 - [股東周年大會通告]", "FILE_TYPE": "PDF",
         "DATE_TIME": "09/04/2026 17:31",
         "FILE_LINK": "/listedco/listconews/sehk/2026/0409/2026040901358_c.pdf"},
        {"NEWS_ID": "12400003", "TITLE": "股東週年大會投票表決結果（HTML 版）",
         "LONG_TEXT": "公告及通告 - [股東周年大會的結果]", "FILE_TYPE": "HTML",
         "DATE_TIME": "13/05/2026 20:59", "FILE_LINK": "/listedco/listconews/x.htm"},
        {"NEWS_ID": "12400004", "TITLE": "董事會會議召開日期",
         "LONG_TEXT": "公告及通告 - [董事會召開日期]", "FILE_TYPE": "PDF",
         "DATE_TIME": "24/09/2026 18:28",
         "FILE_LINK": "/listedco/listconews/sehk/2026/0924/2026092400001_c.pdf"},
    ])}
    circ_rows = {"result": json.dumps([{
        "NEWS_ID": "20000001",
        "TITLE": "發行及購回股份之一般授權、重選董事及股東週年大會通告",
        "LONG_TEXT": "通函 - [一般性授權 &amp; 回購股份]", "FILE_TYPE": "PDF",
        "DATE_TIME": "09/04/2026 17:29",
        "FILE_LINK": "/listedco/listconews/sehk/2026/0409/2026040901310_c.pdf"}])}

    with tempfile.TemporaryDirectory() as td:
        fetcher, sess, _ = make_fetcher(Path(td), {
            "https://www1.hkexnews.hk/robots.txt": ALLOW_ROBOTS,
            PREFIX_URL.format(code="00700"): (200, prefix_body,
                                              "application/javascript;charset=utf-8"),
            _hkex_url("40000", "40100"): (200, json.dumps(annual_rows).encode(),
                                          "application/json"),
            _hkex_url("10000", "-2"): (200, json.dumps(ann_rows).encode(),
                                       "application/json"),
            _hkex_url("20000", "-2"): (200, json.dumps(circ_rows).encode(),
                                       "application/json"),
        })
        adapter = HkexnewsAdapter(tickers=("700",))
        check("披露易 JSONP 能剥壳（代码→stockId）",
              adapter._stock_id(fetcher, "00700") == "7609", "stockId=7609")

        cands = adapter.candidates(fetcher, 10)
        uids = [c.doc_uid for c in cands]
        check("披露易 doc_uid=NEWS_ID 且只留 PDF",
              uids == ["12100025", "12400000", "12400001", "20000001"], str(uids))
        check("披露易剔除非 PDF 与无关标题", "12400003" not in uids and "12400004" not in uids,
              f"{len(uids)} 条")
        check("披露易投票结果/通告分型",
              [c.extra["doc_type"] for c in cands]
              == ["年报", "股东会表决结果", "股东大会通告", "股东大会通函"],
              str([c.extra["doc_type"] for c in cands]))
        check("披露易日期取 DATE_TIME（日/月/年）",
              cands[1].published == "2026-05-13", cands[1].published)
        check("披露易 PDF URL 拼在 www1 主机",
              cands[1].url == "https://www1.hkexnews.hk/listedco/listconews/"
                                "sehk/2026/0513/2026051301258_c.pdf", cands[1].url)
        check("披露易 ticker 归一为 00700.HK",
              adapter.parse(cands[0], FetchResult(
                  source_id="hkexnews", url=cands[0].url, status=200, body=b"",
                  content_type="application/pdf"), fetcher).ticker == "00700.HK",
              "00700.HK")

    # 16. arXiv 适配器：API 被 robots 禁止、改走 /list + /abs + /pdf、逐篇许可
    from app.rag.acquire.adapters.arxiv import (ABS_URL, LIST_URL, PDF_URL,
                                                ArxivAdapter, _license_label)

    arxiv_robots = ("User-agent: *\nCrawl-delay: 15\nAllow: /archive\nAllow: /list\n"
                    "Allow: /abs\nAllow: /pdf\nDisallow: /api\nDisallow: /search\n")
    API_URL = "https://arxiv.org/api/query?search_query=cat:q-fin.ST"

    def _list_html(rows):
        dts = "".join(
            f'<dt><a name="item{i}">[{i}]</a> <a href="/abs/{uid}" id="{uid}" '
            f'title="Abstract">arXiv:{uid}</a> '
            f'[<a href="/pdf/{uid}" title="Download PDF">pdf</a>]</dt>'
            f'<dd><div class="meta"><div class="list-title mathjax">'
            f'<span class="descriptor">Title:</span> {title}</div>'
            f'<div class="list-authors"><a href="x">Someone</a></div>'
            f'<div class="list-subjects"><span class="descriptor">Subjects:</span> '
            f'<span class="primary-subject">{cat}</span></div></div></dd>'
            for i, (uid, title, cat) in enumerate(rows, 1))
        return f'<html><body><dl id="articles">{dts}</dl></body></html>'

    pm_html = _list_html([
        ("2611.00001", "Portfolio Construction with Quantile Factors", "q-fin.PM"),
        ("2611.00002", "Cross-listed Paper", "q-fin.PM"),
    ])
    st_html = _list_html([
        ("2611.00002", "Cross-listed Paper", "q-fin.ST"),
        ("2611.00003", "Tail Risk in Statistical Finance", "q-fin.ST"),
    ])
    abs_html = (
        '<html><head>'
        '<meta name="citation_title" content="Portfolio Construction with Quantile Factors"/>'
        '<meta name="citation_author" content="Li, Na"/>'
        '<meta name="citation_author" content="Wang, Wu"/>'
        '<meta name="citation_date" content="2026/11/04"/>'
        '<meta name="citation_pdf_url" content="https://arxiv.org/pdf/2611.00001"/>'
        '</head><body>'
        '<div class="subjects"><span class="descriptor">Subjects:</span> '
        '<span class="primary-subject">Portfolio Management (q-fin.PM)</span>; '
        'Statistical Finance (q-fin.ST)</div>'
        '<div class="abs-license"><a href="http://creativecommons.org/licenses/by/4.0/" '
        'title="Rights to this article">view license</a></div>'
        '</body></html>'
    ).encode()

    try:
        import fitz

        _pdf = fitz.open()
        _pdf.new_page().insert_text((72, 72), "Quantile factor portfolio body " * 8)
        pdf_blob = _pdf.tobytes()
        _pdf.close()
    except Exception as e:
        pdf_blob = b""
        check("构造 arXiv PDF 夹具", False, str(e))

    abs_html_nc = abs_html.replace(
        b"http://creativecommons.org/licenses/by/4.0/",
        b"http://creativecommons.org/licenses/by-nc-nd/4.0/")

    with tempfile.TemporaryDirectory() as td:
        fetcher, sess, _ = make_fetcher(Path(td), {
            "https://arxiv.org/robots.txt": (200, arxiv_robots.encode(), "text/plain"),
            API_URL: (200, b"<feed/>", "application/atom+xml"),
            LIST_URL.format(cat="q-fin.PM"): (200, pm_html.encode(), "text/html"),
            LIST_URL.format(cat="q-fin.ST"): (200, st_html.encode(), "text/html"),
            ABS_URL.format(uid="2611.00001"): (200, abs_html, "text/html"),
            ABS_URL.format(uid="2611.00003"): (200, abs_html_nc, "text/html"),
            PDF_URL.format(uid="2611.00001"): (200, pdf_blob, "application/pdf"),
        })
        try:
            fetcher.get(API_URL, source_id="arxiv", expect="atom")
            check("arXiv API 路径被 robots 拒绝", False, "未抛异常")
        except NotCollected as e:
            check("arXiv API 路径被 robots 拒绝",
                  e.reason_code == "robots_denied", e.reason_code)
        check("arXiv API 拒绝时零正文请求",
              API_URL not in sess.calls, f"calls={len(sess.calls)}")
        check("export.arxiv.org 已移出白名单",
              not sources.get("arxiv").allows_url("https://export.arxiv.org/api/query"),
              str(sources.get("arxiv").hosts))
        check("arXiv /list /abs /pdf 均在 robots 允许内",
              all(fetcher.robots_allowed("arxiv", u) for u in (
                  LIST_URL.format(cat="q-fin.ST"),
                  ABS_URL.format(uid="2611.00001"),
                  PDF_URL.format(uid="2611.00001"))), "三条均允许")

        adapter = ArxivAdapter(categories=("q-fin.PM", "q-fin.ST"))
        cands = adapter.candidates(fetcher, 30)
        uids = [c.doc_uid for c in cands]
        check("arXiv doc_uid 不带版本号、标题去描述符",
              uids[0] == "2611.00001"
              and cands[0].title == "Portfolio Construction with Quantile Factors",
              f"{uids[0]} / {cands[0].title}")
        check("arXiv 跨分类轮转且去重",
              uids == ["2611.00001", "2611.00003", "2611.00002"],
              str(uids))
        check("arXiv 候选 url 指向 abs 页",
              cands[0].url == "https://arxiv.org/abs/2611.00001", cands[0].url)

        doc = adapter.parse(cands[0], FetchResult(
            source_id="arxiv", url=cands[0].url, status=200, body=abs_html,
            content_type="text/html; charset=utf-8"), fetcher)
        check("arXiv 元数据取自 abs 页",
              doc.title == "Portfolio Construction with Quantile Factors"
              and doc.extra["authors"] == ["Li, Na", "Wang, Wu"]
              and doc.published == "2026-11-04",
              f"{doc.published} / {doc.extra['authors']}")
        check("arXiv 逐篇许可记到 Document.license",
              doc.license == "CC BY 4.0", doc.license)
        check("arXiv 未知许可原样留 URL（不猜）",
              _license_label("https://example.org/some-license/2.0")
              == "https://example.org/some-license/2.0", "原样保留")
        check("arXiv 主分类→doc_type、宏观 ticker 为空、正文来自 PDF",
              doc.doc_type == "q-fin.PM" and doc.ticker == "" and doc.kind == "论文"
              and "Quantile factor" in doc.text,
              f"{doc.doc_type} / {doc.text[:32]}")
        nc_cand = next(c for c in cands if c.doc_uid == "2611.00003")
        try:
            adapter.parse(nc_cand, FetchResult(
                source_id="arxiv", url=nc_cand.url, status=200, body=abs_html_nc,
                content_type="text/html; charset=utf-8"), fetcher)
            check("arXiv NC 许可的论文不收", False, "未抛异常")
        except NotCollected as e:
            check("arXiv NC 许可的论文不收",
                  e.reason_code == "license_denied", e.reason_code)
        check("arXiv NC 论文零 PDF 请求",
              PDF_URL.format(uid="2611.00003") not in sess.calls,
              f"calls={len(sess.calls)}")

        check("manifest 带 license 与 extra（其他源不带 extra）",
              doc.to_dict()["license"] == "CC BY 4.0"
              and "extra" in doc.to_dict()
              and "extra" not in type(doc)(
                  source_id="gov_cn", doc_uid="1", url="u", title="t", published="",
                  ticker="", doc_type="政策文件", lang="zh", text="x" * 200,
                  kind="政策").to_dict(),
              "逐篇许可进清单，源级无多余字段")

    # 17. 入库：逐页版式清洗、页码标注、切片 id 幂等
    from app.rag.chunking import normalize_paged_text
    from app.rag.ingest.external import build_chunks
    from app.rag.store import doc_id

    header = "示例公司2024 年年度报告"
    body = [f"第{i}页正文：" + "公司经营情况说明与财务数据摘录。" * 30 for i in range(1, 5)]
    paged = "\n".join(f"{header}\n{i} / 4\n{body[i - 1]}" for i in range(1, 5))
    cleaned, starts = normalize_paged_text(paged)
    check("逐页版式：页眉页脚剔除、正文无损",
          header not in cleaned and "1 / 4" not in cleaned
          and all(b in cleaned for b in body) and len(starts) == 4,
          f"页数={len(starts)} 字符 {len(paged)}→{len(cleaned)}")

    off_seq = "\n".join(["5 / 5\n收入合计 100 万元"] * 4)
    same, off_starts = normalize_paged_text(off_seq)
    check("非 1..N 页脚序列不当作版式",
          same == off_seq and off_starts == [0], "原样返回，一字不删")

    rec = {
        "source_id": "cninfo", "doc_uid": "1225114741", "kind": "披露文件",
        "published": "2026-04-17", "ticker": "600519.SS", "doc_type": "年度报告",
        "lang": "zh", "url": "https://static.cninfo.com.cn/x.PDF",
        "license": "", "text": paged,
    }
    chunks = build_chunks(rec)
    pages = [c["metadata"]["page"] for c in chunks]
    check("切片带页码且单调不减",
          pages == sorted(pages) and pages[0] == 1 and pages[-1] == 4
          and len(set(pages)) == 4, str(pages))
    check("同一文档重复切块 id 稳定",
          [c["id"] for c in build_chunks(rec)] == [c["id"] for c in chunks],
          f"{len(chunks)} 块")
    check("id 由 kind/source/uid/序号 决定",
          chunks[0]["id"] == doc_id("披露文件", "cninfo", "1225114741", "c0"),
          chunks[0]["id"][:12])
    check("正文太短不入库", build_chunks({**rec, "text": "太短"}) == [], "返回空")

    # ── 汇总 ──
    failed = [r for r in RESULTS if not r[0]]
    for ok, name, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f"  — {detail}" if detail else ""))
    print()
    print(f"verify_corpus: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
