# -*- coding: utf-8 -*-
"""Phase 0 Stage 1 —— 固收研究零 key 数据源冒烟脚本。

只做真实拉取 + 诚实标注 as-of/来源，绝不伪造。任一源失败都会如实输出原因，
不影响其它源。数值全部来自官方/可合法访问源，或由真实数据计算。

三个探针：
  1. Treasury 10Y（美国财政部每日收益率曲线，零 key，官方）
  2. EDGAR companyfacts（SEC XBRL，零 key，官方）→ 由真实财务计算杠杆率
  3. FOMC 政策信号（federalreserve.gov 声明，零 key，官方）+ CME FedWatch 尽力探测

运行：D:\\python\\python.exe scripts/smoke_fixed_income.py
"""
from __future__ import annotations

import datetime
import re
import sys
import urllib.request

import pandas as pd
import requests

UA = "Super-Investment-Agent/FixedIncome-Phase0 contact:research@example.com"


def _system_proxies() -> dict:
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}
    return {k: v for k, v in proxies.items() if v}


def _get(url: str, timeout: int = 30, **kw) -> requests.Response:
    kw.setdefault("proxies", _system_proxies() or None)
    kw.setdefault("headers", {"User-Agent": UA})
    kw.setdefault("timeout", timeout)
    return requests.get(url, **kw)


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def probe_treasury() -> dict:
    out = {"source": "US Treasury daily yield curve (home.treasury.gov)", "as_of": now_iso(), "ok": False}
    this_year = datetime.date.today().year
    this_month = datetime.date.today().strftime("%Y%m")
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{this_year}/all"
        f"?type=daily_treasury_yield_curve&field_tdr_date_value_month={this_month}&page&_format=csv"
    )
    try:
        r = _get(url)
        r.raise_for_status()
        df = pd.read_csv(pd.io.common.StringIO(r.text))
        out["rows"] = len(df)
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date"]).sort_values("Date")
        out["as_of"] = df["Date"].max().date().isoformat()
        if "10 Yr" in df.columns:
            last = df.dropna(subset=["10 Yr"]).iloc[-1]
            out["ok"] = True
            out["10Y"] = float(last["10 Yr"])
            out["10Y_date"] = last["Date"].date().isoformat()
            out["curve_tail"] = {c: (None if pd.isna(last[c]) else float(last[c]))
                                 for c in df.columns if c != "Date"}
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _edgar_series(data: dict, metric: str) -> list[dict]:
    """取某指标全部记录（含 end/filed/frame），供下游挑选。"""
    try:
        units = data["facts"]["us-gaap"][metric]["units"]
        _, arr = next(iter(units.items()))
        return arr
    except Exception:
        return []


def _edgar_latest_by_end(data: dict, metric: str, n: int = 8) -> list[dict]:
    """按 end 去重（保留最新 filed），按 end 从新到旧取 n 条。处理时点(instant)指标。"""
    recs = _edgar_series(data, metric)
    by_end: dict[str, dict] = {}
    for r in recs:
        en = r.get("end")
        if not en:
            continue
        prev = by_end.get(en)
        if prev is None or (r.get("filed") or "") > (prev.get("filed") or ""):
            by_end[en] = r
    return sorted(by_end.values(), key=lambda r: r["end"], reverse=True)[:n]


def _edgar_annual(data: dict, metric: str, recent: int = 6) -> list[dict]:
    """取带纯年度 frame（CY#### / FY####）的记录，用于持续型(duration)指标如营收。"""
    recs = _edgar_series(data, metric)
    by_end: dict[str, dict] = {}
    for r in recs:
        if not re.match(r"(CY|FY)\d{4}$", r.get("frame") or ""):
            continue
        by_end[r["end"]] = r
    return sorted(by_end.values(), key=lambda r: r["end"])[-recent:]


