"""
Phase 4 冒烟测试：对话 Agent 层。

验证内容：
  A. 逐项比对（与原 Violetz 直接 import 运行，同一输入对比输出）
     - planner.build_plan 全意图
     - VioletzScorer（各指标评分函数 + compute 全流程）
     - FundScorer（全流程）
     - ambiguity（get_market_label / 歧义检测 / 澄清解析 / 问题构建）
  B. 意图识别（LLM 三层降级：Function Calling → JSON → 规则）
     - 查行情 / 深度分析 / 对比 / 基金净值 / 歧义检测
  C. 端到端三类对话（真实数据）
     - 查行情 AAPL（yfinance）
     - 深度分析 600519（A股，Sina+东财，经统一数据层）
     - 基金净值 005827（天天基金）
  D. SSE 流式生成器（stream_fund_report_tokens / stream_analysis_report）

运行: PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\smoke_phase4.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 原项目（用于逐项比对，直接 import，保证比对对象是原代码）
ORIG = Path(r"D:\Desktop\ClaudeProjects\Agent-for-Investment")
sys.path.insert(0, str(ORIG))

from app.agent import intent as intent_mod  # noqa: E402
from app.agent import planner, expressor, runner, scorer, ambiguity  # noqa: E402
from app.agent.fund import intent as fund_intent  # noqa: E402
from app.agent.fund import (  # noqa: E402
    planner as fund_planner,
    expressor as fund_expressor,
    runner as fund_runner,
    scorer as fund_scorer,
)

# 原版模块
import workshops.planner as orig_planner  # noqa: E402
import workshops.scorer as orig_scorer  # noqa: E402
import workshops.fund_scorer as orig_fund_scorer  # noqa: E402
import workshops.ambiguity as orig_ambiguity  # noqa: E402

_results: list[dict] = []


def record(name: str, ok: bool, detail: str = ""):
    _results.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def eq(a, b):
    return a == b


# ══════════════════════════════════════════════════════════════
# A. 逐项比对（原版 vs 迁移版）
# ══════════════════════════════════════════════════════════════

def check_planner():
    print("\n=== A1. planner.build_plan 逐项比对 ===")
    cases = [
        {"intent": "query_price", "tickers": ["AAPL"], "timeframe": None},
        {"intent": "query_history", "tickers": ["AAPL"], "timeframe": None},
        {"intent": "financials", "tickers": ["600519.SS"]},
        {"intent": "analysis", "tickers": ["600519.SS"], "analysis_type": "valuation"},
        {"intent": "compare", "tickers": ["AAPL", "MSFT"]},
        {"intent": "analysis", "tickers": []},
    ]
    n = 0
    for c in cases:
        a = planner.build_plan(dict(c))
        b = orig_planner.build_plan(dict(c))
        same = a == b
        record(f"planner {c.get('intent')} {c.get('tickers')}",
               same, f"steps={len(b.get('steps', []))}")
        n += 1 if same else 0
    return n


def check_scorer():
    print("\n=== A2. VioletzScorer 逐项比对 ===")
    n = 0
    # 单个指标评分函数遍历
    fns = [
        ("_score_pe", [None, -5, 5, 12, 18, 30, 45, 70, 100]),
        ("_score_pb", [None, -1, 0.5, 1.2, 2.5, 4, 8, 15]),
        ("_score_peg", [None, -0.5, 0.3, 0.8, 1.2, 2, 3]),
        ("_score_revenue_growth", [None, 0.6, 0.35, 0.2, 0.1, 0.0, -0.05, -0.2]),
        ("_score_roe", [None, 0.4, 0.3, 0.2, 0.1, 0.05, 0.01, -0.1]),
        ("_score_profit_margin", [None, 0.4, 0.25, 0.15, 0.08, 0.02, -0.05, -0.2]),
        ("_score_debt_to_equity", [None, -1, 0.2, 0.5, 0.8, 1.5, 2.5, 4]),
        ("_score_free_cash_flow", [None, 60e9, 20e9, 5e9, 5e8, -2e9, -10e9]),
        ("_score_eps", [None, 25, 15, 7, 4, 2, 0.5, -1]),
        ("_score_news_sentiment", [None, -1, -0.5, 0, 0.5, 1]),
        ("_score_dividend_yield", [None, 0, 0.07, 0.05, 0.03, 0.015, 0.005]),
    ]
    for fname, args_list in fns:
        for args in args_list:
            a = getattr(scorer, fname)(args)
            b = getattr(orig_scorer, fname)(args)
            same = eq(a, b)
            record(f"scorer {fname}({args})", same, f"{a}")
            n += 1 if same else 0

    # _score_52w_position 特殊处理（三个位置参数）
    for args in (None, (100, 200, 150), (100, 150, 200), (100, 110, 90), (100, 100, 100), (120, 150, 150)):
        if args is None:
            a, b = scorer._score_52w_position(None, None, None), orig_scorer._score_52w_position(None, None, None)
            disp = None
        else:
            a = scorer._score_52w_position(*args)
            b = orig_scorer._score_52w_position(*args)
            disp = args
        same = eq(a, b)
        record(f"scorer _score_52w_position({disp})", same, f"{a}")
        n += 1 if same else 0

    # compute 全流程
    fin = {"pe_ratio": 19.8, "price_to_book": 6.65, "peg_ratio": 2.1,
           "revenue_growth": 0.15, "return_on_equity": 0.3365, "profit_margin": 0.505,
           "debt_to_equity": 0.196, "free_cash_flow": 5.2e10, "eps": 68.0}
    quote = {"current_price": 1348.86, "52_week_high": 1800, "52_week_low": 1300,
             "dividend_yield": 0.02, "pe_ratio": 19.8}
    sent = {"score": 0.35}
    a = scorer.VioletzScorer().compute(fin, quote, sent)
    b = orig_scorer.VioletzScorer().compute(fin, quote, sent)
    same = (a.total_score == b.total_score and a.rating == b.rating
            and [round(d.score, 4) for d in a.breakdown.values()]
            == [round(d.score, 4) for d in b.breakdown.values()])
    record("VioletzScorer.compute 全流程", same,
           f"总分 {a.total_score} {a.rating} | 维度 {[d.score for d in a.breakdown.values()]}")
    n += 1 if same else 0
    return n


def check_fund_scorer():
    print("\n=== A3. FundScorer 逐项比对 ===")
    nav = {"基金代码": "005827", "日增长率": 0.5}
    history = {"summary": {"start_nav": 1.0, "end_nav": 1.2, "days": 60,
                           "max_daily_increase": 2.5, "max_daily_decrease": -2.0,
                           "avg_daily_change": 0.1}}
    holdings = [{"占净值比例": "5.2"}] * 10
    info = {"净资产规模": "200亿元", "基金经理人": "张三", "管理费率": "1.5%"}
    ratings = {"上海证券": "5", "晨星评级": "5"}
    a = fund_scorer.FundScorer().compute(nav, history, holdings, info, ratings)
    b = orig_fund_scorer.FundScorer().compute(nav, history, holdings, info, ratings)
    same = (a.total_score == b.total_score and a.rating == b.rating
            and [round(d.score, 4) for d in a.breakdown.values()]
            == [round(d.score, 4) for d in b.breakdown.values()])
    record("FundScorer.compute 全流程", same,
           f"总分 {a.total_score} {a.rating} | 维度 {[d.score for d in a.breakdown.values()]}")
    return 1 if same else 0


def check_ambiguity():
    print("\n=== A4. ambiguity 逐项比对 ===")
    n = 0
    for tk in ("600519.SS", "0700.HK", "AAPL", "SPY", "000001.SZ"):
        a = ambiguity.get_market_label(tk)
        b = orig_ambiguity.get_market_label(tk)
        same = eq(a, b)
        record(f"get_market_label({tk})", same, f"{a}")
        n += 1 if same else 0

    raw = "比亚迪怎么样？对比一下中国平安"
    a = ambiguity.check_ambiguity_from_input(raw)
    b = orig_ambiguity.check_ambiguity_from_input(raw)
    same = [x["name"] for x in a] == [x["name"] for x in b]
    record("check_ambiguity_from_input 比亚迪/中国平安", same,
           f"{[x['name'] for x in a]}")
    n += 1 if same else 0

    pc = ambiguity.PendingClarification(
        company_name="比亚迪", options=ambiguity.DUAL_LISTED["比亚迪"],
        original_intent={}, user_input="比亚迪怎么样")
    for reply in ("港股", "1211.HK", "A股", "随便哪个"):
        a = ambiguity.resolve_ambiguity(reply, pc)
        b = orig_ambiguity.resolve_ambiguity(reply, pc)
        same = (a or {}).get("ticker") == (b or {}).get("ticker")
        record(f"resolve_ambiguity({reply})", same, f"{a}")
        n += 1 if same else 0

    qa = ambiguity.build_question([{"name": "比亚迪", "options": ambiguity.DUAL_LISTED["比亚迪"]}])
    qb = orig_ambiguity.build_question([{"name": "比亚迪", "options": orig_ambiguity.DUAL_LISTED["比亚迪"]}])
    same = eq(qa, qb)
    record("build_question", same, "")
    n += 1 if same else 0
    return n


# ══════════════════════════════════════════════════════════════
# B. 意图识别
# ══════════════════════════════════════════════════════════════

def check_intent():
    print("\n=== B. 意图识别 ===")
    n = 0
    cases = [
        ("查一下苹果股价", "query_price", "AAPL"),
        ("分析一下贵州茅台", "analysis", "600519"),
        ("腾讯最近一个月走势怎么样", "query_history", "0700.HK"),
        ("对比特斯拉和蔚来", "compare", "TSLA"),
    ]
    for text, want_intent, want_ticker in cases:
        t0 = time.time()
        r = intent_mod.parse(text)
        dt = round(time.time() - t0, 1)
        ok = (r["intent"] == want_intent and r["tickers"]
              and any(want_ticker in t for t in r["tickers"]))
        record(f"parse('{text}')", ok,
               f"→ {r['intent']} {r['tickers']} ({dt}s)")
        n += 1 if ok else 0

    # 基金意图
    r = fund_intent.parse("查询 005827 的净值")
    ok = r["intent"] == "fund_nav" and "005827" in (r.get("fund_codes") or [])
    record("fund parse('查询 005827 的净值')", ok, f"→ {r['intent']} {r.get('fund_codes')}")
    n += 1 if ok else 0

    # 歧义检测
    amb = ambiguity.check_ambiguity_from_input("比亚迪怎么样")
    ok = len(amb) > 0 and amb[0]["name"] == "比亚迪"
    record("歧义检测 比亚迪", ok, f"{[x['name'] for x in amb]}")
    n += 1 if ok else 0
    return n


# ══════════════════════════════════════════════════════════════
# C. 端到端三类对话
# ══════════════════════════════════════════════════════════════

def e2e_query_price():
    print("\n=== C1. 端到端 · 查行情 AAPL ===")
    n = 0
    t0 = time.time()
    intent = intent_mod.parse("查一下苹果股价")
    plan = planner.build_plan(intent)
    exec_r = runner.execute_plan(plan)
    summary = exec_r["summary"]
    report = expressor.generate_report(exec_r, intent)
    dt = round(time.time() - t0, 1)

    record("query_price 执行成功率", summary["success"] == summary["total"],
           f"{summary['success']}/{summary['total']} ({dt}s)")
    n += 1 if summary["success"] == summary["total"] else 0

    quote = expressor._find_result(exec_r["results"], "get_quote") or {}
    price = quote.get("current_price")
    ok_price = price is not None and price > 0
    record("AAPL 真实价格", ok_price, f"current_price={price} {quote.get('currency')}")
    n += 1 if ok_price else 0

    ok_report = "实时报价" in report and ("免责声明" in report or "投资有风险" in report)
    record("query_price 报告结构", ok_report, f"len={len(report)}")
    n += 1 if ok_report else 0

    print(report[:600])
    return n


def e2e_analysis():
    print("\n=== C2. 端到端 · 深度分析 600519 ===")
    n = 0
    t0 = time.time()
    intent = intent_mod.parse("分析一下贵州茅台")
    plan = planner.build_plan(intent)
    exec_r = runner.execute_plan(plan)
    summary = exec_r["summary"]
    dt_exec = round(time.time() - t0, 1)

    record("analysis 执行成功率", summary["success"] >= summary["total"] - 1,
           f"{summary['success']}/{summary['total']} ({dt_exec}s)")
    n += 1 if summary["success"] >= summary["total"] - 1 else 0

    # 数据完整性：quote / financials 应成功且含真实值
    quote = expressor._find_result(exec_r["results"], "get_quote") or {}
    fin = expressor._find_result(exec_r["results"], "get_financials") or {}
    ok_data = (quote.get("current_price") is not None
               and quote.get("current_price") > 0
               and fin.get("return_on_equity") is not None)
    record("600519 行情/财务真实数据", ok_data,
           f"price={quote.get('current_price')} ROE={fin.get('return_on_equity')}")
    n += 1 if ok_data else 0

    # Violetz 推荐评分（模拟 SSE 流式路径）
    vz = scorer.VioletzScorer().compute(
        financials=fin, quote=quote,
        sentiment=expressor._find_result(exec_r["results"], "get_news_sentiment"))
    vz_text = expressor._format_violetz_score(vz)
    ok_vz = "[__VZ_SCORE__]" in vz_text and vz.total_score > 0
    record("Violetz 推荐评分（A股真实数据）", ok_vz,
           f"总分 {vz.total_score} {vz.rating} | 可用指标 {vz.available_metrics}/12")
    n += 1 if ok_vz else 0

    # 生成完整报告（含 LLM 分析）
    t1 = time.time()
    report = expressor.generate_report(exec_r, intent)
    dt_report = round(time.time() - t1, 1)
    ok_report = ("实时报价" in report and "投资有风险" in report
                 and len(report) > 500)
    record("analysis 完整报告（含LLM分析）", ok_report, f"len={len(report)} (LLM {dt_report}s)")
    n += 1 if ok_report else 0

    print("\n" + report[:800])
    return n


def e2e_fund():
    print("\n=== C3. 端到端 · 基金净值 005827 ===")
    n = 0
    t0 = time.time()
    intent = fund_intent.parse("查询 005827 的净值")
    plan = fund_planner.build_fund_plan(intent)
    exec_r = fund_runner.execute_fund_plan(plan)
    summary = exec_r["summary"]
    dt = round(time.time() - t0, 1)

    record("fund_nav 执行成功率", summary["success"] == summary["total"],
           f"{summary['success']}/{summary['total']} ({dt}s)")
    n += 1 if summary["success"] == summary["total"] else 0

    nav = fund_expressor._find_result(exec_r["results"], "get_fund_nav") or {}
    unit = nav.get("单位净值")
    ok_nav = unit is not None and unit > 0
    record("005827 真实净值", ok_nav,
           f"单位净值={unit} 累计净值={nav.get('累计净值')} 日期={nav.get('净值日期')}")
    n += 1 if ok_nav else 0

    report = fund_expressor.generate_fund_report(exec_r, intent)
    ok_report = "005827" in report and "单位净值" in report
    record("fund_nav 报告结构", ok_report, f"len={len(report)}")
    n += 1 if ok_report else 0

    print(report[:500])
    return n


# ══════════════════════════════════════════════════════════════
# D. SSE 流式生成器
# ══════════════════════════════════════════════════════════════

def check_streaming():
    print("\n=== D. SSE 流式生成器 ===")
    n = 0
    # 基金净值流式（无 LLM）
    intent = fund_intent.parse("查询 005827 的净值")
    plan = fund_planner.build_fund_plan(intent)
    exec_r = fund_runner.execute_fund_plan(plan)
    tokens = list(fund_expressor.stream_fund_report_tokens(exec_r, intent))
    joined = "".join(tokens)
    ok = len(tokens) > 0 and "单位净值" in joined
    record("stream_fund_report_tokens(fund_nav)", ok,
           f"{len(tokens)} tokens, len={len(joined)}")
    n += 1 if ok else 0

    # 分析流式（LLM，验证逐 token 产出）
    intent = intent_mod.parse("分析一下贵州茅台")
    plan = planner.build_plan(intent)
    exec_r = runner.execute_plan(plan)
    t0 = time.time()
    got = 0
    for tok in expressor.stream_analysis_report(exec_r["results"], intent.get("intent")):
        got += len(tok)
        if got > 80:
            break
    ok = got > 0
    record("stream_analysis_report(analysis) 逐token产出", ok,
           f"已产出 {got} 字符 ({round(time.time()-t0,1)}s)")
    n += 1 if ok else 0
    return n


def main():
    print("=" * 70)
    print("Phase 4 验证: 对话 Agent 层")
    print("=" * 70)

    n_orig = 0
    n_orig += check_planner()
    n_orig += check_scorer()
    n_orig += check_fund_scorer()
    n_orig += check_ambiguity()
    print(f"\n原版逐项比对通过: {n_orig} 项")

    n_intent = check_intent()

    n_e2e = 0
    n_e2e += e2e_query_price()
    n_e2e += e2e_analysis()
    n_e2e += e2e_fund()

    n_stream = check_streaming()

    passed = sum(1 for r in _results if r["ok"])
    print("\n" + "=" * 70)
    print(f"汇总: 通过 {passed} / {len(_results)}（原版比对 {n_orig} + 意图 {n_intent} + E2E {n_e2e} + 流式 {n_stream}）")
    for r in _results:
        print(f"  [{'PASS' if r['ok'] else '--'}] {r['name']}")
    print("=" * 70)

    if passed != len(_results):
        sys.exit(1)
    print("\nPhase 4 核心验证通过")


if __name__ == "__main__":
    main()
