# -*- coding: utf-8 -*-
"""FOMC 政策声明数据源（零 key，官方，federalreserve.gov）。

经 fomccalendars.htm 提取各次会议声明链接，取最新一份并抓取正文。
Phase 1 只保证「拿到最近一份 FOMC 声明的真实文本」；逐句政策语义解析
（鸽鹰分/政策语言抽取）属 Phase 2 rates.policy 范畴。
"""
from __future__ import annotations

import datetime as dt
import re

from app.data.cache import get_cache
from app.data.sources.fi_http import get
from app.data.vintage import build_vintage

_TTL = 86400  # 24 小时
_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_RE_HREF = re.compile(r'href="(/newsevents/pressreleases/monetary\d{8}[a-z]\.htm)"')
_DATE_RE = re.compile(r"monetary(\d{8})")


def _strip_html(html: str) -> str:
    t = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    t = re.sub(r"<style[\s\S]*?</style>", " ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    for ent, ch in [("&#8217;", "'"), ("&#8212;", "—"), ("&#8220;", "\""),
                    ("&#8221;", "\""), ("&#8216;", "'"), ("&amp;", "&")]:
        t = t.replace(ent, ch)
    return re.sub(r"\s+", " ", t).strip()


class FomcSource:
    """FOMC 政策声明。"""

    def __init__(self):
        self.cache = get_cache()

    def _calendar_hrefs(self) -> list[str]:
        cached = self.cache.get_dict("fomc_calendar_hrefs")
        if cached is None:
            r = get(_CALENDAR_URL, timeout=40)
            r.raise_for_status()
            hrefs = _RE_HREF.findall(r.text)
            self.cache.set_dict({"hrefs": hrefs}, _TTL, "fomc_calendar_hrefs")
            cached = {"hrefs": hrefs}
        return cached.get("hrefs", [])

    def fetch_latest_statement(self) -> dict:
        """取最近一份 FOMC 声明原文（净 HTML），标注 url/meeting_date/正文解析状态。"""
        hrefs = self._calendar_hrefs()
        if not hrefs:
            return {"ok": False, "error": "日历页未解析到 monetary 声明链接", "source": "fomc"}
        latest = max(hrefs)  # 日期最大者在字符串序上也是最大
        m = _DATE_RE.search(latest)
        meeting_date = (f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
                        if m else None)
        url = "https://www.federalreserve.gov" + latest
        r = get(url, timeout=40)
        r.raise_for_status()
        text = _strip_html(r.text)

        # 尽力定位正文：声明正文通常在标题后的正段落，跳过站点导航样板
        body = text
        body_parsed = False
        for marker in ("For release at", "December", "FOMC statement"):
            idx = text.rfind(marker)
            if idx > 0 and idx < len(text) - 400:
                body = text[idx:idx + 2200]
                body_parsed = False  # 仅粗略截取，不算语义解析
                break

        out = {
            "ok": True, "source": "fomc",
            "url": url, "meeting_date": meeting_date,
            "as_of": dt.date.today().isoformat(),
            "text_len": len(text),
            "body_parsed": body_parsed,
            "body": body,        # 关键字定位后的正文本（policy 计算鸽鹰分用）
            "excerpt": body[:800],
        }
        out["vintage"] = build_vintage(
            requested_as_of=None, data_observation_date=meeting_date,
            source="fomc", is_point_in_time=True,
            note="FOMC 政策声明原文（官方发布日即该政策文本的时点）",
        )
        return out
