"""离线验证：技术指标 + 投资策略交易信号。

- 用真实历史行情（本地缓存优先，网络兜底）构造指标，断言确定性 / 数值关系。
- build_trade_plan 结构完整、缺失输入降级不抛异常。
- 合规：含「进场/止损位/仓位」的策略文本命中 check_advice，apply_gate 附免责声明。

用法: D:\\python\\python.exe scripts\\verify_trade_plan.py
退出码: 0 = 全部通过；1 = 存在失败。
"""
from __future__ import annotations

import sys
import traceback

# Windows 控制台 GBK 无法编码部分 Unicode（✓/中文标题），兜底 UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("gb"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, ".")  # 项目根

import numpy as np
import pandas as pd

from app.analysis.technical import compute_indicators
from app.analysis.trade_plan import build_trade_plan
from app.compliance.rules import apply_gate, check_advice
from app.report.charts import ChartGenerator
from app.report.markdown import result_to_markdown
from app.report.pdf import result_to_pdf

_PASS = 0
_FAIL = 0


def _ok(name: str, cond: bool, detail: str = ""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def _get_real_history():
    """真实历史行情：先试本地数据缓存，失败则网络拉取。"""
    from app.data.router import get_market_data
    md = get_market_data()
    for ticker in ("600519", "AAPL"):
        try:
            df = md.get_history(ticker, "1y")
            if df is not None and len(df) >= 60:
                print(f"  [数据] {ticker} 1y 历史 {len(df)} 行")
                return df
        except Exception as e:
            print(f"  [数据] {ticker} 获取失败: {e}")
    return None


def _test_indicators(df: pd.DataFrame | None):
    print("== 技术指标 compute_indicators ==")
    if df is None:
        print("  [跳过] 无真实历史数据")
        return

    ind = compute_indicators(df)
    _ok("available=True", ind.get("available") is True)
    _ok("close 存在且 >0", ind.get("close") is not None and ind["close"] > 0)
    _ok("MA5/MA20/MA60 存在", all(ind.get(k) is not None for k in ("ma5", "ma20", "ma60")))
    _ok("RSI∈[0,100]", ind.get("rsi14") is not None and 0 <= ind["rsi14"] <= 100)
    boll = (ind.get("boll_lower"), ind.get("boll_mid"), ind.get("boll_upper"))
    _ok("布林 下<中<上", all(v is not None for v in boll) and boll[0] < boll[1] < boll[2])
    _ok("趋势字段合法", ind.get("trend") in ("多头", "空头", "粘合"))
    _ok("52周位置∈[0,1.2]", ind.get("pos_52w") is None or 0 <= ind["pos_52w"] <= 1.2)
    sup, res = ind.get("support_20"), ind.get("resistance_20")
    _ok("支撑≤压力", sup is None or res is None or sup <= res)

    # 确定性：同输入两次输出相等（浮点全等）
    ind2 = compute_indicators(df)
    keys = [k for k in ind if k not in ("reason",) and not k.startswith("n_")]
    eq = all(ind[k] == ind2[k] for k in keys)
    _ok("确定性（同输入两次相等）", eq)

    # 空 DataFrame → available=False 不抛异常
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    try:
        e = compute_indicators(empty)
        _ok("空 DataFrame 降级", e.get("available") is False)
    except Exception:
        _ok("空 DataFrame 降级", False, f"抛异常:\n{traceback.format_exc()}")

    # 缺列 → available=False
    bad = pd.DataFrame({"Close": [1, 2, 3]})
    try:
        b = compute_indicators(bad)
        _ok("缺列降级", b.get("available") is False)
    except Exception:
        _ok("缺列降级", False, f"抛异常:\n{traceback.format_exc()}")


def _test_trade_plan(df: pd.DataFrame | None):
    print("== build_trade_plan 策略合成 ==")
    val = {
        "dcf": {"fair_value": 120.0, "upside": 0.15},
        "relative_valuation": {"pe": {"company": 25.0, "industry_avg": 30.0, "premium": -0.167}},
    }
    fin = {"current_price": 105.0}

    tp = build_trade_plan(price_data=df, valuation_result=val,
                          swot_rating="看好", company_info={}, financial_result=fin)
    _ok("结构 available=True", tp.get("available") is True)
    _ok("评级 ∈ 枚举", tp.get("rating") in ("强烈看多", "看多", "中性", "看空", "强烈看空"))
    _ok("仓位 ∈ 枚举", tp.get("position_hint") in ("观望", "轻仓", "半仓", "标准仓"))
    ez = tp.get("entry_zone") or [None, None]
    _ok("进场区间 2 元素", isinstance(ez, list) and len(ez) == 2)
    sl, tpv = tp.get("stop_loss"), tp.get("take_profit")
    _ok("止损/止盈为 None 或数值", all(v is None or isinstance(v, (int, float)) for v in (sl, tpv)))
    if df is not None and sl is not None and ez[0] is not None:
        _ok("止损 < 进场下限", sl < ez[0])
    _ok("reasons 非空", len(tp.get("reasons") or []) > 0)
    _ok("score_breakdown 字段", all(k in tp.get("score_breakdown", {}) for k in
                                    ("tech_score", "val_score", "total", "factors")))
    _ok("风险收益比为 None 或正数", tp.get("risk_reward_ratio") is None or tp.get("risk_reward_ratio") > 0,
        f"rr={tp.get('risk_reward_ratio')}")
    _ok("置信度枚举", tp.get("confidence_level") in ("高", "中", "低"),
        f"conf={tp.get('confidence_level')}")
    _ok("周线趋势/共振字段", tp.get("weekly_trend") in ("多头", "空头", "粘合", None)
        and tp.get("weekly_resonance") in ("共振", "背离", "周线参考", None))

    # 确定性
    tp2 = build_trade_plan(price_data=df, valuation_result=val,
                           swot_rating="看好", company_info={}, financial_result=fin)
    _ok("策略确定性", tp.get("rating") == tp2.get("rating")
        and tp.get("position_hint") == tp2.get("position_hint"))

    # 缺失输入降级
    tp_none = build_trade_plan(price_data=None, valuation_result=None,
                               swot_rating=None, company_info=None, financial_result=None)
    _ok("全空输入不抛异常", isinstance(tp_none, dict) and tp_none.get("available") is True)
    _ok("全空输入 reasons 有值", len(tp_none.get("reasons") or []) > 0)
    _ok("全空输入评级中性或兜底", tp_none.get("rating") is not None)

    # 高估估值 → 应非看多（估值 -2 分压制）
    val_high = {"dcf": {"fair_value": 60.0, "upside": -0.4},
                "relative_valuation": {"pe": {"company": 45.0, "industry_avg": 30.0, "premium": 0.5}}}
    tp_high = build_trade_plan(price_data=df, valuation_result=val_high,
                               swot_rating="谨慎", company_info={}, financial_result=fin)
    _ok("高估输入非『强烈看多』", tp_high.get("rating") != "强烈看多")


def _test_compliance():
    print("== 合规检测 ==")
    body = "建议在进场参考区间 90~110 建仓，止损位设 85，标准仓位。"
    flagged, hits = check_advice(body)
    _ok("命中合规词", flagged and len(hits) > 0, f"hits={hits}")
    gated, _, hit = apply_gate(body, source="verify_trade_plan")
    _ok("apply_gate 附免责声明", hit and "合规提示" in gated)


def _test_kline(df: pd.DataFrame | None):
    """K线 + 均线叠加图：真实行情列式化 → 生成 PNG → 确定性 / 降级 / 章节内嵌。"""
    print("== K线 + 均线叠加图 ==")
    import hashlib
    import os
    import shutil

    tmp = "data/reports/_verify_kline"
    os.makedirs(tmp, exist_ok=True)
    gen = ChartGenerator(output_dir=tmp)

    # 缺失 price_history → None 不抛异常
    try:
        p = gen._price_action_chart({"price_history": {"available": False}},
                                    "pa_missing.png")
        _ok("缺失行情 → None", p is None)
    except Exception:
        _ok("缺失行情 → None", False, f"抛异常:\n{traceback.format_exc()}")

    if df is None:
        print("  [跳过] 无真实历史数据")
        return

    tail = df.tail(300)
    result = {
        "price_history": {
            "available": True,
            "dates": [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
                      for d in tail.index],
            "open": [float(x) for x in tail["Open"]],
            "high": [float(x) for x in tail["High"]],
            "low": [float(x) for x in tail["Low"]],
            "close": [float(x) for x in tail["Close"]],
            "volume": [float(x) for x in tail["Volume"]],
        },
        "trade_plan": {"available": True, "entry_zone": [90.0, 120.0],
                       "stop_loss": 85.0, "take_profit": 130.0},
    }

    try:
        p1 = gen._price_action_chart(result, "price_action.png")
        _ok("K线图生成", p1 is not None and os.path.exists(p1)
            and os.path.getsize(p1) > 0, f"p1={p1}")
        if p1:
            p2 = gen._price_action_chart(result, "price_action2.png")
            same = (p2 is not None
                    and hashlib.md5(open(p1, "rb").read()).hexdigest()
                    == hashlib.md5(open(p2, "rb").read()).hexdigest())
            _ok("K线图确定性（同输入两次字节相等）", same)
    except Exception:
        _ok("K线图生成", False, f"抛异常:\n{traceback.format_exc()}")

    # 报告章节内嵌 + 附录去重
    md = result_to_markdown(result,
                            charts={"price_action": os.path.join(tmp, "price_action.png")})
    _ok("md 章节内嵌 K线图", "![日K线与均线叠加" in md)
    appendix = md.split("## 附录：图表", 1)
    _ok("md 附录不含 price_action", len(appendix) == 1 or "price_action" not in appendix[1])

    # PDF 章节渲染不崩
    try:
        pdf_path = os.path.join(tmp, "report.pdf")
        result_to_pdf(result, charts={"price_action": p1}, output_path=pdf_path)
        _ok("pdf 章节渲染不崩", os.path.exists(pdf_path)
            and os.path.getsize(pdf_path) > 0)
    except Exception:
        _ok("pdf 章节渲染不崩", False, f"抛异常:\n{traceback.format_exc()}")

    shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 60)
    print("verify_trade_plan.py — 技术指标 + 投资策略交易信号 + K线图")
    print("=" * 60)

    df = _get_real_history()
    _test_indicators(df)
    print()
    _test_trade_plan(df)
    print()
    _test_compliance()
    print()
    _test_kline(df)

    print()
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
