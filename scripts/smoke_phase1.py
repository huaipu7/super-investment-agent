"""
Phase 1 冒烟测试：统一数据层真实数据抓取验证。

覆盖三市场 + 缓存 + 清洗：
  1. AAPL（美股）— 行情/历史（yfinance 被 Yahoo 限流时自动回退 Sina）
  2. 600519（A股）— Sina 实时行情快照 + akshare 财务指标 + 历史
  3. 005827（基金）— 实时净值/历史净值/基本信息
  4. SQLite 缓存命中 + 清洗管道

说明：本网络环境中 Yahoo Finance 被 429 限流、东方财富 push2 间歇性断连，
Sina 可靠。各路径尽力抓取真实数据，网络受限项如实标注"受限"，不填假值。

运行: D:\\python\\python.exe scripts\\smoke_phase1.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.router import get_market_data
from app.data.cache import get_cache
from app.data.cleaning.pipeline import CleaningPipeline
from app.data.sources.fund_source import (
    fund_search_by_code, fund_realtime_nav, fund_historical_nav,
    fund_basic_info, fund_ratings,
)

md = get_market_data()
_results = []


def section(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def record(name, ok, detail=""):
    _results.append((name, ok))
    mark = "PASS" if ok else "LIMITED/FAIL"
    print(f"[{mark}] {name} {detail}")


def show_dict(d, label="", limit=15):
    print(f"[{label}] 字段数: {len(d)}")
    for k, v in list(d.items())[:limit]:
        print(f"    {k}: {v}")


# ── 1. AAPL 美股 ───────────────────────────────────────────────
section("1. AAPL（美股 / yfinance 优先，Sina 兜底）")

try:
    t0 = time.time()
    q = md.get_quote("AAPL")
    dt = time.time() - t0
    ok = q.get("current_price") is not None
    record("AAPL 实时行情", ok, f"price={q.get('current_price')} 来源字段={q.get('update_time','yfinance')} 耗时{dt:.1f}s")
    show_dict(q, "AAPL 行情", limit=12)
except Exception as e:
    record("AAPL 实时行情", False, f"-> {type(e).__name__}: {str(e)[:80]}")

try:
    hist = md.get_history("AAPL", "3mo")
    ok = len(hist) > 0 and "Close" in hist.columns
    record("AAPL 历史行情", ok, f"rows={len(hist)} cols={list(hist.columns)}")
except Exception as e:
    record("AAPL 历史行情", False, f"-> {type(e).__name__}: {str(e)[:80]}")

try:
    fin = md.get_financials("AAPL")
    record("AAPL 财务概览", bool(fin), f"fields={len(fin)}")
    show_dict(fin, "AAPL 财务", limit=12)
except Exception as e:
    record("AAPL 财务概览", False, f"-> {type(e).__name__}: {str(e)[:80]}（Yahoo 限流时为受限项）")

try:
    fs = md.fetch_financial_statements("AAPL")
    for name, df in fs.items():
        print(f"    [AAPL {name}] 期数: {len(df)}, 指标数: {len(df.columns)}")
    record("AAPL 财务报表", all(len(v) > 0 for v in fs.values()))
except Exception as e:
    record("AAPL 财务报表", False, f"-> {type(e).__name__}: {str(e)[:80]}（Yahoo 限流时为受限项）")

# ── 2. 600519 A股 ──────────────────────────────────────────────
section("2. 600519（A股 / Sina + akshare）")

try:
    spot = md.get_quote("600519")
    ok = spot.get("price") and spot.get("name")
    record("600519 实时行情快照", ok, f"name={spot.get('name')} price={spot.get('price')} 涨跌幅={spot.get('change_pct')}%")
    show_dict(spot, "600519 快照", limit=14)
except Exception as e:
    record("600519 实时行情快照", False, f"-> {type(e).__name__}: {str(e)[:80]}")

try:
    fin_a = md.get_financials("600519")
    ok = fin_a.get("roe") is not None
    record("600519 财务指标", ok, f"ROE={fin_a.get('roe')}% 负债率={fin_a.get('debt_ratio')}% 毛利率={fin_a.get('gross_margin')}%")
    show_dict(fin_a, "600519 财务指标", limit=12)
except Exception as e:
    record("600519 财务指标", False, f"-> {type(e).__name__}: {str(e)[:80]}")

try:
    hist_a = md.get_history("600519", "1mo")
    ok = len(hist_a) > 0 and "Close" in hist_a.columns
    record("600519 历史行情", ok, f"rows={len(hist_a)}")
except Exception as e:
    record("600519 历史行情", False, f"-> {type(e).__name__}: {str(e)[:80]}")

try:
    info_a = md.fetch_company_info("600519")
    ok = bool(info_a.get("name") or info_a.get("industry"))
    record("600519 公司信息", ok, f"name={info_a.get('name')} industry={info_a.get('industry')}")
except Exception as e:
    record("600519 公司信息", False, f"-> {type(e).__name__}: {str(e)[:80]}（东财间歇性断连时为受限项）")

# ── 3. 005827 基金 ─────────────────────────────────────────────
section("3. 005827（基金 / 天天基金）")

f = fund_search_by_code("005827")
record("005827 基金检索", f is not None, f"{f}")
if f:
    print(f"    基金简称: {f.get('基金简称')}, 类型: {f.get('基金类型')}")

nav = fund_realtime_nav("005827")
record("005827 实时净值", nav is not None and nav.get("单位净值") is not None,
       f"单位净值={nav.get('单位净值')} 累计={nav.get('累计净值')} 日期={nav.get('净值日期')}")

try:
    hnav = fund_historical_nav("005827")
    record("005827 历史净值", hnav is not None and len(hnav) > 0, f"rows={len(hnav) if hnav is not None else 0}")
except Exception as e:
    record("005827 历史净值", False, f"-> {type(e).__name__}: {str(e)[:80]}")

try:
    bi = fund_basic_info("005827")
    record("005827 基本信息", bi is not None, f"全称={bi.get('基金全称') if bi else None}")
except Exception as e:
    record("005827 基本信息", False, f"-> {type(e).__name__}: {str(e)[:80]}")

try:
    rt = fund_ratings("005827")
    record("005827 评级", rt is not None, f"{rt}")
except Exception as e:
    record("005827 评级", False, f"-> {type(e).__name__}: {str(e)[:80]}")

# ── 4. 缓存命中 + 清洗管道 ─────────────────────────────────────
section("4. SQLite 缓存命中 + 清洗管道")

cache = get_cache()
q1 = md.get_quote("600519")
cached = cache.get_dict("sina_quote", "600519") or cache.get_dict("quote", "600519")
record("SQLite 缓存命中", cached is not None, f"key={'sina_quote:600519' if cache.get_dict('sina_quote','600519') else 'quote:600519'}")

t1 = time.time()
q2 = md.get_quote("600519")
dt2 = time.time() - t1
print(f"[缓存命中] 二次 get_quote(600519) 耗时 {dt2*1000:.0f}ms（应远小于首抓）")
assert q1["price"] == q2["price"], "二次查询价格应一致（缓存命中）"

# 清洗管道：优先用真实可抓到的财务报表；都受限时用财务指标验证清洗组件逻辑
cleaned_ok = False
try:
    fs600 = md.fetch_financial_statements("600519")
    if all(len(v) > 0 for v in fs600.values()):
        pipe = CleaningPipeline(source="akshare")
        cleaned = pipe.run(fs600)
        for name, df in cleaned.items():
            print(f"    [清洗后 {name}] 期数: {len(df)}, 指标数: {len(df.columns)}")
        print(f"    [清洗报告] {pipe.report()}")
        cleaned_ok = True
        record("清洗管道(600519 财务报表)", True)
except Exception as e:
    print(f"    600519 财务报表受限: {type(e).__name__}: {str(e)[:60]}")

if not cleaned_ok:
    # 用财务指标表验证清洗组件（真实数据，非编造）
    try:
        df_idx = md.akshare.get_financial_indicators("600519")
        df = __import__("pandas").DataFrame([df_idx])
        pipe = CleaningPipeline(source="akshare")
        norm = pipe.normalizer.normalize({"income": df})
        record("清洗组件(财务指标)可用", True, f"指标行数={len(df)}")
    except Exception as e:
        record("清洗组件", False, f"-> {type(e).__name__}: {str(e)[:80]}")

# ── 汇总 ───────────────────────────────────────────────────────
print("\n" + "=" * 60)
passed = sum(1 for _, ok in _results if ok)
limited = sum(1 for _, ok in _results if not ok)
print(f"汇总: 通过 {passed} / 受限或失败 {limited}")
for name, ok in _results:
    print(f"  [{'PASS' if ok else '--'}] {name}")
print("=" * 60)

# 硬性通过条件：三市场的核心真实数据必须可用
hard_required = [
    "AAPL 实时行情", "AAPL 历史行情",
    "600519 实时行情快照", "600519 财务指标",
    "005827 实时净值", "SQLite 缓存命中",
]
missing = [n for n in hard_required if n not in [r[0] for r in _results if r[1]]]
if missing:
    print(f"\n未满足核心数据可用性: {missing}")
    sys.exit(1)
print("\nPhase 1 核心验证通过（三市场真实数据 + 缓存命中）")
