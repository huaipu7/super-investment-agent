"""自主深度调查验证 —— 预算硬约束 / 确定性信号 / 取证强制 / 诚实降级。

Part A —— 预算：三重上限逐条独立触发 + 墙钟 + 无进展 + 振荡 + 哨兵优先级。
Part B —— 信号：合成真实科目代码的三表 → material 判定符合阈值表。
Part E1 —— 诚实降级：空三表 → 全部信号 value=None 且零 material（不编造）。
（Part C/D/F/G/H 随后续 Step 落地后补齐，见计划书 §七。）

全部离线，不调真 LLM、不联网。
运行: D:\\python\\python.exe scripts\\verify_investigation.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.investigation.budget import (  # noqa: E402
    Budget, Depth, Spend, StopReason, check_stop,
)
from app.investigation.signals import (  # noqa: E402
    extract_signals, has_any_statement_data, lookup_series, material_signals,
)

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    """断言助手。`bool(ok)` 是刻意的：断言表达式常以字符串/列表结尾
    （如 `... and d['reason']`），那是真值但不是 bool，直接累加会 TypeError。"""
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _budget(**kw) -> Budget:
    """建一个只改指定旋钮的 Budget，其余放到不干扰的默认值。"""
    base = dict(
        max_rounds=99, max_tool_calls=99, max_llm_calls=99, deadline_seconds=100000,
        max_hypotheses=4, max_evidence_per_hypothesis=4, max_peers=3,
        no_progress_window=3, min_round_seconds=60,
    )
    base.update(kw)
    return Budget(**base)


def _drive(budget: Budget, *, tools_per_round=1, tool_llm_cost=0, clock=None,
           exhausted_at=None, max_steps=200):
    """驱动一条与 loop.py 等价的消耗序列，返回 (spend, stop_reason)。

    顺序与 loop 一致：开轮前 check(llm_cost=1) → 消耗 1 轮 1 LLM →
    轮内每个工具前 check(llm_cost=tool_llm_cost) → 消耗工具。
    """
    spend = Spend(started_at=0.0)
    t = 0.0
    reason = None
    for step in range(max_steps):
        exh = exhausted_at is not None and step >= exhausted_at
        reason = check_stop(budget, spend, t, llm_cost=1, llm_exhausted=exh)
        if reason:
            break
        spend.rounds += 1
        spend.llm_calls += 1
        stopped = False
        for _ in range(tools_per_round):
            r2 = check_stop(budget, spend, t, llm_cost=tool_llm_cost,
                            check_rounds=False)
            if r2:
                reason = r2
                stopped = True
                break
            spend.tool_calls += 1
            spend.llm_calls += tool_llm_cost
        if stopped:
            break
        if clock:
            t = clock(t)
    return spend, reason


# ── Part A：预算硬约束 ───────────────────────────────────────

def check_budget():
    print("\n=== Part A: 预算硬约束 ===")

    # A1 轮次上限：恰好 2 轮
    s, r = _drive(_budget(max_rounds=2), tools_per_round=0)
    check("A1 轮次上限恰好跑满 2 轮", s.rounds == 2 and r is StopReason.MAX_ROUNDS,
          f"rounds={s.rounds} reason={r}")

    # A2 工具调用上限：恰好 3 次，且此时尚未撞轮次上限
    b = _budget(max_rounds=10, max_tool_calls=3)
    s, r = _drive(b, tools_per_round=1)
    check("A2 工具调用达上限即停", s.tool_calls == 3 and r is StopReason.MAX_TOOL_CALLS,
          f"tool_calls={s.tool_calls} reason={r}")
    check("A2 未撞轮次上限（证明是工具数触发的）", s.rounds < b.max_rounds,
          f"rounds={s.rounds}")

    # A3 LLM 调用上限：在工具上限之前触发
    b = _budget(max_rounds=10, max_tool_calls=99, max_llm_calls=2)
    s, r = _drive(b, tools_per_round=1)
    check("A3 LLM 调用上限先于工具上限触发",
          r is StopReason.MAX_LLM_CALLS and s.tool_calls < b.max_tool_calls,
          f"llm_calls={s.llm_calls} tool_calls={s.tool_calls} reason={r}")

    # A3b 工具自带 LLM 成本也要计入（compare_peers 内部烧 1 次）
    b = _budget(max_rounds=10, max_tool_calls=99, max_llm_calls=3)
    s, r = _drive(b, tools_per_round=1, tool_llm_cost=1)
    check("A3b 工具自带 LLM 成本计入总额",
          r is StopReason.MAX_LLM_CALLS and s.llm_calls <= b.max_llm_calls + 1,
          f"llm_calls={s.llm_calls} reason={r}")

    # A4 墙钟 deadline（假时钟每轮 +100s）
    b = _budget(max_rounds=99, deadline_seconds=250, min_round_seconds=60)
    s, r = _drive(b, tools_per_round=0, clock=lambda t: t + 100)
    check("A4 墙钟到点即停", r is StopReason.DEADLINE, f"reason={r}")
    check("A4 剩余不足 min_round_seconds 时不再开新轮",
          s.remaining(b, 200.0) < b.min_round_seconds and s.rounds == 2,
          f"remaining={s.remaining(b, 200.0)} rounds={s.rounds}")

    # A5 min_round_seconds 边界：一开始就来不及
    b = _budget(deadline_seconds=100, min_round_seconds=60)
    s = Spend(started_at=0.0)
    check("A5 一开始就来不及 → 直接不启动",
          check_stop(b, s, 50.0, llm_cost=1) is StopReason.DEADLINE)

    # A6 超时哨兵优先级最高
    b = _budget(deadline_seconds=1, min_round_seconds=60, max_rounds=0)
    s = Spend(started_at=0.0)
    check("A6 哨兵优先于墙钟/轮次",
          check_stop(b, s, 999.0, llm_exhausted=True) is StopReason.LLM_EXHAUSTED)

    # A7 连续无进展
    b = _budget(no_progress_window=3)
    s = Spend(started_at=0.0, consecutive_no_progress=3)
    check("A7 连续无进展达窗口即停",
          check_stop(b, s, 0.0, llm_cost=1) is StopReason.NO_PROGRESS)
    s2 = Spend(started_at=0.0, consecutive_no_progress=2)
    check("A7b 未达窗口不误停", check_stop(b, s2, 0.0, llm_cost=1) is None)

    # A8 振荡
    b = _budget()
    s = Spend(started_at=0.0, oscillation=True)
    check("A8 振荡即停", check_stop(b, s, 0.0, llm_cost=1) is StopReason.OSCILLATION)

    # A9 未触任何上限 → 允许继续
    check("A9 预算充足时不误停",
          check_stop(_budget(), Spend(started_at=0.0), 0.0, llm_cost=1) is None)

    # A9b 轮内工具调用不被轮次上限误杀（最后一轮不能变成死轮）
    b = _budget(max_rounds=1, max_tool_calls=99)
    s = Spend(started_at=0.0, rounds=1, llm_calls=1)
    check("A9b 轮内不再按轮次判定（check_rounds=False）",
          check_stop(b, s, 0.0, llm_cost=0, check_rounds=False) is None
          and check_stop(b, s, 0.0, llm_cost=0) is StopReason.MAX_ROUNDS)

    # A10 档位映射：深度档三项上限严格大于标准档，且 deadline < SSE 兜底 900
    bs = Budget.from_settings(Depth.STANDARD)
    bd = Budget.from_settings(Depth.DEEP)
    check("A10 深度档上限更大",
          bd.max_rounds > bs.max_rounds and bd.max_tool_calls > bs.max_tool_calls
          and bd.max_llm_calls > bs.max_llm_calls and bd.deadline_seconds > bs.deadline_seconds,
          f"std={bs.max_rounds}/{bs.max_tool_calls}/{bs.max_llm_calls}/{bs.deadline_seconds} "
          f"deep={bd.max_rounds}/{bd.max_tool_calls}/{bd.max_llm_calls}/{bd.deadline_seconds}")
    check("A10b 深度档 deadline < 900（SSE 兜底）", bd.deadline_seconds < 900,
          str(bd.deadline_seconds))
    check("A10c 档位归一化", Depth.coerce("DEEP") is Depth.DEEP
          and Depth.coerce("nonsense") is Depth.STANDARD
          and Depth.coerce(None) is Depth.STANDARD)


# ── Part B：确定性信号 ──────────────────────────────────────

def _annual_frames() -> dict:
    """合成三表（两期，便于算同比）。沿用真实方向：index=报告期, columns=科目代码。

    注意：这是测试夹具，不是行情数据。断言只针对管线行为，不针对数值真伪。
    设计意图：让 9 条信号 material，3 条不 material，以覆盖两侧分支。
    """
    cols = ["2022-12-31", "2023-12-31"]
    return {
        "income": pd.DataFrame(
            {
                "REVENUE": [1000.0, 1100.0],
                "COST_OF_REVENUE": [700.0, 800.0],
                "NET_INCOME": [100.0, 90.0],
                "OPERATING_INCOME": [150.0, 130.0],
                "RESEARCH_DEV": [50.0, 70.0],
                "INTEREST_EXPENSE": [10.0, 14.0],
            },
            index=cols, dtype=float),
        "balance_sheet": pd.DataFrame(
            {
                "INVENTORY": [200.0, 250.0],
                "ACCOUNTS_RECEIVABLE": [150.0, 160.0],
                "TOTAL_LIABILITIES": [400.0, 460.0],
                "TOTAL_EQUITY": [600.0, 640.0],
            },
            index=cols, dtype=float),
        "cash_flow": pd.DataFrame(
            {
                "OPERATING_CASH_FLOW": [120.0, 60.0],
                "CAPEX": [60.0, 105.0],
            },
            index=cols, dtype=float),
    }


def _empty_frames() -> dict:
    return {"income": pd.DataFrame(), "balance_sheet": pd.DataFrame(),
            "cash_flow": pd.DataFrame()}


def check_signals():
    print("\n=== Part B: 确定性信号 ===")
    fr = _annual_frames()

    # 形状约定：index=科目代码、columns=报告期
    s_rev = lookup_series(fr, "REVENUE")
    check("B0 取数：index=科目、columns=报告期，时间升序",
          s_rev is not None and list(s_rev.index) == ["2022-12-31", "2023-12-31"],
          str(list(s_rev.index) if s_rev is not None else None))

    sigs = extract_signals(fr, period="annual")
    by = {s.code: s for s in sigs}
    check("B1 信号条数固定", len(sigs) == 12, str(len(sigs)))
    check("B1b 未知科目不编造（不存在的科目取到 None）",
          lookup_series(fr, "TOTALLY_MADE_UP") is None)

    # 毛利率 30.00% → 27.27%，同比 -2.73pp → material
    gm = by["gross_margin_delta"]
    check("B2 毛利率同比降 2.73pp 判为 material",
          gm.material and gm.direction == "down" and abs(gm.value - (-2.7273)) < 0.01,
          f"value={gm.value} material={gm.material}")

    # 营收同比 +10% → material
    rg = by["revenue_growth"]
    check("B3 营收同比 +10% 判为 material", rg.material and abs(rg.value - 10.0) < 0.01,
          f"value={rg.value}")

    # 存货增速 25% - 营收增速 10% = 15pp → material
    inv = by["inventory_growth_minus_revenue"]
    check("B4 存货增速超营收 15pp 判为 material", inv.material and abs(inv.value - 15.0) < 0.01,
          f"value={inv.value}")

    # 经营现金流/净利润 60/90 = 0.667 < 0.7 → material（盈利质量）
    cc = by["cash_conversion"]
    check("B5 现金流/净利 0.67 低于 0.7 判为 material",
          cc.material and abs(cc.value - 0.6667) < 0.01, f"value={cc.value}")

    # 研发占比 +1.36pp → material（阈值 1.0）
    rd = by["rd_to_revenue_delta"]
    check("B6 研发占比 +1.36pp 判为 material", rd.material and abs(rd.value - 1.3636) < 0.01,
          f"value={rd.value}")

    # 成本增速 14.29% - 营收增速 10% = 4.29pp → 不 material（阈值 8）
    cg = by["cogs_growth_minus_revenue"]
    check("B7 成本增速超营收 4.29pp 未达阈值 8 → 不 material",
          not cg.material and abs(cg.value - 4.2857) < 0.01, f"value={cg.value}")

    # 应收增速 6.67% - 10% = -3.33pp → 不 material
    check("B8 应收增速低于营收 → 不 material",
          not by["ar_growth_minus_revenue"].material)

    # 负债权益比 0.667→0.719 = +0.052 → 不 material（阈值 0.2）
    check("B9 负债权益比 +0.05 未达阈值 0.2 → 不 material",
          not by["debt_to_equity_delta"].material)

    mats = material_signals(sigs)
    codes = {s.code for s in mats}
    # 营业利润率 15.00% → 11.82%，同比 -3.18pp，同样过线
    check("B10 material 集合正确",
          codes == {"gross_margin_delta", "net_margin_delta", "operating_margin_delta",
                    "revenue_growth", "rd_to_revenue_delta", "interest_expense_growth",
                    "inventory_growth_minus_revenue", "cash_conversion",
                    "capex_to_revenue_delta"},
          str(sorted(codes)))
    check("B11 material 信号必带阈值说明",
          all(s.threshold for s in mats), str([s.code for s in mats if not s.threshold]))

    # 季度口径：不产出任何增量信号
    q = extract_signals(fr, period="quarterly")
    qby = {s.code: s for s in q}
    check("B12 季度口径：增量信号一律不产出且标注原因",
          not qby["gross_margin_delta"].material and qby["revenue_growth"].value is None
          and "季度累计" in qby["revenue_growth"].note,
          qby["revenue_growth"].note)

    # ── 利润率的 material 用相对变化，不用绝对 pp ──
    def _gm_signal(cogs_prev: float, cogs_cur: float):
        """营收恒为 1000 的两期三表 → 毛利率信号。"""
        fr_x = {"income": pd.DataFrame(
            {"REVENUE": [1000.0, 1000.0], "COST_OF_REVENUE": [cogs_prev, cogs_cur]},
            index=["2022-12-31", "2023-12-31"], dtype=float)}
        return {s.code: s for s in extract_signals(fr_x, period="annual")}["gross_margin_delta"]

    # 高毛利：毛利率 91% → 90%，Δ=-1.00pp（旧绝对阈值 1.5 判不出），相对 -1.10% → material
    hi = _gm_signal(90.0, 100.0)
    check("B13 高毛利：Δ=-1.00pp 未达旧绝对阈值，但相对 -1.10% 判为 material",
          hi.material and abs(hi.value - (-1.0)) < 1e-6
          and abs(hi.rel_value - (-1.0989)) < 0.01,
          f"value={hi.value} rel={hi.rel_value} material={hi.material}")

    # 低毛利：毛利率 5% → 4.9%，相对 -2.00%（过相对线）但 |Δ|=0.1pp 低于噪声地板 0.5pp → 不 material
    lo = _gm_signal(950.0, 951.0)
    check("B14 低毛利：相对 -2.00% 过线但 |Δ|=0.10pp 低于噪声地板 → 不 material",
          (not lo.material) and abs(lo.value - (-0.1)) < 1e-6
          and abs(lo.rel_value - (-2.0)) < 0.01,
          f"value={lo.value} rel={lo.rel_value} material={lo.material}")

    # 中毛利（30%）上，地板与相对线各管一头：Δ=-0.40pp 卡在地板下、Δ=-0.60pp 过线
    mid_low = _gm_signal(700.0, 704.0)
    mid_ok = _gm_signal(700.0, 706.0)
    check("B14b 中毛利：Δ=-0.40pp 卡在 0.5pp 地板下 → 不 material；Δ=-0.60pp → material",
          (not mid_low.material) and mid_ok.material,
          f"low={mid_low.value}/{mid_low.rel_value}/{mid_low.material} "
          f"ok={mid_ok.value}/{mid_ok.rel_value}/{mid_ok.material}")

    check("B15 相对变化写进阈值说明（可核对，不留暗箱）",
          "相对" in hi.threshold or "Δ/上期" in hi.threshold, hi.threshold)

    d_hi = hi.to_dict()
    check("B16 信号字典带 rel_value（报告/接口可见）",
          d_hi.get("rel_value") is not None and abs(d_hi["rel_value"] - (-1.0989)) < 0.01,
          str(d_hi.get("rel_value")))


# ── Part E1：诚实降级（无数据 → 零材料信号）─────────────────

def check_degradation():
    print("\n=== Part E1: 诚实降级 ===")
    fr = _empty_frames()
    check("E1 空三表：has_any_statement_data 为假", has_any_statement_data(fr) is False)
    sigs = extract_signals(fr, period="annual")
    check("E2 空三表：信号全为 None（不编造数值）",
          len(sigs) == 12 and all(s.value is None for s in sigs),
          str([(s.code, s.value) for s in sigs if s.value is not None]))
    check("E3 空三表：零 material → 下游将播种零假设",
          material_signals(sigs) == [])

    # 缺单个科目：只有该族信号为 None，其余照常
    fr2 = _annual_frames()
    fr2["cash_flow"] = pd.DataFrame()      # 抽掉现金流表
    sigs2 = extract_signals(fr2, period="annual")
    by2 = {s.code: s for s in sigs2}
    check("E4 抽掉现金流表：现金流信号为 None，其余仍可算",
          by2["cash_conversion"].value is None and by2["revenue_growth"].value is not None,
          f"cash_conversion={by2['cash_conversion'].value}")

    # 只有一期：同比类信号为 None（不足两期不硬造）
    fr3 = {"income": pd.DataFrame({"REVENUE": [1000.0]}, index=["2023-12-31"]),
           "balance_sheet": pd.DataFrame(), "cash_flow": pd.DataFrame()}
    sigs3 = {s.code: s for s in extract_signals(fr3, period="annual")}
    check("E5 仅一期：同比信号为 None", sigs3["revenue_growth"].value is None,
          str(sigs3["revenue_growth"].value))


# ── Part I：单轮工具调用引擎 ────────────────────────────────

def check_engine():
    """chat_once_with_tools 必须只发一次、不执行工具、不返回 JSON 字符串参数。

    全程不打网络：直接换掉 _retry_call（绕过 __init__ 与 httpx）。
    """
    print("\n=== Part I: 单轮工具调用引擎 ===")
    from app.llm.client import LLMClient, ChatResult

    canned = {
        "content": [
            {"type": "text", "text": "我先取新闻。"},
            {"type": "tool_use", "id": "tu_1", "name": "get_news",
             "input": {"ticker": "AAPL", "limit": 20}},
            {"type": "tool_use", "id": "tu_2", "name": "search_knowledge",
             "input": {"query": "去库存"}},
        ],
        "stop_reason": "tool_use",
    }

    c = object.__new__(LLMClient)
    calls = {"n": 0}

    def fake_retry(fn, task=None):
        calls["n"] += 1
        return canned

    c._retry_call = fake_retry
    msgs = [{"role": "user", "content": "为什么毛利率降了"}]
    res = c.chat_once_with_tools(messages=msgs, system="s", tools=[{"name": "x"}],
                                 task="investigation")

    check("I1 只发一次请求（无内部轮次）", calls["n"] == 1, f"calls={calls['n']}")
    check("I2 返回类型是 ChatResult", isinstance(res, ChatResult),
          type(res).__name__)
    check("I3 文本块被保留", res.text == "我先取新闻。", repr(res.text))
    check("I4 stop_reason 透传", res.raw.get("stop_reason") == "tool_use")
    check("I5 工具调用数与块数一致", len(res.tool_calls) == 2,
          str(len(res.tool_calls)))
    check("I6 工具参数是活 dict（不是 JSON 串，与 chat_tool_calls 不同）",
          isinstance(res.tool_calls[0]["input"], dict)
          and res.tool_calls[0]["input"]["limit"] == 20,
          repr(res.tool_calls[0]["input"]))
    check("I7 工具名与 id 保留",
          res.tool_calls[1]["name"] == "search_knowledge"
          and res.tool_calls[0]["id"] == "tu_1")
    check("I8 工具**未被执行**（本方法不接触 executor）",
          "未注册工具执行器" not in repr(res.tool_calls)
          and isinstance(res.tool_calls[0]["input"], dict))
    check("I9 传入的 messages 未被改写", len(msgs) == 1 and msgs[0]["role"] == "user",
          str(len(msgs)))

    # 直接给答案（无 tool_use）的轮次
    canned2 = {"content": [{"type": "text", "text": "我认为无需再查。"}],
               "stop_reason": "end_turn"}
    c._retry_call = lambda fn, task=None: canned2
    res2 = c.chat_once_with_tools(messages=msgs)
    check("I10 纯文本轮次：tool_calls 为空、stop_reason=end_turn",
          res2.tool_calls == [] and res2.raw.get("stop_reason") == "end_turn",
          f"tool_calls={res2.tool_calls}")

    # 既有调用点未受影响：chat_with_tools 仍然存在且签名未变
    import inspect
    sig = inspect.signature(LLMClient.chat_with_tools)
    check("I11 chat_with_tools 签名未变（既有调用点零影响）",
          list(sig.parameters)[:5] == ["self", "messages", "system", "tools", "tool_executor"],
          str(list(sig.parameters)))


# ── Part J：封闭工具集 ──────────────────────────────────────

def _fixture_frames() -> dict:
    """三表夹具，沿用 router 出参的真实方向：index=报告期, columns=标准科目代码。

    科目名→代码的归一由各 source 自己完成（base.DataSource 契约里
    "index=会计期间, columns=指标代码"），所以进入调查工具时列名已是标准代码。
    这是测试夹具，不是行情数据。
    """
    cols = ["2022-12-31", "2023-12-31"]
    return {
        "income": pd.DataFrame({
            "REVENUE": [1000.0, 1100.0],
            "COST_OF_REVENUE": [700.0, 800.0],
            "NET_INCOME": [100.0, 90.0],
            "OPERATING_INCOME": [150.0, 130.0],
            "RESEARCH_DEV": [50.0, 70.0],
            "INTEREST_EXPENSE": [10.0, 14.0],
        }, index=cols, dtype=float),
        "balance_sheet": pd.DataFrame({
            "INVENTORY": [200.0, 250.0],
            "ACCOUNTS_RECEIVABLE": [150.0, 160.0],
            "TOTAL_LIABILITIES": [400.0, 460.0],
            "TOTAL_EQUITY": [600.0, 640.0],
        }, index=cols, dtype=float),
        "cash_flow": pd.DataFrame({
            "OPERATING_CASH_FLOW": [120.0, 60.0],
            "CAPEX": [60.0, 105.0],
        }, index=cols, dtype=float),
    }


class _FixtureMarket:
    """替身数据源（**测试夹具**）。raise_on 指定让哪个方法抛错。"""

    def __init__(self, *, frames=None, news=None, briefings=None, info=None,
                 history=None, quarterly=None, raise_on=()):
        self._frames = frames if frames is not None else {}
        self._news = news or []
        self._briefings = briefings or []
        self._info = info if info is not None else {
            "name": "测试公司", "industry": "电子", "sector": "科技",
            "country": "US", "market_cap": 1234.0, "currency": "USD",
            "description": "连接器制造商", "concepts": []}
        self._history = history
        self._quarterly = quarterly
        self._raise_on = set(raise_on)

    def _boom(self, name):
        if name in self._raise_on:
            raise RuntimeError(f"{name} 炸了")

    def fetch_financial_statements(self, ticker, period="annual"):
        self._boom("fetch_financial_statements")
        return self._frames

    def get_financials(self, ticker, as_of=None):
        self._boom("get_financials")
        return {"vintage": {"source": "fixture", "is_point_in_time": False,
                            "note": "夹具"}}

    def get_news(self, ticker, limit=20):
        self._boom("get_news")
        return {"articles": self._news, "briefings": self._briefings}

    def fetch_company_info(self, ticker):
        self._boom("fetch_company_info")
        return dict(self._info)

    def get_history(self, ticker, period="1mo", as_of=None):
        self._boom("get_history")
        return self._history

    def get_benchmark_history(self, ticker, period="5y"):
        return None

    def fetch_quarterly_income(self, ticker):
        self._boom("fetch_quarterly_income")
        return self._quarterly


def _hist_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": [10.0, 11.0, 12.0], "High": [11.0, 12.0, 13.0],
         "Low": [9.0, 10.0, 11.0], "Close": [10.0, 11.0, 15.0],
         "Volume": [100.0, 200.0, 300.0]},
        index=["2023-01-01", "2023-02-01", "2023-03-01"])


def check_tools():
    print("\n=== Part J: 封闭工具集 ===")
    from app.investigation.tools import (
        TOOL_META, TOOL_NAMES, Toolbox, _same_period_yoy,
    )

    news = [{"title": "公司下调全年指引", "summary": "需求走弱，渠道去库存",
             "published": "2023-11-01", "source": "东财", "url": "https://x/1"},
            {"title": "第二条", "summary": "s2", "published": "2023-10-01",
             "source": "东财", "url": "https://x/2"}]
    briefings = [{"title": "2023年第三季度业绩说明会", "type": "业绩说明会",
                  "published": "2023-10-30", "source": "巨潮资讯",
                  "url": "https://y/1"}]

    mk = _FixtureMarket(frames=_fixture_frames(), news=news, briefings=briefings,
                        history=_hist_frame())
    tb = Toolbox(market=mk, max_peers=3)

    # J1 三表摘要：真实清洗后科目代码正确、vintage 透传
    r = tb.execute("get_statement_summary", {"ticker": "AAPL"})
    check("J1 三表摘要：清洗后取到标准科目",
          r["ok"] and "REVENUE" in r["lines"] and "INVENTORY" in r["lines"],
          str(sorted(r.get("lines", {}))[:4]))
    check("J1b 报告期顺序正确",
          r["periods"] == ["2022-12-31", "2023-12-31"], str(r["periods"]))
    check("J1c vintage 原样透传（不谎称 point-in-time）",
          isinstance(r["vintage"], dict) and r["vintage"]["is_point_in_time"] is False,
          str(r["vintage"]))
    check("J1d 科目带中文标签",
          r["line_labels"]["REVENUE"] == "营业收入", r["line_labels"]["REVENUE"])

    # J2 单科目 + 同比
    r = tb.execute("get_statement_line",
                   {"ticker": "AAPL", "line_code": "revenue", "n_periods": 6})
    check("J2 单科目：最新值与同比正确",
          r["ok"] and r["latest"] == 1100.0 and abs(r["yoy_percent"] - 10.0) < 0.01
          and r["yoy_period"] == "2022-12-31",
          f"latest={r['latest']} yoy={r['yoy_percent']} base={r['yoy_period']}")
    check("J2b 科目代码大小写不敏感（归一为大写）", r["line_code"] == "REVENUE")

    # J3 未知科目必须拒（不接受模型自造科目名）
    r = tb.execute("get_statement_line",
                   {"ticker": "AAPL", "line_code": "MY_MADE_UP_LINE"})
    check("J3 未知科目被拒且返回合法清单",
          r["ok"] is False and len(r.get("allowed", [])) > 10 and "自造" in r["reason"],
          r["reason"])

    # J4 新闻：URL 原样保留（这是引用的载体）
    r = tb.execute("get_news", {"ticker": "AAPL", "limit": 5})
    check("J4 新闻取回且 URL 保留",
          r["ok"] and r["article_count"] == 2
          and r["articles"][0]["url"] == "https://x/1", str(r.get("article_count")))

    # J5 空新闻 → 如实不可用（且不编造）
    tb2 = Toolbox(market=_FixtureMarket(frames=_fixture_frames()), max_peers=3)
    r = tb2.execute("get_news", {"ticker": "AAPL"})
    check("J5 无新闻 → available=False + reason",
          r["ok"] and r["available"] is False and "未获取到新闻" in r["reason"])

    # J6 公告：A股有源；美股如实说明无源
    r = tb.execute("get_announcements", {"ticker": "600519.SS"})
    check("J6 A股公告可用（巨潮）",
          r["ok"] and r["available"] and r["source"] == "巨潮资讯"
          and r["briefings"][0]["url"] == "https://y/1")
    r = tb2.execute("get_announcements", {"ticker": "AAPL"})
    check("J6b 美股公告如实不可用（不编造）",
          r["ok"] and r["available"] is False and "巨潮" in r["reason"], r["reason"])

    # J7 公司画像：概念板块透传
    r = tb.execute("get_company_profile", {"ticker": "AAPL"})
    check("J7 画像：行业与业务描述取到",
          r["ok"] and r["industry"] == "电子" and r["summary"] == "连接器制造商")
    mk_ashare = _FixtureMarket(frames=_fixture_frames(), info={
        "name": "某公司", "industry": "电子", "country": "CN",
        "concepts": ["消费电子", "汽车电子"]})
    r = Toolbox(market=mk_ashare).execute("get_company_profile", {"ticker": "600519.SS"})
    check("J7b A股概念板块透传",
          r["concepts"] == ["消费电子", "汽车电子"], str(r["concepts"]))

    # J8 历史行情：区间涨跌 + 基准缺失如实为 None
    r = tb.execute("get_price_history", {"ticker": "AAPL", "period": "3mo"})
    check("J8 区间涨跌正确（10 → 15 = +50%）",
          r["ok"] and abs(r["change_percent"] - 50.0) < 0.01
          and r["benchmark"] is None,
          f"chg={r['change_percent']} bench={r['benchmark']}")

    # J9 季度利润表：A股累计口径如实标注
    q = pd.DataFrame({"REVENUE": [100.0, 260.0, 420.0, 600.0],
                      "NET_INCOME": [10.0, 26.0, 42.0, 60.0]},
                     index=["2023-03-31", "2023-06-30", "2023-09-30",
                            "2023-12-31"], dtype=float)
    r = Toolbox(market=_FixtureMarket(quarterly=q)).execute(
        "get_quarterly_income", {"ticker": "600519.SS"})
    check("J9 季度利润表：序列取到且累计口径被标注",
          r["ok"] and r["revenue"] == [100.0, 260.0, 420.0, 600.0]
          and "累计" in r.get("note", ""), r.get("note"))
    r = Toolbox(market=_FixtureMarket(quarterly=None)).execute(
        "get_quarterly_income", {"ticker": "AAPL"})
    check("J9b 无季度数据 → 如实不可用", r["ok"] and r["available"] is False)

    # J10 未知工具 → error + 合法清单
    r = tb.execute("hack_the_planet", {})
    check("J10 未知工具被拒且给出合法清单",
          r.get("ok") is False and "未知工具" in r["error"]
          and set(r["allowed"]) == set(TOOL_NAMES), str(r.get("error")))

    # J11 工具内部抛错 → 降级不抛
    tb_boom = Toolbox(market=_FixtureMarket(frames=_fixture_frames(),
                                            raise_on=("get_news",)))
    r = tb_boom.execute("get_news", {"ticker": "AAPL"})
    check("J11 工具抛错 → ok=False + reason 带异常类型（不向上抛）",
          r["ok"] is False and "RuntimeError" in r["reason"], r["reason"])
    r = tb_boom.execute("get_statement_summary", {"ticker": "AAPL"})
    check("J11b 单工具失败不影响其他工具", r["ok"] is True)

    # J12 空三表 → 如实不可用（下游将播种零假设）
    r = Toolbox(market=_FixtureMarket(frames={})).execute(
        "get_statement_summary", {"ticker": "AAPL"})
    check("J12 空三表 → available=False + reason",
          r["ok"] is False and r["available"] is False and "三表为空" in r["reason"],
          r["reason"])

    # J13 compare_peers：requested vs resolved 分离 + 每次调查只跑一次
    from unittest import mock
    with mock.patch("app.analysis.competitive.CompetitiveAnalyzer.identify_competitors",
                    return_value=[{"name": "A", "ticker": "A1"},
                                  {"name": "B", "ticker": "B1"}]):
        with mock.patch(
                "app.analysis.pipeline.AnalysisOrchestrator._fetch_competitor_revenues",
                return_value=[{"name": "A", "ticker": "A1", "revenue": 100.0,
                               "currency_original": "USD", "pe": 20.0,
                               "market_cap": 1000.0}]):
            r1 = tb.execute("compare_peers", {"ticker": "AAPL", "max_peers": 3})
            r2 = tb.execute("compare_peers", {"ticker": "AAPL", "max_peers": 3})
    check("J13 竞品：requested(LLM 提的) 与 resolved(真实取到的) 分离",
          r1["ok"] and r1["requested"] == 2 and r1["resolved"] == 1,
          f"requested={r1.get('requested')} resolved={r1.get('resolved')}")
    check("J13b 竞品 PE 中位数来自真实取数",
          r1["peer_pe"]["value"] == 20.0 and r1["peer_pe"]["confidence"] == "low")
    check("J13c once_per_run：第二次调用复用首次结果",
          r2.get("duplicate") is True and r2["resolved"] == 1)

    # J14 budget 正确性：compare_peers 的 LLM 成本必须登记（否则预算说谎）
    check("J14 compare_peers 的 llm_cost 已登记为 1",
          TOOL_META["compare_peers"]["llm_cost"] == 1
          and TOOL_META["compare_peers"]["once_per_run"] is True)
    check("J14b 纯取数工具 llm_cost 为 0",
          all(TOOL_META[n]["llm_cost"] == 0 for n in TOOL_NAMES
              if n != "compare_peers"))

    # J15 tool_executor 返回合法 JSON 字符串（引擎契约）
    import json as _json
    s = tb.execute.__self__.tool_executor("get_company_profile", {"ticker": "AAPL"})
    check("J15 tool_executor 返回可解析的 JSON 字符串",
          isinstance(s, str) and _json.loads(s)["ok"] is True)

    # J16 同期对同期同比：季度累计缺去年同期 → 如实 None
    cum = pd.Series([100.0, 260.0, 420.0],
                    index=["2023-06-30", "2023-09-30", "2023-12-31"])
    y, base, note = _same_period_yoy(cum, "quarterly")
    check("J16 季度累计缺去年同期 → 同比为 None 且说明原因",
          y is None and "不可用相邻期做差" in note, f"yoy={y} note={note}")
    cum2 = pd.Series([80.0, 100.0, 260.0, 420.0],
                     index=["2022-12-31", "2023-06-30", "2023-09-30", "2023-12-31"])
    y, base, note = _same_period_yoy(cum2, "quarterly")
    check("J16b 有去年同期 → 正确取到并标注基期",
          abs(y - 425.0) < 0.01 and base == "2022-12-31",
          f"yoy={y} base={base}")
    ann = pd.Series([100.0, 110.0], index=["2022-12-31", "2023-12-31"])
    y, base, note = _same_period_yoy(ann, "annual")
    check("J16c 年度序列取上一期", abs(y - 10.0) < 0.01 and base == "2022-12-31")


# ── Part F：取证强制（核心不变量，纯代码无 LLM）───────────────

class _FakeLLM:
    """替身 LLM：chat() 返回预置文本。"""

    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def chat(self, messages, **kwargs) -> str:
        self.calls.append(kwargs)
        return self.response


def _ev(kind, relation, *, valid=True, ok=True, strength=None):
    from app.investigation.hypotheses import strength_for
    return {"source_kind": kind, "relation": relation, "attribution_valid": valid,
            "ok": ok, "strength": strength or strength_for(kind)}


def check_hypotheses():
    print("\n=== Part F: 取证强制（无 LLM）===")
    from app.investigation.hypotheses import (
        STATUS_INSUFFICIENT, STATUS_PARTIAL, STATUS_UNVERIFIED, STATUS_VERIFIED,
        evaluate, frame_hypotheses, normalize_relation, seed_families, strength_for,
    )

    st = lambda evs: evaluate(evs)["status"]  # noqa: E731

    check("F1 零证据 → 证据不足", st([]) == STATUS_INSUFFICIENT, st([]))
    check("F2 **单条新闻支持 → 部分验证（绝不能是已验证）**",
          st([_ev("news", "supports")]) == STATUS_PARTIAL,
          st([_ev("news", "supports")]))
    check("F3 两条支持但同一来源类型 → 仍部分验证",
          st([_ev("news", "supports"), _ev("news", "supports")]) == STATUS_PARTIAL,
          st([_ev("news", "supports"), _ev("news", "supports")]))
    check("F4 两条支持且来源类型不同 → 已验证",
          st([_ev("news", "supports"), _ev("financial_statement", "supports")])
          == STATUS_VERIFIED)
    check("F5 两条支持但存在强反驳 → 降回部分验证",
          st([_ev("news", "supports"), _ev("peer", "supports"),
              _ev("financial_statement", "contradicts")]) == STATUS_PARTIAL,
          st([_ev("news", "supports"), _ev("peer", "supports"),
              _ev("financial_statement", "contradicts")]))
    check("F5b 弱反驳不降级（medium 反驳 < strong）",
          st([_ev("news", "supports"), _ev("peer", "supports"),
              _ev("announcement", "contradicts")]) == STATUS_VERIFIED)
    check("F6 **attribution_valid=False 计零，无法凑成已验证**",
          st([_ev("news", "supports"), _ev("peer", "supports", valid=False)])
          == STATUS_PARTIAL,
          st([_ev("news", "supports"), _ev("peer", "supports", valid=False)]))
    check("F6b 全部未采信 → 证据不足",
          st([_ev("news", "supports", valid=False)]) == STATUS_INSUFFICIENT)
    check("F7 工具失败项(ok=False) → 证据不足",
          st([_ev("news", "supports", ok=False)]) == STATUS_INSUFFICIENT)
    check("F8 强度未知 → 不可用 → 证据不足",
          st([_ev("news", "supports", strength="bogus")]) == STATUS_INSUFFICIENT)
    check("F9 有证据但只反驳 → 未验证",
          st([_ev("news", "contradicts")]) == STATUS_UNVERIFIED,
          st([_ev("news", "contradicts")]))
    check("F10 支持全为模型推断 → 部分验证（永不已验证）",
          st([_ev("llm_hypothesis", "supports"), _ev("llm_hypothesis", "supports")])
          == STATUS_PARTIAL)

    # 判定结果带可解释性
    d = evaluate([_ev("news", "supports"), _ev("financial_statement", "supports")])
    check("F11 判定带计数与来源类型（可追溯）",
          bool(d["support_count"] == 2
               and d["support_kinds"] == ["financial_statement", "news"]
               and d["reason"]),
          f"{d['support_count']} {d['support_kinds']}")

    # 关系归一
    check("F12 关系归一：中文/英文/未知",
          normalize_relation("支持") == "supports"
          and normalize_relation("Contradicts") == "contradicts"
          and normalize_relation("随便写") == "neutral"
          and normalize_relation(None) == "neutral")

    # 强度由来源类型确定性赋值，不由模型决定
    check("F13 强度按来源类型确定性赋值",
          strength_for("financial_statement") == "strong"
          and strength_for("news") == "medium"
          and strength_for("llm_hypothesis") == "weak"
          and strength_for("未见过") == "weak")

    # ── 闸门 1：零 material 信号 → 零族 → 零假设 ──
    fr = _annual_frames()
    sigs = extract_signals(fr, period="annual")
    fams = seed_families(sigs)
    check("F14 有 material 信号 → 播种出成因族", len(fams) >= 3, str(len(fams)))
    ids = {f["family"] for f in fams}
    check("F14b 毛利降+存货超营收增长 → 触发「需求走弱」",
          "demand_weakness" in ids, str(sorted(ids)))
    check("F14c 每族都带触发信号（可追溯）",
          all(f["seeded_by"] for f in fams))
    check("F15 **零 material 信号 → 零族（闸门 1）**",
          seed_families(extract_signals(_empty_frames())) == [])
    check("F15b 无信号输入也不崩", seed_families([]) == [])

    # ── 闸门 2：越界族被丢弃 ──
    hyp, note = frame_hypotheses(fams, sigs, None, client=None, max_hypotheses=4)
    check("F16 无 LLM → 回退规则族且如实标注降级",
          len(hyp) > 0 and note and all(h["prior_confidence"] == "low" for h in hyp),
          note)

    good = json.dumps({"hypotheses": [
        {"id": "H1", "family": fams[0]["family"], "statement": "本公司的测试假设",
         "predicted_observables": ["观察点"], "why_it_matters": "重要",
         "prior_confidence": "high"}]}, ensure_ascii=False)
    hyp2, note2 = frame_hypotheses(fams, sigs, None,
                                   client=_FakeLLM(good), max_hypotheses=4)
    check("F17 合法族被接受，且带族标签",
          len(hyp2) == 1 and hyp2[0]["family"] == fams[0]["family"]
          and hyp2[0]["family_label"] == fams[0]["label"] and note2 == "",
          f"{len(hyp2)} note={note2!r}")

    bad = json.dumps({"hypotheses": [
        {"id": "H1", "family": "模型自创的族", "statement": "编造的成因"},
        {"id": "H2", "family": fams[0]["family"], "statement": "合法"}]},
        ensure_ascii=False)
    hyp3, note3 = frame_hypotheses(fams, sigs, None,
                                   client=_FakeLLM(bad), max_hypotheses=4)
    check("F18 **越界族被丢弃并记日志（闸门 2）**",
          len(hyp3) == 1 and hyp3[0]["family"] == fams[0]["family"]
          and "丢弃" in note3 and "模型自创的族" in note3, note3)

    # 全部越界 → 回退规则族（不静默给空）
    allbad = json.dumps({"hypotheses": [{"family": "全自创", "statement": "x"}]},
                        ensure_ascii=False)
    hyp4, note4 = frame_hypotheses(fams, sigs, None, client=_FakeLLM(allbad),
                                   max_hypotheses=4)
    check("F19 全部越界 → 回退规则族（不静默返回空）",
          len(hyp4) > 0 and "回退规则族" in note4, note4)

    # 契约不符 → 回退
    hyp5, note5 = frame_hypotheses(fams, sigs, None, client=_FakeLLM("不是 JSON"),
                                   max_hypotheses=4)
    check("F20 契约不符 → 回退规则族并标注",
          len(hyp5) > 0 and "回退规则族" in note5, note5)

    # 截断规则是代码的（max_hypotheses 生效）
    many = json.dumps({"hypotheses": [
        {"id": f"H{i}", "family": f["family"], "statement": "s",
         "prior_confidence": "low"} for i, f in enumerate(fams, 1)]},
        ensure_ascii=False)
    hyp6, _ = frame_hypotheses(fams, sigs, None, client=_FakeLLM(many),
                               max_hypotheses=2)
    check("F21 假设数被 max_hypotheses（代码层）截断", len(hyp6) == 2, str(len(hyp6)))

    # 零族 → 零假设（闸门 1 端到端）
    hyp7, note7 = frame_hypotheses([], [], None, client=_FakeLLM(good),
                                   max_hypotheses=4)
    check("F22 零族 → 零假设（闸门 1 端到端）", hyp7 == [] and note7 == "")


# ── Part C/D：循环（预算 / 去重 / 无进展 / 振荡 / 哨兵 / 降级）────

class _FakeToolbox:
    """替身工具箱：取数与画像直给，execute 返回固定 payload 并记调用。"""

    def __init__(self, *, payload=None, frames=None, profile=None, max_peers=3):
        self.max_peers = max_peers
        self.calls: list[tuple] = []
        self._payload = payload if payload is not None else {"ok": True, "note": "固定结果"}
        self._frames = _fixture_frames() if frames is None else frames
        self._profile = profile or {"name": "测试公司", "industry": "电子", "country": "US"}

    def frames(self, ticker, period="annual"):
        return self._frames

    def profile(self, ticker):
        return dict(self._profile)

    def execute(self, name, args):
        self.calls.append((name, dict(args or {})))
        return dict(self._payload)


class _ScriptedLLM:
    """按脚本逐轮返回文本+动作；raw_ok=False 模拟引擎轮数耗尽哨兵。

    实现的是调用方契约 chat_once_with_tools（单次 POST、不执行工具）。
    """

    def __init__(self, rounds, *, raw_ok=True, frame_family=None):
        self._rounds = list(rounds)
        self._i = 0
        self.raw_ok = raw_ok
        self.frame_family = frame_family
        self.last_messages: list = []

    def chat(self, messages, **kwargs) -> str:
        """假设措辞调用（frame_hypotheses 用的是 chat，不是 chat_once_with_tools）。

        默认返回合法的空假设集 → 走规则族回退（无告警、结果确定）；给
        frame_family 时回显该族，用于验证正常措辞路径。
        """
        if self.frame_family:
            return json.dumps({"hypotheses": [{
                "id": "H1", "family": self.frame_family, "statement": "测试假设陈述",
                "predicted_observables": ["观察点"], "why_it_matters": "重要",
                "prior_confidence": "high"}]}, ensure_ascii=False)
        return json.dumps({"hypotheses": []}, ensure_ascii=False)

    def chat_once_with_tools(self, messages, system=None, tools=None, task=None,
                             max_tokens=None):
        from app.llm.client import ChatResult
        spec = (self._rounds[self._i] if self._i < len(self._rounds)
                else {"text": "取证完成。", "calls": []})
        self._i += 1
        self.last_messages = list(messages)
        calls = spec.get("calls") or []
        content = [{"type": "text", "text": spec.get("text", "")}]
        for j, c in enumerate(calls):
            content.append({"type": "tool_use", "id": f"tu{self._i}_{j}",
                            "name": c["name"], "input": c.get("args") or {}})
        raw = {} if not self.raw_ok else {
            "content": content,
            "stop_reason": "tool_use" if calls else "end_turn"}
        return ChatResult(
            text=spec.get("text", ""), messages=list(messages),
            tool_calls=[{"id": b["id"], "name": b["name"], "input": b["input"]}
                        for b in content if b["type"] == "tool_use"],
            raw=raw)


def _loop_budget(**kw):
    from app.investigation.budget import Budget, Depth
    base = dict(max_rounds=6, max_tool_calls=50, max_llm_calls=50,
                deadline_seconds=100000, max_hypotheses=4,
                max_evidence_per_hypothesis=4, max_peers=3,
                no_progress_window=3, min_round_seconds=60, depth=Depth.STANDARD)
    base.update(kw)
    return Budget(**base)


def _attr(hid="H1", relation="supports", idx=0, ref=""):
    return {"hypothesis_id": hid, "relation": relation, "source_ref_index": idx,
            "source_ref": ref, "rationale": "r"}


def _kinds(res):
    return [(h["id"], h["status"]) for h in res["hypotheses"]]


def check_loop():
    print("\n=== Part C/D: 循环（预算/去重/无进展/振荡/哨兵/降级）===")
    from app.investigation.loop import InvestigationLoop, EXHAUSTED_SENTINEL
    from app.investigation.budget import StopReason

    # ── C1 无进展：每轮换**未被截断的参数**（绕过去重）但零新增证据 → 窗口内停 ──
    # 用 search_knowledge 的 query（不计入 _ARG_CAPS，不会被折叠）
    rounds = [{"text": json.dumps({"attributions": [], "note": f"第{i}轮"}),
               "calls": [{"name": "search_knowledge",
                          "args": {"query": f"第{i}个不同的查询问句"}}]}
              for i in range(6)]
    tb = _FakeToolbox()
    res = InvestigationLoop(budget=_loop_budget(), toolbox=tb,
                            llm=_ScriptedLLM(rounds), clock=lambda: 0.0).run("AAPL")
    check("C1 连续无进展 → no_progress（未耗尽轮次预算）",
          res["stop_reason"] == StopReason.NO_PROGRESS.value
          and res["spent"]["rounds"] == 3
          and res["spent"]["rounds"] < res["limits"]["max_rounds"],
          f"reason={res['stop_reason']} rounds={res['spent']['rounds']}")

    # ── B 去重：同样的查询不重复执行；参数归一化让 limit=20/25 视为同一次 ──
    rounds = [{"text": json.dumps({"attributions": [_attr()], "note": "a"}),
               "calls": [{"name": "get_news", "args": {"ticker": "aapl", "limit": 20}}]},
              {"text": json.dumps({"attributions": [_attr()], "note": "b"}),
               "calls": [{"name": "get_news", "args": {"ticker": "AAPL", "limit": 25}}]},
              {"text": json.dumps({"attributions": [], "note": "收尾"}), "calls": []}]
    tb = _FakeToolbox()
    llm = _ScriptedLLM(rounds)
    res = InvestigationLoop(budget=_loop_budget(), toolbox=tb, llm=llm,
                            clock=lambda: 0.0).run("AAPL")
    check("B1 **去重生效：只执行 1 次，命中记为 duplicate 且不计 tool_calls**",
          len(tb.calls) == 1 and res["spent"]["tool_calls"] == 1
          and res["spent"]["duplicates"] == 1,
          f"calls={len(tb.calls)} tool_calls={res['spent']['tool_calls']} "
          f"dups={res['spent']['duplicates']}")
    check("B2 参数规范化：limit=20/25 与大小写 ticker 视为同一次查询",
          len(tb.calls) == 1 and tb.calls[0][1]["ticker"] == "AAPL"
          and tb.calls[0][1]["limit"] == 20, str(tb.calls))
    # 直接看 tool_result 的原始 content —— 不要重新 json.dumps 整个消息列表，
    # 那样内层 JSON 的引号会被转义，子串搜索会假阴。
    _contents = [b.get("content", "")
                 for m in llm.last_messages if isinstance(m.get("content"), list)
                 for b in m["content"]
                 if isinstance(b, dict) and b.get("type") == "tool_result"]
    check("B3 第二轮确实拿到了 duplicate 标记（回灌给模型）",
          any('"duplicate": true' in str(c) for c in _contents),
          f"tool_results={len(_contents)}")

    # ── A6' 哨兵：轮数耗尽绝不冒充结论 ──
    tb = _FakeToolbox()
    llm = _ScriptedLLM([{"text": EXHAUSTED_SENTINEL,
                         "calls": [{"name": "get_news", "args": {"ticker": "AAPL"}}]}],
                       raw_ok=False)
    res = InvestigationLoop(budget=_loop_budget(), toolbox=tb, llm=llm,
                            clock=lambda: 0.0).run("AAPL")
    check("D1 哨兵 → llm_exhausted，且未执行任何工具",
          res["stop_reason"] == StopReason.LLM_EXHAUSTED.value
          and res["spent"]["tool_calls"] == 0 and len(tb.calls) == 0,
          f"reason={res['stop_reason']} tools={res['spent']['tool_calls']}")
    blob = json.dumps(res, ensure_ascii=False, default=str)
    check("D2 **哨兵文本绝不流入报告（结论/假设/叙述）**",
          EXHAUSTED_SENTINEL not in blob and res["narrative"] == "")
    check("D3 哨兵停止时不会有假设被判已验证",
          all(h["status"] != "已验证" for h in res["hypotheses"]), str(_kinds(res)))

    # ── C2 振荡：动作集合 A,B,A → 原地打转即停 ──
    def _r(args_limit, note):
        return {"text": json.dumps({"attributions": [_attr()], "note": note}),
                "calls": [{"name": "get_news",
                           "args": {"ticker": "AAPL", "limit": args_limit}}]}
    tb = _FakeToolbox()
    res = InvestigationLoop(
        budget=_loop_budget(), toolbox=tb,
        llm=_ScriptedLLM([_r(20, "a"), _r(21, "b"), _r(20, "a")]),
        clock=lambda: 0.0).run("AAPL")
    check("C2 动作集合 A,B,A 重复 → oscillation",
          res["stop_reason"] == StopReason.OSCILLATION.value
          and res["spent"]["rounds"] == 3,
          f"reason={res['stop_reason']} rounds={res['spent']['rounds']}")

    # ── E 诚实降级：工具失败 → 不影响整轮收尾，且证据计零 ──
    tb = _FakeToolbox(payload={"ok": False, "reason": "新闻数据不可用"})
    res = InvestigationLoop(
        budget=_loop_budget(), toolbox=tb,
        llm=_ScriptedLLM([_r(20, "a"), {"text": json.dumps(
            {"attributions": [], "note": "无更多证据"}), "calls": []}]),
        clock=lambda: 0.0).run("AAPL")
    check("E1 工具失败仍能正常收尾（不抛、stop_reason 合法）",
          res["stop_reason"] in {r.value for r in StopReason}, res["stop_reason"])
    check("E2 失败的证据 ok=False 且带 reason，且计零（无假设被验证）",
          res["evidence"] and all(e["ok"] is False for e in res["evidence"])
          and all(h["status"] != "已验证" for h in res["hypotheses"]),
          str(_kinds(res)))

    # ── E3 零数据 → 零假设（闸门 1 端到端，关键断言）──
    tb = _FakeToolbox(frames={})
    res = InvestigationLoop(
        budget=_loop_budget(), toolbox=tb,
        llm=_ScriptedLLM([{"text": "x", "calls": [{"name": "get_news",
                                                  "args": {"ticker": "AAPL"}}]}]),
        clock=lambda: 0.0).run("AAPL")
    check("E3 **空三表 → 零假设、零 LLM 取证轮（不编造泛泛假设）**",
          res["hypotheses"] == [] and res["no_data"] is True
          and res["spent"]["rounds"] == 0 and res["spent"]["llm_calls"] == 0,
          f"hyps={len(res['hypotheses'])} rounds={res['spent']['rounds']}")
    check("E3b 零数据原因被如实写出", bool(res["data_note"]), res["data_note"])

    # ── 闸门 3 端到端：编造出处 / 越界索引 被拒并计入未采信 ──
    rounds = [
        {"text": json.dumps({"attributions": [
            _attr(idx=0, ref="https://完全编造的域名.example/x"),
            _attr(hid="H99", idx=0)], "note": "a"}),
         "calls": [{"name": "get_news", "args": {"ticker": "AAPL"}}]},
        {"text": json.dumps({"attributions": [], "note": "收尾"}), "calls": []}]
    tb = _FakeToolbox(payload={"ok": True, "articles": [
        {"title": "真实标题", "url": "https://real.example/1"}]})
    res = InvestigationLoop(budget=_loop_budget(), toolbox=tb,
                            llm=_ScriptedLLM(rounds), clock=lambda: 0.0).run("AAPL")
    ev = res["evidence"]
    check("G1 **编造出处被拒（attribution_valid=False）**",
          any(not e["attribution_valid"] and "编造" in e["reject_reason"] for e in ev),
          str([e["reject_reason"] for e in ev]))
    check("G2 不存在的假设 id 被拒",
          any(not e["attribution_valid"] and "id 不存在" in e["reject_reason"]
              for e in ev))
    check("G3 未采信项仍留档但计零（无假设被验证）",
          all(e["attribution_valid"] is False for e in ev)
          and all(h["status"] != "已验证" for h in res["hypotheses"]),
          str(_kinds(res)))

    # ── 越界索引被拒 ──
    tb = _FakeToolbox()
    res = InvestigationLoop(
        budget=_loop_budget(), toolbox=tb,
        llm=_ScriptedLLM([
            {"text": json.dumps({"attributions": [_attr(idx=7)], "note": "a"}),
             "calls": [{"name": "get_news", "args": {"ticker": "AAPL"}}]},
            {"text": json.dumps({"attributions": [], "note": "收尾"}), "calls": []}]),
        clock=lambda: 0.0).run("AAPL")
    check("G4 证据索引越界被拒",
          res["evidence"] and all(not e["attribution_valid"] for e in res["evidence"])
          and "越界" in res["evidence"][0]["reject_reason"],
          str([e["reject_reason"] for e in res["evidence"]]))

    # ── 正常路径：真实出处 → 部分验证；再来一条不同来源 → 已验证 ──
    tb = _FakeToolbox(payload={"ok": True, "articles": [
        {"title": "真实标题", "url": "https://real.example/1"}]})
    res = InvestigationLoop(
        budget=_loop_budget(), toolbox=tb,
        llm=_ScriptedLLM([
            {"text": json.dumps({"attributions": [_attr(idx=0)], "note": "a"}),
             "calls": [{"name": "get_news", "args": {"ticker": "AAPL"}}]},
            {"text": json.dumps({"attributions": [], "note": "收尾"}), "calls": []}]),
        clock=lambda: 0.0).run("AAPL")
    h1 = res["hypotheses"][0]
    check("G5 单条真实新闻支持 → 部分验证（不是已验证）",
          h1["status"] == "部分验证" and res["evidence"][0]["source_ref"]
          == "https://real.example/1" and res["evidence"][0]["attribution_valid"],
          f"{h1['status']} ref={res['evidence'][0]['source_ref']}")

    # 同一条假设被两种不同来源支持 → 已验证（索引是**全局**的：第 2 轮要引 ref 1）
    tb = _FakeToolbox(payload={"ok": True, "line_code": "REVENUE",
                               "line_label": "营业收入", "latest": 1100.0})
    res = InvestigationLoop(
        budget=_loop_budget(), toolbox=tb,
        llm=_ScriptedLLM([
            {"text": json.dumps({"attributions": [_attr(idx=0)], "note": "a"}),
             "calls": [{"name": "get_news", "args": {"ticker": "AAPL"}}]},
            {"text": json.dumps({"attributions": [_attr(idx=1)], "note": "b"}),
             "calls": [{"name": "get_statement_line",
                        "args": {"ticker": "AAPL", "line_code": "REVENUE"}}]},
            {"text": json.dumps({"attributions": [], "note": "收尾"}), "calls": []}]),
        clock=lambda: 0.0).run("AAPL")
    h1 = res["hypotheses"][0]
    check("G6 两种不同来源类型各 1 条支持 → 已验证",
          h1["status"] == "已验证" and sorted(h1["support_kinds"])
          == ["financial_statement", "news"],
          f"{h1['status']} kinds={h1.get('support_kinds')}")
    check("G6b 引用编号是全局的（ref 1 = 第 2 轮那次取数）",
          [e["source_kind"] for e in res["evidence"]] == ["news",
                                                          "financial_statement"],
          str([(e["source_kind"], e["tool"]) for e in res["evidence"]]))

    # 同一份结果被登记两次 → 来源类型只有一种，绝不能凑成已验证
    tb = _FakeToolbox(payload={"ok": True, "articles": [
        {"title": "t", "url": "https://real/1"}]})
    res = InvestigationLoop(
        budget=_loop_budget(), toolbox=tb,
        llm=_ScriptedLLM([
            {"text": json.dumps({"attributions": [_attr(idx=0)], "note": "a"}),
             "calls": [{"name": "get_news", "args": {"ticker": "AAPL"}}]},
            {"text": json.dumps({"attributions": [_attr(idx=0), _attr(idx=0)],
                                 "note": "b"}), "calls": []}]),
        clock=lambda: 0.0).run("AAPL")
    check("G7 同一份结果登记两次不能凑成已验证",
          res["hypotheses"][0]["status"] == "部分验证",
          f"{res['hypotheses'][0]['status']} "
          f"kinds={res['hypotheses'][0].get('support_kinds')}")

    # record_evidence 工具调用路径（不占 tool_calls 预算）
    tb = _FakeToolbox(payload={"ok": True, "articles": [
        {"title": "真实标题", "url": "https://real.example/9"}]})
    res = InvestigationLoop(
        budget=_loop_budget(), toolbox=tb,
        llm=_ScriptedLLM([
            {"text": "先取新闻", "calls": [
                {"name": "get_news", "args": {"ticker": "AAPL"}}]},
            {"text": "登记证据", "calls": [
                {"name": "record_evidence",
                 "args": {"hypothesis_id": "H1", "relation": "supports",
                          "ref_index": 0, "rationale": "新闻印证需求走弱"}}]},
            {"text": json.dumps({"attributions": [], "note": "收尾"}), "calls": []}]),
        clock=lambda: 0.0).run("AAPL")
    h1 = res["hypotheses"][0]
    check("G8 record_evidence 工具路径生效且不占工具预算",
          res["evidence"] and res["evidence"][0]["attribution_valid"]
          and h1["status"] == "部分验证"
          and res["spent"]["tool_calls"] == 1,
          f"status={h1['status']} tools={res['spent']['tool_calls']} "
          f"ev={len(res['evidence'])}")
    check("G8b 未提供 source_ref 时用工具结果的规范出处（不是模型编的）",
          res["evidence"][0]["source_ref"] == "https://real.example/9",
          str(res["evidence"][0]["source_ref"]))

    # record_evidence 里编造 source_ref → 被拒
    tb = _FakeToolbox(payload={"ok": True, "articles": [
        {"title": "真实标题", "url": "https://real.example/9"}]})
    res = InvestigationLoop(
        budget=_loop_budget(), toolbox=tb,
        llm=_ScriptedLLM([
            {"text": "取新闻", "calls": [
                {"name": "get_news", "args": {"ticker": "AAPL"}}]},
            {"text": "登记", "calls": [
                {"name": "record_evidence",
                 "args": {"hypothesis_id": "H1", "relation": "supports",
                          "ref_index": 0,
                          "source_ref": "https://编造的.example/x"}}]},
            {"text": json.dumps({"attributions": [], "note": "收尾"}), "calls": []}]),
        clock=lambda: 0.0).run("AAPL")
    check("G9 record_evidence 里编造出处同样被拒",
          res["evidence"] and not res["evidence"][0]["attribution_valid"]
          and "编造" in res["evidence"][0]["reject_reason"],
          str(res["evidence"][0]["reject_reason"]) if res["evidence"] else "无证据")

    # ── 预算真的会拦住循环（端到端，不是只看 check_stop 单测）──
    tb = _FakeToolbox()
    rounds = [{"text": json.dumps({"attributions": [], "note": f"r{i}"}),
               "calls": [{"name": "search_knowledge",
                          "args": {"query": f"不同查询{i}"}}]}
              for i in range(9)]
    res = InvestigationLoop(budget=_loop_budget(max_rounds=2), toolbox=tb,
                            llm=_ScriptedLLM(rounds), clock=lambda: 0.0).run("AAPL")
    check("A12 端到端轮次上限：恰好 2 轮后停",
          res["spent"]["rounds"] == 2
          and res["stop_reason"] == StopReason.MAX_ROUNDS.value,
          f"rounds={res['spent']['rounds']} reason={res['stop_reason']}")

    res = InvestigationLoop(budget=_loop_budget(max_tool_calls=2), toolbox=tb,
                            llm=_ScriptedLLM(rounds), clock=lambda: 0.0).run("AAPL")
    check("A13 端到端工具上限：恰好 2 次执行后停",
          res["spent"]["tool_calls"] == 2
          and res["stop_reason"] == StopReason.MAX_TOOL_CALLS.value,
          f"tools={res['spent']['tool_calls']} reason={res['stop_reason']}")

    # 墙钟（假时钟每轮跳 100s）
    t = {"v": 0.0}

    def _clk():
        return t["v"]

    class _TickLLM(_ScriptedLLM):
        def chat_once_with_tools(self, messages, **kw):
            out = super().chat_once_with_tools(messages, **kw)
            t["v"] += 100.0
            return out

    tb = _FakeToolbox()
    res = InvestigationLoop(
        budget=_loop_budget(deadline_seconds=250, max_rounds=99), toolbox=tb,
        llm=_TickLLM(rounds), clock=_clk).run("AAPL")
    check("A14 端到端墙钟：剩余不足 min_round_seconds 即停且不超轮次预算",
          res["stop_reason"] == StopReason.DEADLINE.value
          and res["spent"]["rounds"] < 99,
          f"reason={res['stop_reason']} rounds={res['spent']['rounds']}")

    # 进度事件：每轮至少一次 progress，且都带 spent
    events = []
    tb = _FakeToolbox()
    InvestigationLoop(budget=_loop_budget(max_rounds=2), toolbox=tb,
                      llm=_ScriptedLLM(rounds), clock=lambda: 0.0,
                      progress=events.append).run("AAPL")
    check("P1 每轮都推进度事件且带 spent/limits 信息",
          len(events) >= 3
          and all("spent" in e and "pct" in e and "round" in e for e in events)
          and events[-1]["phase"] == "conclude",
          f"n={len(events)} last={events[-1]['phase'] if events else None}")
    check("P2 进度里的假设状态随轮次推进（UI 可呈现）",
          any(e.get("hypotheses") for e in events))

    # ── 正常措辞路径（非降级）：模型按播种族措辞 ──
    tb = _FakeToolbox()
    res = InvestigationLoop(
        budget=_loop_budget(), toolbox=tb,
        llm=_ScriptedLLM([{"text": json.dumps({"attributions": [], "note": "收尾"}),
                           "calls": []}], frame_family="demand_weakness"),
        clock=lambda: 0.0).run("AAPL")
    check("L1 正常措辞路径：族来自模型措辞且非降级",
          res["hypotheses"] and res["hypotheses"][0]["family"] == "demand_weakness"
          and res["degrade_note"] == "",
          f"n={len(res['hypotheses'])} note={res['degrade_note']!r}")

    # ── 单假设证据上限（防止一个假设吃光预算）──
    tb = _FakeToolbox()
    many = [{"text": json.dumps({"attributions": [_attr(idx=0)], "note": f"r{i}"}),
             "calls": [{"name": "get_news",
                        "args": {"ticker": "AAPL", "limit": 20 - i}}]}
            for i in range(6)]
    many.append({"text": json.dumps({"attributions": [], "note": "收尾"}), "calls": []})
    res = InvestigationLoop(budget=_loop_budget(max_evidence_per_hypothesis=2),
                            toolbox=tb, llm=_ScriptedLLM(many),
                            clock=lambda: 0.0).run("AAPL")
    check("L2 单假设证据数被封顶（max_evidence_per_hypothesis）",
          all(len(h["evidence"]) <= 2 for h in res["hypotheses"]),
          str([len(h["evidence"]) for h in res["hypotheses"]]))


# ── Part G：报告组装 ────────────────────────────────────────

class _BoomLLM:
    def chat(self, messages, **kwargs):
        raise RuntimeError("总述服务炸了")


class _ProseLLM:
    def chat(self, messages, **kwargs) -> str:
        return "总述文本"


def _handmade_run() -> dict:
    """手工 run：一次覆盖 已验证 / 未验证 / 证据不足 / 模型推断 / 未采信 / 缺数。"""
    ev = lambda i, hid, kind, strength, valid=True, reason="", ref="": {  # noqa: E731
        "id": i, "hypothesis_id": hid, "relation": "supports",
        "tool": "t", "args": {}, "source_kind": kind, "strength": strength,
        "source_ref": ref, "snippet": "片段", "ok": True,
        "attribution_valid": valid, "reject_reason": reason, "rationale": "r", "round": 1}
    return {
        "ticker": "TEST", "period": "annual", "depth": "standard",
        "stop_reason": "max_rounds",
        "limits": {"max_rounds": 6, "max_tool_calls": 14, "max_llm_calls": 10,
                   "deadline_seconds": 300},
        "spent": {"rounds": 6, "tool_calls": 9, "llm_calls": 7, "duplicates": 2,
                  "elapsed": 123.4, "remaining": 1.0},
        "budget_overshoot_seconds": 12.5,
        "signals": [{"code": "revenue_growth", "label": "营收同比", "value": -8.0,
                     "unit": "%", "direction": "down", "threshold": "|x| >= 5.0",
                     "material": True, "baseline": 100.0, "delta": -8.0,
                     "periods": ["2022", "2023"], "note": ""}],
        "material_signals": [{"code": "revenue_growth", "label": "营收同比", "value": -8.0,
                              "unit": "%", "direction": "down",
                              "threshold": "|x| >= 5.0"}],
        "seeded_families": [{"family": "demand_contraction", "label": "需求收缩",
                             "seeded_by": ["revenue_growth"],
                             "observables": ["订单"], "why": "w"}],
        "hypotheses": [
            {"id": "H1", "family": "demand_contraction", "family_label": "需求收缩",
             "statement": "下游需求收缩", "status": "已验证",
             "status_reason": "≥2 条支持且来自 2 种来源类型：news、peer",
             "support_count": 2, "contradict_count": 0, "rejected_count": 1,
             "support_kinds": ["news", "peer"], "prior_confidence": "high",
             "seeded_by": ["revenue_growth"], "predicted_observables": ["订单"],
             "why_it_matters": "重要"},
            {"id": "H2", "family": "earnings_quality", "family_label": "盈利质量下降",
             "statement": "现金流质量恶化", "status": "证据不足",
             "status_reason": "无可采信证据（未采信项计零）",
             "support_count": 0, "contradict_count": 0, "rejected_count": 0,
             "support_kinds": [], "prior_confidence": "low", "seeded_by": [],
             "predicted_observables": [], "why_it_matters": ""},
            {"id": "H3", "family": "other", "family_label": "其他",
             "statement": "净利下滑但毛利未降", "status": "未验证",
             "status_reason": "有可采信证据但无一支持",
             "support_count": 0, "contradict_count": 0, "rejected_count": 0,
             "support_kinds": [], "prior_confidence": "low", "seeded_by": [],
             "predicted_observables": [], "why_it_matters": ""},
        ],
        "status_counts": {"已验证": 1, "证据不足": 1, "未验证": 1},
        "evidence": [
            ev("E1", "H1", "news", "medium", ref="https://real.example/1"),
            ev("E2", "H1", "peer", "strong", ref="peer-table"),
            ev("E3", "H1", "llm_hypothesis", "weak"),
            ev("E4", "H1", "news", "medium", valid=False,
               reason="自述出处未出现在该工具结果中（疑似编造）"),
        ],
        "narrative": "n", "degrade_note": "", "data_note": "", "no_data": False,
        "disclaimer": "d",
    }


def check_report():
    print("\n=== Part G: 报告组装 ===")
    from app.investigation.report import build_report, report_markdown, fmt_num

    check("G0 缺失数值渲染为 —（不填 0）",
          fmt_num(None) == "—" and fmt_num(float("nan")) == "—"
          and fmt_num(1.234) == "1.234" and fmt_num(-8.0, "%") == "-8.0%",
          f"{fmt_num(None)} {fmt_num(float('nan'))} {fmt_num(-8.0, '%')}")

    rep = build_report(_handmade_run(), llm=None, prose=False)
    check("G1 每条假设都有状态与展示标签",
          len(rep["hypotheses"]) == 3
          and all(r["status_label"] and r["status_color"] for r in rep["hypotheses"]),
          str([r["status_label"] for r in rep["hypotheses"]]))

    h1 = rep["hypotheses"][0]
    check("G2 已验证的假设渲染 ≥2 条采信证据，未采信项单列",
          h1["status"] == "已验证" and len(h1["evidence"]) == 3
          and len(h1["rejected_evidence"]) == 1
          and "编造" in h1["rejected_evidence"][0]["reject_reason"],
          f"accepted={len(h1['evidence'])} rejected={len(h1['rejected_evidence'])}")

    check("G3 未验证原因单列，且不含已验证项",
          {u["id"] for u in rep["unverified_causes"]} == {"H2", "H3"},
          str([u["id"] for u in rep["unverified_causes"]]))

    check("G4 模型推断单独台账且标 confidence=low",
          len(rep["llm_hypothesis_ledger"]) == 1
          and rep["llm_hypothesis_ledger"][0]["confidence"] == "low"
          and rep["llm_hypothesis_ledger"][0]["hypothesis_id"] == "H1")

    check("G5 墙钟超出量被如实写出（不假装硬停）",
          rep["budget_overshoot_seconds"] == 12.5)

    md = report_markdown(rep)
    check("G6 Markdown 含关键章节",
          all(t in md for t in ("## 触发的真实财务信号", "## 成因假设与证据",
                                "## 未验证原因", "## 模型推断台账", "诚实边界")),
          [t for t in ("## 触发的真实财务信号", "## 成因假设与证据",
                       "## 未验证原因", "## 模型推断台账", "诚实边界")
           if t not in md])
    check("G7 Markdown 里未采信项被标明计零",
          "未采信（计零，仅留档）" in md)
    check("G8 报告可 JSON 序列化（API 直接返回）",
          isinstance(json.loads(json.dumps(rep, ensure_ascii=False, default=str)), dict))

    # 总述：成功路径
    rep2 = build_report(_handmade_run(), llm=_ProseLLM(), prose=True)
    check("G9 总述成功时写入且无降级注记",
          rep2["prose"] == "总述文本" and rep2["prose_note"] == "")

    # 总述：失败路径不能影响确定性结论
    rep3 = build_report(_handmade_run(), llm=_BoomLLM(), prose=True)
    check("G10 **总述失败仍出完整报告，并如实标注**",
          bool(rep3["prose"] == "" and "总述生成失败" in rep3["prose_note"]
               and len(rep3["hypotheses"]) == 3 and rep3["unverified_causes"]),
          rep3["prose_note"])
    check("G11 总述失败时 Markdown 也完整",
          "## 成因假设与证据" in report_markdown(rep3)
          and "总述生成失败" in report_markdown(rep3))

    # 端到端：从真实 loop 输出组装报告
    tb = _FakeToolbox(payload={"ok": True, "line_code": "REVENUE",
                               "line_label": "营业收入", "latest": 1100.0})
    from app.investigation.loop import InvestigationLoop
    run = InvestigationLoop(
        budget=_loop_budget(), toolbox=tb,
        llm=_ScriptedLLM([
            {"text": json.dumps({"attributions": [_attr(idx=0)], "note": "a"}),
             "calls": [{"name": "get_news", "args": {"ticker": "AAPL"}}]},
            {"text": json.dumps({"attributions": [_attr(idx=1)], "note": "b"}),
             "calls": [{"name": "get_statement_line",
                        "args": {"ticker": "AAPL", "line_code": "REVENUE"}}]},
            {"text": json.dumps({"attributions": [], "note": "收尾"}), "calls": []}]),
        clock=lambda: 0.0).run("AAPL")
    rep4 = build_report(run, llm=None, prose=False)
    verified = [r for r in rep4["hypotheses"] if r["status"] == "已验证"]
    check("G12 端到端：loop → 报告，已验证项确实渲染 ≥2 条证据",
          verified and all(len(r["evidence"]) >= 2 for r in verified),
          f"verified={len(verified)}")
    check("G13 端到端：报告带消耗与停止原因（可审计）",
          rep4["spent"]["rounds"] > 0 and rep4["stop_reason"]
          and rep4["stop_reason_label"])


# ── Part H：API 接入 ────────────────────────────────────────

def _api_run(ticker: str = "TEST"):
    """产出一次**真实**循环的 run（用替身依赖，无网络无 LLM）。"""
    from app.investigation.loop import InvestigationLoop
    tb = _FakeToolbox(payload={"ok": True, "line_code": "REVENUE",
                               "line_label": "营业收入", "latest": 1100.0})
    return InvestigationLoop(
        budget=_loop_budget(), toolbox=tb,
        llm=_ScriptedLLM([
            {"text": json.dumps({"attributions": [_attr(idx=0)], "note": "a"}),
             "calls": [{"name": "get_news", "args": {"ticker": ticker}}]},
            {"text": json.dumps({"attributions": [_attr(idx=1)], "note": "b"}),
             "calls": [{"name": "get_statement_line",
                        "args": {"ticker": ticker, "line_code": "REVENUE"}}]},
            {"text": json.dumps({"attributions": [], "note": "收尾"}), "calls": []}]),
        clock=lambda: 0.0).run(ticker)


def check_api():
    print("\n=== Part H: API 接入 ===")
    import threading
    from unittest import mock

    from fastapi.testclient import TestClient

    import api.routes_investigation as ri

    def fake_run(ticker, **kwargs):
        kwargs.get("progress") and kwargs["progress"]({
            "phase": "round", "round": 1, "max_rounds": 6, "label": "第1轮",
            "message": "m", "pct": 10, "tool": "get_news",
            "spent": {"rounds": 1, "tool_calls": 0, "llm_calls": 1, "duplicates": 0,
                      "elapsed": 0.1, "remaining": 299.9},
            "hypotheses": []})
        return _api_run(ticker)

    ri._tasks.clear()
    ri._by_key.clear()

    from api.main import app
    with mock.patch.object(ri, "run_investigation", fake_run), \
            mock.patch.object(ri, "_get_llm", lambda: None):
        with TestClient(app) as client:
            # 同步跑通
            r = client.post("/api/v1/investigation/run-sync/TEST?depth=standard")
            check("H1 run-sync 返回报告", r.status_code == 200 and r.json().get("ticker")
                  == "TEST", f"http {r.status_code}")
            rep = r.json()
            check("H2 报告含假设/消耗/停止原因（可审计）",
                  bool(rep.get("hypotheses") and rep.get("spent", {}).get("rounds")
                       and rep.get("stop_reason")), str(rep.get("stop_reason")))
            run_id = ri._by_key.get("TEST|standard")
            check("H3 按 run_id 建键（不是 ticker）", bool(run_id), str(run_id))

            st = client.get(f"/api/v1/investigation/status/{run_id}")
            check("H4 status 返回 spent/limits/hypotheses",
                  st.status_code == 200 and st.json()["status"] == "completed"
                  and st.json()["limits"]["max_rounds"]
                  and st.json()["hypotheses"],
                  f"http {st.status_code}")

            res = client.get(f"/api/v1/investigation/result/{run_id}")
            check("H5 result 返回完整报告", res.status_code == 200
                  and "hypotheses" in res.json())

            byt = client.get("/api/v1/investigation/result/by-ticker/TEST?depth=standard")
            check("H6 by-ticker 取最近一次完成结果", byt.status_code == 200
                  and byt.json().get("ticker") == "TEST")

            md = client.get(f"/api/v1/investigation/report/{run_id}")
            check("H7 Markdown 报告可导出且含关键章节",
                  md.status_code == 200 and "## 成因假设与证据" in md.text
                  and "诚实边界" in md.text, f"http {md.status_code}")

            check("H8 未知 run_id → 404",
                  client.get("/api/v1/investigation/status/nope").status_code == 404
                  and client.get("/api/v1/investigation/result/nope").status_code == 404)

            # 409 防重入：用一个卡住的替身让任务停在 running
            gate = threading.Event()

            def slow_run(ticker, **kwargs):
                gate.wait(timeout=8)
                return _api_run(ticker)

            with mock.patch.object(ri, "run_investigation", slow_run):
                a = client.post("/api/v1/investigation/run/SLOW?depth=deep")
                b = client.post("/api/v1/investigation/run/SLOW?depth=deep")
                check("H9 同标的同档位重复启动 → 409",
                      a.status_code == 200 and b.status_code == 409,
                      f"{a.status_code}/{b.status_code}")
                c = client.post("/api/v1/investigation/run/SLOW?depth=standard")
                check("H9b 不同档位可并存（按 run_id 建键的收益）", c.status_code == 200,
                      str(c.status_code))
                gate.set()

            # 配置关闭 → 任务 failed 且原因如实
            class _Off:
                INVESTIGATION_ENABLED = False
            with mock.patch.object(ri, "get_settings", lambda: _Off()):
                client.post("/api/v1/investigation/run/OFF?depth=standard")
                off_id = ri._by_key.get("OFF|standard")
                import time as _t
                for _ in range(40):
                    if ri._tasks[off_id]["status"] == "failed":
                        break
                    _t.sleep(0.05)
                check("H10 INVESTIGATION_ENABLED=false → 如实失败",
                      ri._tasks[off_id]["status"] == "failed"
                      and "关闭" in (ri._tasks[off_id].get("error") or ""),
                      str(ri._tasks[off_id].get("error")))

    # 路由已挂到 app 上
    paths = {getattr(r, "path", "") for r in app.routes}
    check("H11 路由已接入 main.py",
          "/api/v1/investigation/run/{ticker}" in paths
          and "/api/v1/investigation/stream/{run_id}" in paths,
          str(sorted(p for p in paths if "investigation" in p)))


def check_live():
    """活体 curl（可选，需后端已启动）。

    后端未起时不计数——本节是可选增强，不能因为它让整套离线验收变红。
    能连上时才真正断言。
    """
    print("\n=== Part H2: 活体 curl（可选；未启动则跳过，不计入结果）===")
    import subprocess
    url = "http://127.0.0.1:8000/api/v1/investigation/run-sync/600519.SS?depth=standard"
    try:
        p = subprocess.run(["curl", "-s", "--max-time", "300", "-X", "POST", url],
                           capture_output=True, timeout=320)
        out = (p.stdout or b"").decode("utf-8", "replace")
    except Exception as e:                          # noqa: BLE001
        print(f"[SKIP] 活体：后端未启动或网络不可达（{type(e).__name__}），如实跳过")
        return
    if not out.strip():
        print("[SKIP] 活体：后端未启动或返回空，如实跳过")
        return
    try:
        data = json.loads(out)
    except Exception:                               # noqa: BLE001
        print(f"[SKIP] 活体：响应非 JSON（{out[:80]!r}），如实跳过")
        return
    spent, limits = data.get("spent") or {}, data.get("limits") or {}
    check("H2 活体：stop_reason 在已知集合内",
          data.get("stop_reason") in {
              "all_hypotheses_settled", "max_rounds", "max_tool_calls",
              "max_llm_calls", "deadline", "no_progress", "oscillation",
              "llm_exhausted", "llm_error"},
          str(data.get("stop_reason")))
    check("H2b 活体：实际消耗不超过上限",
          all(spent.get(k, 0) <= limits.get(k, 0)
              for k in ("rounds", "tool_calls", "llm_calls")),
          f"spent={spent} limits={limits}")


def main():
    check_budget()
    check_signals()
    check_degradation()
    check_engine()
    check_tools()
    check_hypotheses()
    check_loop()
    check_report()
    check_api()
    check_live()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
