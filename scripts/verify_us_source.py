"""美股数据源（东方财富 F10）验证 —— 代码判定 / 分表映射 / 长转宽 / 路由 / 诚实降级。

Part A —— is_us_ticker：美股与 A 股/港股代码的边界判定。
Part B —— EM_US_METRIC_MAP：按报表分表、无跨表口径污染、优先级有据。
Part C —— _to_wide：长表→宽表、同码取舍优先级、脏输入与空输入。
Part D —— 诚实降级：非美股代码返回空；akshare 抛错时返回空而不抛。
Part E —— 账务恒等式（合成夹具）：资产=负债+权益、毛利=收入-成本。
Part F —— router 路由：美股走新源且不调 yfinance；港股仍回退 yfinance；A 股不受影响。
Part G —— 活体（可选，网络失败如实标注而不断言）：真实拉取 SE 三表。
Part H —— 报表币种 vs 挂牌币种：ADR（BABA=CNY / GRVY=KRW）不得被当成美元。


除 Part G 外全部离线：不联网、不调 LLM、不碰 SQLite。
运行: D:\\python\\python.exe scripts\\verify_us_source.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.data.sources.akshare_us_source import (  # noqa: E402
    AKShareUSSource, EM_US_METRIC_MAP, _PRIORITY, _to_wide,
    is_us_ticker, normalize_us_ticker,
)

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    """断言助手。`bool(ok)` 是刻意的：断言表达式常以字符串/列表结尾，
    那是真值但不是 bool，直接累加会 TypeError。"""
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _long(rows: list[dict]) -> pd.DataFrame:
    """构造东财美股长表夹具（列名与真实返回一致）。"""
    return pd.DataFrame(rows)


def _row(item: str, amount, date: str = "2025-12-31", code: str = "004001001") -> dict:
    return {
        "REPORT_DATE": pd.Timestamp(date), "REPORT": date[:4] + "/FY",
        "STD_ITEM_CODE": code, "ITEM_NAME": item, "AMOUNT": amount,
    }


# ── Part A: 代码判定 ─────────────────────────────────────────

def part_a():
    print("\n=== Part A: is_us_ticker ===")
    for t in ["SE", "AAPL", "aapl", "TCEHY", "BRK.B", "BRK-B", "BRK_B"]:
        check(f"A 美股代码 {t!r} 判定为真", is_us_ticker(t) is True)
    for t in ["0700.HK", "600519.SS", "600519.SH", "000001.SZ", "600519",
              "0700", "", "  ", "TOOLONGTICKER"]:
        check(f"A 非美股代码 {t!r} 判定为假", is_us_ticker(t) is False)
    check("A 前后空白可容忍", is_us_ticker("  se  ") is True)

    # 股份类别写法归一：实测东财只认下划线（BRK_B 可用，BRK.B / BRK-B 不可用）
    for raw in ["BRK.B", "brk-b", " BRK_B "]:
        check(f"A {raw!r} 归一为 BRK_B", normalize_us_ticker(raw) == "BRK_B",
              f"实际 {normalize_us_ticker(raw)!r}")


# ── Part B: 映射表 ───────────────────────────────────────────

def part_b():
    print("\n=== Part B: EM_US_METRIC_MAP 分表映射 ===")
    check("B 三个报表各有映射表",
          set(EM_US_METRIC_MAP) == {"income", "balance_sheet", "cash_flow"},
          f"实际 {sorted(EM_US_METRIC_MAP)}")
    check("B 各表均非空", all(EM_US_METRIC_MAP[k] for k in EM_US_METRIC_MAP))

    # 跨表口径污染是本次修掉的 bug：现金流量表的「存货/净利润」不是资产负债表口径
    cf = EM_US_METRIC_MAP["cash_flow"]
    check("B 现金流量表不含「存货」(营运资金变动额≠存货余额)", "存货" not in cf)
    check("B 现金流量表不含「净利润」(现金流起点≠利润表口径)", "净利润" not in cf)
    check("B 现金流量表不产出 INVENTORY", "INVENTORY" not in cf.values())
    check("B 现金流量表不产出 NET_INCOME", "NET_INCOME" not in cf.values())

    check("B 资产负债表含「存货」→ INVENTORY",
          EM_US_METRIC_MAP["balance_sheet"].get("存货") == "INVENTORY")
    check("B 利润表含「归属于母公司股东净利润」→ NET_INCOME",
          EM_US_METRIC_MAP["income"].get("归属于母公司股东净利润") == "NET_INCOME")

    # 优先级必须有实据：SET(S) 里每个被取舍的科目都要在 _PRIORITY 里有明确序号
    for stmt, m in EM_US_METRIC_MAP.items():
        dup = {}
        for item, code in m.items():
            dup.setdefault(code, []).append(item)
        for code, items in dup.items():
            if len(items) > 1:
                missing = [i for i in items if i not in _PRIORITY]
                check(f"B {stmt}:{code} 同码科目均有优先级", not missing,
                      f"缺 {missing}")

    prios = [_PRIORITY["归属于母公司股东净利润"], _PRIORITY["净利润"]]
    check("B NET_INCOME 取归母优先（与 A 股 PARENT_NETPROFIT 口径一致）",
          prios[0] < prios[1])
    prios = [_PRIORITY["股东权益合计"], _PRIORITY["归属于母公司股东权益"]]
    check("B TOTAL_EQUITY 取合计优先（保持 资产=负债+权益）", prios[0] < prios[1])


# ── Part C: 长转宽 ───────────────────────────────────────────

def part_c():
    print("\n=== Part C: _to_wide ===")
    check("C 空输入 → 空表", _to_wide(pd.DataFrame(), "income").empty)
    check("C None 输入 → 空表", _to_wide(None, "income").empty)
    check("C 缺列输入 → 空表",
          _to_wide(pd.DataFrame({"ITEM_NAME": ["毛利"]}), "income").empty)
    check("C 未知报表名 → 空表",
          _to_wide(_long([_row("毛利", 1)]), "unknown_stmt").empty)

    # 同码取舍：营业收入(合计) 应压过 主营收入
    df = _to_wide(_long([_row("主营收入", 100), _row("营业收入", 999)]), "income")
    check("C 同码取优先级高的（营业收入 > 主营收入）",
          len(df) == 1 and df.iloc[0]["REVENUE"] == 999,
          f"实际 {df.iloc[0]['REVENUE'] if len(df) else '空'}")

    # 未收录科目直接丢弃
    df = _to_wide(_long([_row("非运算项目", 5), _row("毛利", 7)]), "income")
    check("C 未收录科目被丢弃", list(df.columns) == ["GROSS_PROFIT"])

    # 多期排序
    df = _to_wide(_long([
        _row("毛利", 10, "2024-12-31"), _row("毛利", 20, "2025-12-31"),
    ]), "income")
    check("C 多期升序排序", list(df.index.year) == [2024, 2025])

    # 脏值：不可解析日期 / 非数字金额
    df = _to_wide(_long([
        {"REPORT_DATE": "not-a-date", "ITEM_NAME": "毛利", "AMOUNT": 1},
        _row("毛利", "abc", "2025-12-31"),
    ]), "income")
    check("C 脏日期行被剔除、非数字金额转 NaN", df.empty or df.iloc[0]["GROSS_PROFIT"] != df.iloc[0]["GROSS_PROFIT"])

    # 现金流量表的「存货」不应进入宽表
    df = _to_wide(_long([_row("存货", 3), _row("经营活动产生的现金流量净额", 50)]),
                  "cash_flow")
    check("C 现金流量表的「存货」不进宽表",
          "INVENTORY" not in df.columns and "OPERATING_CASH_FLOW" in df.columns)


# ── Part D: 诚实降级 ─────────────────────────────────────────

def part_d():
    print("\n=== Part D: 诚实降级 ===")
    s = AKShareUSSource()
    for t in ["0700.HK", "600519.SS", "600519"]:
        st = s.fetch_all_financials(t)
        check(f"D 非美股 {t} 三表全空且不抛",
              all(v.empty for v in st.values()) and set(st) == {"income", "balance_sheet", "cash_flow"})

    info = s.fetch_company_info("0700.HK")
    check("D 非美股 company_info 返回骨架不抛", isinstance(info, dict) and info.get("name") == "0700.HK")
    check("D 非美股 shares_outstanding 为 None（不编造）", info.get("shares_outstanding") is None)

    check("D search_competitors 返回空列表", s.search_competitors("SE") == [])
    check("D fetch_news 返回空列表", s.fetch_news("SE") == [])

    # akshare 抛错时必须吞掉并返回空，绝不让异常冒到管线
    import app.data.sources.akshare_us_source as mod
    orig = mod._retry_em
    mod._retry_em = lambda fn, *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        st = s.fetch_all_financials("SE")
        check("D akshare 抛错时返回空表而不抛", all(v.empty for v in st.values()))
        check("D akshare 抛错时日线也返回空", s.fetch_price_history("SE").empty)
    finally:
        mod._retry_em = orig


# ── Part E: 账务恒等式 ───────────────────────────────────────

def part_e():
    print("\n=== Part E: 账务恒等式（合成夹具） ===")
    inc = _to_wide(_long([
        _row("营业收入", 1000), _row("营业成本", 400),
    ]), "income")
    check("E 毛利由科目直取", inc.iloc[0]["GROSS_PROFIT"] == 600)
    check("E 收入-成本 = 毛利",
          inc.iloc[0]["REVENUE"] - inc.iloc[0]["COST_OF_REVENUE"] == inc.iloc[0]["GROSS_PROFIT"])

    bal = _to_wide(_long([
        _row("总资产", 1000), _row("总负债", 600), _row("股东权益合计", 400),
        _row("归属于母公司股东权益", 350),
    ]), "balance_sheet")
    check("E 资产 = 负债 + 权益",
          bal.iloc[0]["TOTAL_ASSETS"] == bal.iloc[0]["TOTAL_LIABILITIES"] + bal.iloc[0]["TOTAL_EQUITY"])
    check("E TOTAL_EQUITY 取合计(400)而非归母(350)", bal.iloc[0]["TOTAL_EQUITY"] == 400)

    inc2 = _to_wide(_long([
        _row("净利润", 120), _row("归属于母公司股东净利润", 100),
    ]), "income")
    check("E NET_INCOME 取归母(100)而非总额(120)", inc2.iloc[0]["NET_INCOME"] == 100)


# ── Part F: router 路由 ──────────────────────────────────────

class _NullCache:
    """禁用缓存的替身，确保路由测试每次都真正走到数据源。"""
    def get_dict(self, *a, **k): return None
    def set_dict(self, *a, **k): return None
    def get_dataframe(self, *a, **k): return None
    def set_dataframe(self, *a, **k): return None


class _SpySource:
    """记录被调用次数的数据源替身。"""
    def __init__(self, frames=None, info=None):
        self.calls: list[str] = []
        self._frames = frames
        self._info = info

    def fetch_all_financials(self, ticker, period="annual"):
        self.calls.append(f"financials:{ticker}")
        if self._frames is not None:
            return self._frames
        return {k: pd.DataFrame() for k in ("income", "balance_sheet", "cash_flow")}

    def fetch_company_info(self, ticker):
        self.calls.append(f"info:{ticker}")
        return self._info if self._info is not None else {"name": ticker.strip().upper()}


def part_f():
    print("\n=== Part F: router 路由 ===")
    import app.data.router as router_mod

    orig_get_cache = router_mod.get_cache
    router_mod.get_cache = lambda: _NullCache()
    try:
        _check_routes(router_mod)
    finally:
        router_mod.get_cache = orig_get_cache


def _check_routes(router_mod):
    real_frame = pd.DataFrame({"REVENUE": [1.0]}, index=pd.to_datetime(["2025-12-31"]))

    # 美股：新源有数据 → 命中新源，且完全不碰 yfinance
    md = router_mod.MarketData()
    us = _SpySource(frames={k: real_frame.copy() for k in ("income", "balance_sheet", "cash_flow")})
    yf = _SpySource()
    md._us, md._yf = us, yf
    st = md.fetch_financial_statements("SE")
    check("F 美股 命中新源", us.calls == ["financials:SE"], f"实际 {us.calls}")
    check("F 美股 未回退 yfinance", yf.calls == [], f"实际 {yf.calls}")
    check("F 美股 返回非空", not st["income"].empty)

    # 港股：新源返回空 → 回退 yfinance
    md2 = router_mod.MarketData()
    us2, yf2 = _SpySource(), _SpySource(frames={k: real_frame.copy() for k in ("income", "balance_sheet", "cash_flow")})
    md2._us, md2._yf = us2, yf2
    st2 = md2.fetch_financial_statements("0700.HK")
    check("F 港股 先试新源再回退 yfinance",
          us2.calls == ["financials:0700.HK"] and yf2.calls == ["financials:0700.HK"],
          f"实际 us={us2.calls} yf={yf2.calls}")
    check("F 港股 回退后拿到数据", not st2["income"].empty)

    # 两边都空 → 诚实返回空，不抛
    md3 = router_mod.MarketData()
    md3._us, md3._yf = _SpySource(), _SpySource()
    st3 = md3.fetch_financial_statements("SE")
    check("F 两源皆空 → 返回空表且不抛", all(v.empty for v in st3.values()))

    # yfinance 抛错 → 吞掉并返回空
    class _Boom(_SpySource):
        def fetch_all_financials(self, ticker, period="annual"):
            self.calls.append(f"financials:{ticker}")
            raise RuntimeError("yf boom")

    md4 = router_mod.MarketData()
    md4._us, md4._yf = _SpySource(), _Boom()
    try:
        st4 = md4.fetch_financial_statements("0700.HK")
        check("F yfinance 抛错 → 吞掉返回空", all(v.empty for v in st4.values()))
    except Exception as e:
        check("F yfinance 抛错 → 吞掉返回空", False, f"抛了 {type(e).__name__}")

    # A 股：不应碰新源
    md5 = router_mod.MarketData()
    us5 = _SpySource()
    class _Ak(_SpySource):
        def fetch_all_financials(self, ticker, period="annual"):
            self.calls.append(f"financials:{ticker}")
            return {k: real_frame.copy() for k in ("income", "balance_sheet", "cash_flow")}
    md5._us, md5._ak = us5, _Ak()
    md5.fetch_financial_statements("600519.SS")
    check("F A 股 不经过美股新源", us5.calls == [], f"实际 {us5.calls}")

    # company_info：美股命中新源
    md6 = router_mod.MarketData()
    us6 = _SpySource(info={"name": "Sea Ltd.", "market_cap": 1.0})
    yf6 = _SpySource()
    md6._us, md6._yf = us6, yf6
    ci = md6.fetch_company_info("SE")
    check("F 美股 company_info 命中新源", us6.calls == ["info:SE"] and ci["name"] == "Sea Ltd.")

    # company_info：港股新源未命中(name 仍是代码) → 回退 yfinance
    md7 = router_mod.MarketData()
    us7 = _SpySource()
    yf7 = _SpySource(info={"name": "腾讯控股", "industry": "软件"})
    md7._us, md7._yf = us7, yf7
    ci7 = md7.fetch_company_info("0700.HK")
    check("F 港股 company_info 回退 yfinance", yf7.calls == ["info:0700.HK"], f"实际 {yf7.calls}")
    check("F 港股 company_info 拿到名称", ci7.get("name") == "腾讯控股")

    # company_info：yfinance 抛错 → 返回东财骨架，不抛
    md8 = router_mod.MarketData()
    class _BoomInfo(_SpySource):
        def fetch_company_info(self, ticker):
            self.calls.append(f"info:{ticker}")
            raise RuntimeError("yf info boom")
    md8._us, md8._yf = _SpySource(), _BoomInfo()
    try:
        ci8 = md8.fetch_company_info("0700.HK")
        check("F company_info yfinance 抛错 → 吞掉返回骨架", isinstance(ci8, dict))
    except Exception as e:
        check("F company_info yfinance 抛错 → 吞掉返回骨架", False, f"抛了 {type(e).__name__}")


# ── Part G: 活体（可选）──────────────────────────────────────

def part_g():
    print("\n=== Part G: 活体（真实拉取 SE，失败如实标注不断言） ===")
    s = AKShareUSSource()
    try:
        st = s.fetch_all_financials("SE")
        inc = st["income"]
        if inc.empty:
            print("[SKIP] G 活体：东财未返回数据（网络不可达或接口变更），不作断言")
            return
        last = inc.index.max()
        check("G 活体 利润表非空", not inc.empty, f"最新期 {last.date()}")
        check("G 活体 有 REVENUE", "REVENUE" in inc.columns and pd.notna(inc.loc[last, "REVENUE"]))
        check("G 活体 有净利润", "NET_INCOME" in inc.columns and pd.notna(inc.loc[last, "NET_INCOME"]))
        if {"REVENUE", "COST_OF_REVENUE", "GROSS_PROFIT"}.issubset(inc.columns):
            r = inc.loc[last]
            diff = abs(r["REVENUE"] - r["COST_OF_REVENUE"] - r["GROSS_PROFIT"])
            check("G 活体 毛利 = 收入 - 成本", diff < 1, f"差 {diff:,.0f}")
        bal = st["balance_sheet"]
        if not bal.empty and {"TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY"}.issubset(bal.columns):
            b = bal.loc[bal.index.max()]
            diff = abs(b["TOTAL_ASSETS"] - b["TOTAL_LIABILITIES"] - b["TOTAL_EQUITY"])
            check("G 活体 资产 = 负债 + 权益", diff < 1, f"差 {diff:,.0f}")
    except Exception as e:
        print(f"[SKIP] G 活体：{type(e).__name__}: {e}（不作断言）")


def part_h():
    print("\n=== Part H: 报表币种 vs 挂牌币种（防 ADR 数量级错误） ===")
    s = AKShareUSSource()

    # 非美股一律 None / 空字典，不猜
    for t in ["0700.HK", "600519.SS", "600519"]:
        check(f"H 非美股 {t} 无报表币种", s.fetch_reporting_currency(t) is None)
        check(f"H 非美股 {t} get_financials 为空", s.get_financials(t) == {})

    # 活体：SE=USD、BABA=CNY、GRVY=KRW。
    # 期望值取自东财 CURRENCY_ABBR，是外部事实而非本仓计算，故可直接断言。
    expect = {"SE": "USD", "BABA": "CNY", "GRVY": "KRW", "MELI": "USD"}
    got_any = False
    for t, exp in expect.items():
        try:
            cur = s.fetch_reporting_currency(t)
        except Exception:
            cur = None
        if cur is None:
            continue
        got_any = True
        check(f"H 活体 {t} 报表币种 = {exp}", cur == exp, f"实际 {cur}")
    if not got_any:
        print("[SKIP] H 活体：网络不可达，跳过币种断言")

    # 关键不变量：get_financials 的 currency 绝不能是行情挂牌币种(USD)覆盖后的结果
    try:
        f_baba = s.get_financials("BABA")
        if f_baba:
            check("H 活体 BABA get_financials.currency 为报表币种 CNY 而非挂牌 USD",
                  f_baba.get("currency") == "CNY", f"实际 {f_baba.get('currency')}")
            check("H 活体 BABA 营收量级为人民币口径(>1e12)",
                  (f_baba.get("revenue") or 0) > 1e12,
                  f"实际 {f_baba.get('revenue')}")
    except Exception as e:
        print(f"[SKIP] H 活体 BABA：{type(e).__name__}: {e}")


def part_i():
    print("\n=== Part I: 竞品 PE 覆盖后摘要重建（防自相矛盾） ===")
    from app.analysis.pipeline import _refresh_relative_valuation_summary as refresh

    def _vr(summary, rel):
        return {"summary": summary, "relative_valuation": rel}

    # 覆盖前摘要写 PE +146.0%，覆盖后 rel 的 PE 溢价变了 → 摘要必须跟着变
    vr = _vr(
        "DCF 内在价值 $119.02，当前价格 $101.79，低估（空间 +16.9%）；"
        "相对估值: PE +146.0%，PB +96.4%，PS +35.4%"
        "；供应链风险溢价 1.20%",
        {"pe": {"company": 39.42, "industry_avg": 39.1, "premium": 0.0082},
         "pb": {"company": 4.92, "industry_avg": 2.5, "premium": 0.9676},
         "ps": {"company": 2.71, "industry_avg": 2.0, "premium": 0.3562},
         "ev_ebitda": {"company": None, "industry_avg": 11.0, "premium": None}},
    )
    refresh(vr)
    s = vr["summary"]
    check("I 摘要 PE 溢价被重建为 +0.8%", "PE +0.8%" in s, s)
    check("I 不再残留旧口径 +146.0%", "+146.0%" not in s)
    check("I PB/PS 保持", "PB +96.8%" in s and "PS +35.6%" in s)
    check("I 供应链风险溢价一项未被破坏", "供应链风险溢价 1.20%" in s)
    check("I DCF 一项未被破坏", "DCF 内在价值 $119.02" in s)
    check("I 分段顺序保持（DCF→相对估值→供应链）",
          s.index("DCF") < s.index("相对估值") < s.index("供应链"))
    check("I ev_ebitda(premium=None) 不被写入", "EV_EBITDA" not in s)

    # 幂等：再调一次结果不变
    once = vr["summary"]
    refresh(vr)
    check("I 幂等", vr["summary"] == once)

    # 无限溢价可重建（不应写入空段）
    vr2 = _vr("DCF 不适用（FCF 不足或为负）",
              {"pe": {"company": None, "industry_avg": 20.0, "premium": None}})
    refresh(vr2)
    check("I 全部无溢价时不追加空段", vr2["summary"] == "DCF 不适用（FCF 不足或为负）",
          vr2["summary"])

    # 摘要原本没有「相对估值」一项时，追加而非报错
    vr3 = _vr("DCF 不适用", {"pe": {"company": 30.0, "industry_avg": 20.0, "premium": 0.5}})
    refresh(vr3)
    check("I 原无该项时追加", vr3["summary"] == "DCF 不适用；相对估值: PE +50.0%", vr3["summary"])

    # 空输入安全
    vr4 = {"summary": "", "relative_valuation": {}}
    refresh(vr4)
    check("I 空输入不报错", vr4["summary"] == "")


def main() -> int:
    print("美股数据源（东方财富 F10）验证")
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()
    part_f()
    part_g()
    part_h()
    part_i()
    print(f"\n{'=' * 60}\n通过 {passed} / 失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