def probe_edgar(cik: str = "0000320193", ticker: str = "AAPL") -> dict:
    out = {"source": "SEC EDGAR companyfacts (data.sec.gov)", "cik": cik, "ticker": ticker,
           "as_of": now_iso(), "ok": False}
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        r = _get(url)
        r.raise_for_status()
        data = r.json()
        rev = (_edgar_annual(data, "RevenueFromContractWithCustomerExcludingAssessedTax")
               or _edgar_annual(data, "Revenues"))
        assets = _edgar_latest_by_end(data, "Assets")
        liab = _edgar_latest_by_end(data, "Liabilities")
        out["ok"] = True
        out["revenue_annual"] = rev
        out["assets_latest"] = assets[:3]
        out["liabilities_latest"] = liab[:3]
        # 用同一财务测算杠杆率 Liabilities / Assets（取两方最近共同 end）
        a_by_end = {a["end"]: a for a in assets}
        l_by_end = {b["end"]: b for b in liab}
        calc = []
        for end, a in a_by_end.items():
            if end in l_by_end and l_by_end[end]["val"]:
                aa, ll, filed = a["val"], l_by_end[end]["val"], a["filed"]
                calc.append({"fiscal_end": end, "filed": filed,
                             "leverage_Liabilities_over_Assets": round(ll / aa, 4)})
        out["ratios"] = calc[:4]
        last = rev[-1] if rev else None
        if last:
            out["latest_revenue"] = {k: last[k] for k in ("frame", "val", "end", "filed")}
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def probe_policy() -> dict:
    out = {"source": "policy signal probe", "as_of": now_iso(), "ok": False}
    # A) CME FedWatch —— 尽力探测（该源 JS/反爬密集、本网络 ConnectTimeout，失败即如实标注）
    cme_note = ""
    for u in ["https://www.cmegroup.com/services/fedwatch/",
              "https://www.cmegroup.com/CmeWS/mvc/xsltTransformer.do"
              "?xlstDoc=CmeWS/CmeWS/xslt/xslitec/fedWatchToolDashboard.xslc"]:
        try:
            r = _get(u, timeout=15)
            if r.status_code == 200 and r.text.strip():
                cme_note = "reachable (需 Phase2 解析)"
                break
        except Exception as e:
            cme_note = f"{type(e).__name__}(网络拒连)"
    if not cme_note:
        cme_note = "CME FedWatch 未能直接取到（JS/反爬/网络），标 Phase2 攻坚或改换替代源"
    out["cme_note"] = cme_note

    # B) FOMC 最新政策声明（fomccalendars.htm 提取会议声明链接，零 key，官方）
    try:
        cal = _get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", timeout=30)
        cal.raise_for_status()
        hrefs = re.findall(r'href="(/newsevents/pressreleases/monetary\d{8}[a-z]\.htm)"', cal.text)
        if hrefs:
            latest = max(hrefs)
            body = _get("https://www.federalreserve.gov" + latest, timeout=30)
            body.raise_for_status()
            text = re.sub(r"<[^>]+>", " ", body.text)
            text = text.replace("&#8217;", "'").replace("&#39;", "'")
            text = re.sub(r"\s+", " ", text).strip()
            m = re.search(r"(Information received[\s\S]{0,1800})", text)
            excerpt = (m.group(1) if m else text[:900])
            out["fomc"] = {"url": "https://www.federalreserve.gov" + latest, "excerpt": excerpt[:900]}
            out["ok"] = True
        else:
            out["fomc_error"] = "日历页未解析到 monetary 声明链接"
    except Exception as e:
        out["fomc_error"] = f"{type(e).__name__}: {e}"
    return out


def _print(title: str, d: dict) -> None:
    print(f"-- {title}:")
    for k, v in d.items():
        if isinstance(v, (int, float, str, bool)) or v is None:
            print(f"   {k}: {v}")
        elif isinstance(v, list):
            print(f"   {k}: ({len(v)} 条)")
            for i in v[-3:]:
                print(f"      {i}")
        elif isinstance(v, dict):
            print(f"   {k}: { {kk: vv for kk, vv in v.items()} }")


def main() -> int:
    # MS 控制台默认 GBK，中文会乱码：兜底切 UTF-8（保持既有项目做法）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("=" * 70)
    print("固收零 key 数据源冒烟 (Phase 0 Stage 1)  运行时间:", now_iso())
    print("=" * 70)
    print("\n[1/3] Treasury 10Y ...")
    t = probe_treasury()
    _print("TRESURY", t)
    print("\n[2/3] EDGAR AAPL companyfacts ...")
    e = probe_edgar()
    _print("EDGAR", e)
    print("\n[3/3] 政策信号 (FOMC 声明 + CME FedWatch 尽力) ...")
    p = probe_policy()
    _print("POLICY", p)
    print("\n" + "=" * 70)
    ok = [k for k, v in [("treasury", t["ok"]), ("edgar", e["ok"]), ("policy", p["ok"])] if v]
    print(f"结果：成功 {len(ok)}/{3}   [{', '.join(ok) if ok else '无'}]")
    print("说明：数值均来自真实源或真实计算；failed 项如实标注原因，未做任何伪造。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
