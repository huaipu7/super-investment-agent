"""
Phase 9 冒烟测试：新闻/股东大会纳入深度研究 + 四项体验优化。

覆盖：
  - 数据层：get_news（东财新闻+巨潮公告 schema）/ get_meetings（A股列表 / 非A股 None）
  - 分析层：NewsAnalyzer / MeetingAnalyzer 空数据分支（available=False，不调 LLM）
  - 深度研究：SSE 进度流事件序列（含 [3.25/7] 新闻舆情、[3.375/7] 股东大会）
  - 研报结构：news_analysis / meeting_analysis JSON + md 报告含五/六/七/八章节
  - compare 端点：<2 只 400；2 股指标矩阵与 metric_rows 对齐
  - 报告归档过滤：reports/list?ticker= 前缀过滤

运行: PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\smoke_phase9.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from api.main import app
from app.analysis.meeting import MeetingAnalyzer
from app.analysis.news import NewsAnalyzer
from app.data.router import get_market_data

_results: list[dict] = []


def record(name: str, ok: bool, detail: str = ""):
    _results.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def parse_sse(lines) -> list[dict]:
    """解析 SSE 行流为 [{event, data}]（事件间以空行分隔）"""
    blocks = []
    buf = None
    for line in lines:
        line = line.strip()
        if line.startswith("event:"):
            buf = {"event": line.split(":", 1)[1].strip()}
        elif line.startswith("data:") and buf is not None:
            try:
                buf["data"] = json.loads(line.split(":", 1)[1].strip())
            except Exception:
                buf["data"] = line.split(":", 1)[1].strip()
        elif not line and buf is not None:
            blocks.append(buf)
            buf = None
    if buf is not None:
        blocks.append(buf)
    return blocks


client = TestClient(app)


# ── 数据层 ─────────────────────────────────────────────────────

def check_data():
    print("\n=== 数据层：新闻 / 公告 / 股东大会 ===")
    md = get_market_data()

    news = md.get_news("600519", limit=5)
    arts = news.get("articles") or []
    briefs = news.get("briefings") or []
    schema_ok = all({"title", "summary", "published", "source", "url"} <= set(a) for a in arts)
    record("东财新闻 schema", len(arts) > 0 and schema_ok,
           f"{len(arts)} 篇, 巨潮公告 {len(briefs)} 条")
    if arts:
        print("   样例:", arts[0].get("published"), arts[0].get("title"))

    meets = md.get_meetings("600519")
    m_ok = isinstance(meets, list) and len(meets) > 0 and all("name" in m for m in meets)
    record("股东大会 600519", m_ok,
           f"{len(meets)} 场" if isinstance(meets, list) else type(meets).__name__)
    if isinstance(meets, list) and meets:
        print("   样例:", meets[0].get("name"), meets[0].get("start_date"))

    m_none = md.get_meetings("AAPL")
    record("非 A 股股东大会 → None", m_none is None, f"get_meetings('AAPL')={m_none}")


# ── 分析层（空数据分支，不调 LLM）─────────────────────────────

def check_analyzers():
    print("\n=== 分析层：空数据分支（不调 LLM） ===")
    na = NewsAnalyzer().analyze("测试公司", [], [])
    ok = isinstance(na, dict) and na.get("available") is False and "reason" in na
    record("NewsAnalyzer 空 → available=False", ok, f"reason={na.get('reason')}")

    ma = MeetingAnalyzer().analyze("测试公司", None)
    ok = isinstance(ma, dict) and ma.get("available") is False and "reason" in ma
    record("MeetingAnalyzer None → available=False", ok, f"reason={ma.get('reason')}")


# ── 深度研究：SSE 进度流 + 新章节（约 1-2 分钟）───────────────

def check_pipeline_sse():
    print("\n=== 深度研究：SSE 进度流 + 新章节（600519，约 1-2 分钟） ===")
    r = client.post("/api/v1/analysis/run/600519")
    record("分析启动", r.status_code == 200 and r.json().get("status") == "pending",
           str(r.json()))

    # SSE 消费完整事件序列（timeout=600s 防挂死，管线正常 ~1-2min 收尾）
    with client.stream("GET", "/api/v1/analysis/stream/600519?timeout=600") as resp:
        events = parse_sse(resp.iter_lines())
    ev_names = [e["event"] for e in events]
    prog = [e["data"] for e in events if e["event"] == "progress"]
    steps = [p.get("step") for p in prog]
    labels = [p.get("label") for p in prog]

    ok_events = ("progress" in ev_names and "status" in ev_names and events[-1]["event"] == "done")
    ok_steps = "3.25/7" in steps and "3.375/7" in steps and "done" in steps
    status_ev = next((e["data"] for e in events if e["event"] == "status"), {})
    ok_term = status_ev.get("status") == "completed"
    record("SSE 事件序列", ok_events and ok_steps and ok_term,
           f"事件={ev_names}, 步骤={steps}")
    print("   阶段:", " → ".join(labels))

    # result 结构：news_analysis / meeting_analysis
    r = client.get("/api/v1/analysis/result/600519")
    j = r.json()
    na = j.get("news_analysis") or {}
    ma = j.get("meeting_analysis") or {}
    na_ok = isinstance(na, dict) and "available" in na and "article_count" in na
    if na.get("available"):
        na_ok = na_ok and all(k in na for k in ("sentiment", "key_drivers", "summary"))
    ma_ok = isinstance(ma, dict) and "available" in ma and "meeting_count" in ma
    record("news_analysis 结构", na_ok,
           f"available={na.get('available')}, articles={na.get('article_count')}, "
           f"情绪={na.get('sentiment')}")
    record("meeting_analysis 结构", ma_ok,
           f"available={ma.get('available')}, {ma.get('meeting_count')} 场")

    # 生成报告 → md 含五/六/七/八章节
    r = client.post("/api/v1/analysis/report/600519")
    rep = r.json()
    record("报告生成", r.status_code == 200 and rep.get("report", {}).get("markdown"),
           f"{rep.get('charts_count')} 图, 格式={list(rep.get('report', {}).keys())}")
    md_path = rep.get("report", {}).get("markdown")
    if md_path:
        md_name = Path(md_path).name
        r = client.get("/api/v1/reports/download/" + md_name)
        content = r.content.decode("utf-8", errors="replace")
        ok5 = "## 五、新闻舆情" in content
        ok6 = "## 六、股东大会" in content
        ok7 = "## 七、SWOT 分析" in content
        ok8 = "## 八、风险分析" in content
        record("报告含新章节", ok5 and ok6 and ok7 and ok8,
               f"五/六/七/八 = {ok5}/{ok6}/{ok7}/{ok8}")


# ── 多股对比 ───────────────────────────────────────────────────

def check_compare():
    print("\n=== 多股对比 ===")
    r = client.post("/api/v1/compare", json={"tickers": ["600519"]})
    ok400 = r.status_code == 400 and "至少选择2只股票" in r.json().get("detail", "")
    record("<2 只 → 400", ok400, str(r.json()))

    r = client.post("/api/v1/compare", json={"tickers": ["600519", "000858"]})
    j = r.json()
    items = j.get("items", [])
    expected_keys = {row["key"] for row in j.get("metric_rows", [])}
    items_ok = len(items) == 2 and all(it.get("metrics") for it in items)
    keys_ok = all(expected_keys <= set(it.get("metrics", {}).keys()) for it in items)
    record("compare 2 股指标矩阵", items_ok and keys_ok,
           f"{len(items)} 只, 指标列={len(expected_keys)}")

    vals = {
        it["ticker"]: {"pe": it.get("metrics", {}).get("pe_ratio"),
                       "roe": it.get("metrics", {}).get("return_on_equity")}
        for it in items
    }
    print("   指标样例:", vals)
    any_metric = any(v.get("pe") is not None or v.get("roe") is not None for v in vals.values())
    record("compare 真实指标非全空", any_metric, str(vals))


# ── 报告归档过滤 ──────────────────────────────────────────────

def check_reports_filter():
    print("\n=== 报告归档过滤 ===")
    r = client.get("/api/v1/reports/list", params={"ticker": "600519"})
    names = [f["name"] for f in r.json()]
    all_prefix = len(names) > 0 and all(n.startswith("600519") for n in names)
    record("ticker=600519 过滤", r.status_code == 200 and all_prefix,
           f"{len(names)} 个, 全部前缀匹配={all_prefix}")

    r = client.get("/api/v1/reports/list", params={"ticker": "NOPE999"})
    record("无匹配 ticker → 空", r.status_code == 200 and r.json() == [], f"{len(r.json())} 个")


if __name__ == "__main__":
    check_data()
    check_analyzers()
    check_compare()
    check_pipeline_sse()
    check_reports_filter()

    passed = sum(1 for r in _results if r["ok"])
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"Phase 9 结果: {passed}/{total} 通过")
    for r in _results:
        if not r["ok"]:
            print(f"  FAIL: {r['name']} {r['detail']}")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)
