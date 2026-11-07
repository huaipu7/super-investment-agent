"""验证：LLM 结构化输出的提取与校验（app/llm/structured.py + app/llm/schemas.py）。

覆盖：
  (a) extract_json 稳健性：围栏 / 前置散文 / 后置散文 / 字符串内括号 / 数组 / 垃圾
  (b) 结构严格：容器不符契约 → 回退 default，并报出字段路径
  (c) 叶子宽容：坏叶子（数字写成字符串、"35%"、int 出现在 str 字段）不毁整份结果
  (d) 意图识别三层降级：FC 越界 → JSON 层 → 规则层
  (e) 分析器接线：Segment/News/Meeting/SWOT 经新解析器后下游可正常消费

运行: D:\\python\\python.exe scripts\\verify_structured_output.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


class FakeClient:
    """替身 LLM：chat() 返回预置文本。"""

    def __init__(self, response: str):
        self.response = response

    def chat(self, messages, **kwargs) -> str:
        return self.response


def main():
    from app.llm.structured import extract_json, parse_llm_json
    from app.llm.schemas import (
        SwotResult, SegmentResult, NewsResult, MeetingResult,
        GeopoliticalResult, ComparisonResult, Competitor, IntentResult,
    )

    # ── (a) extract_json 稳健性 ───────────────────────────────
    check("(a) 裸 JSON", extract_json('{"a": 1}') == {"a": 1})
    check("(a) ```json 围栏",
          extract_json('```json\n{"a": 1}\n```') == {"a": 1})
    check("(a) 无语言标记围栏",
          extract_json('```\n{"a": 1}\n```') == {"a": 1})
    check("(a) 前置散文",
          extract_json('分析如下：\n{"a": 1}') == {"a": 1})
    check("(a) 后置散文",
          extract_json('{"a": 1}\n以上就是结论。') == {"a": 1})
    check("(a) 字符串内含花括号",
          extract_json('{"s": "x} y{", "n": 2}') == {"s": "x} y{", "n": 2})
    check("(a) 数组", extract_json('[{"name": "A"}]') == [{"name": "A"}])
    check("(a) 散文含干扰花括号后取到真 JSON",
          extract_json('说明{不是JSON}，结果：{"a": 9}') == {"a": 9})

    try:
        extract_json("完全不是 JSON")
        check("(a) 垃圾输入抛错", False)
    except Exception:
        check("(a) 垃圾输入抛错", True)

    # ── (b) 结构严格 ──────────────────────────────────────────
    r = parse_llm_json('{"segments": "oops"}', model=SegmentResult,
                       default={"FB": 1}, context="T")
    check("(b) segments 非列表 → 回退 default", r == {"FB": 1})

    r = parse_llm_json('{"no_swot": 1}', model=SwotResult, default={"FB": 1}, context="T")
    check("(b) 缺 swot 键 → 回退 default", r == {"FB": 1})

    r = parse_llm_json('{"intent": "buy_everything"}', model=IntentResult,
                       default=None, context="T")
    check("(b) intent 越界枚举 → None", r is None)

    r = parse_llm_json("完全不是 JSON", model=NewsResult, default={"FB": 1}, context="T")
    check("(b) 非 JSON → 回退 default", r == {"FB": 1})

    # ── (c) 叶子宽容 ──────────────────────────────────────────
    r = parse_llm_json(
        '{"segments":[{"name":"云","revenue_share":"35%","net_income_share":0.4}]}',
        model=SegmentResult, default={}, context="T")
    seg = r["segments"][0]
    check("(c) \"35%\" → 0.35", seg["revenue_share"] == 0.35)
    check("(c) 占比可参与数值求和（不再 TypeError）",
          abs(sum(s["revenue_share"] for s in r["segments"]) - 0.35) < 1e-9)

    r = parse_llm_json('{"swot":{"strengths":[{"point":123}]}}',
                       model=SwotResult, default={"FB": 1}, context="T")
    check("(c) int 出现在 str 字段 → 强转而非丢弃",
          r.get("swot", {}).get("strengths", [{}])[0].get("point") == "123")

    r = parse_llm_json('{"swot":{"strengths":["纯字符串优势"]}}',
                       model=SwotResult, default={"FB": 1}, context="T")
    check("(c) 字符串条目 → 归一化为 {point}",
          r["swot"]["strengths"][0]["point"] == "纯字符串优势")

    r = parse_llm_json('{"sentiment":"bullish","sentiment_score":"看多","summary":"ok"}',
                       model=NewsResult, default={"FB": 1}, context="T")
    check("(c) 坏 sentiment_score → None，但其余字段保留",
          r["sentiment_score"] is None and r["sentiment"] == "bullish"
          and r["summary"] == "ok")

    r = parse_llm_json('{"governance_risks":"单一风险"}', model=MeetingResult,
                       default={"FB": 1}, context="T")
    check("(c) governance_risks 标量 → 包成列表",
          r["governance_risks"] == ["单一风险"])

    r = parse_llm_json('{"risk_level":3,"events":[]}', model=GeopoliticalResult,
                       default={"FB": 1}, context="T")
    check("(c) risk_level=3 → \"3\"", r["risk_level"] == "3")

    r = parse_llm_json('[{"name":7,"ticker":"","reason":"r"}]', model=list[Competitor],
                       default=[], context="T")
    check("(c) 竞品列表逐项归一化",
          r[0]["name"] == "7" and r[0]["ticker"] is None)

    r = parse_llm_json('{"matrix":[{"dimension":"份额"}]}', model=ComparisonResult,
                       default={"FB": 1}, context="T")
    check("(c) 对比结果缺项 → 补默认键而非回退",
          r.get("matrix", [{}])[0].get("dimension") == "份额"
          and r.get("competitive_advantage") == "")

    # ── (d) 意图识别三层降级 ──────────────────────────────────
    import app.agent.intent as I

    with mock.patch.object(I, "chat_tool_calls",
                           return_value=[{"name": "parse_investment_intent",
                                          "arguments": '{"intent":"query_price","tickers":["AAPL"]}'}]):
        p = I.parse("苹果现在多少钱")
    check("(d) 第1层 FC 命中", p["intent"] == "query_price" and p["tickers"] == ["AAPL"])

    with mock.patch.object(I, "chat_tool_calls", return_value=None), \
         mock.patch.object(I, "chat", return_value='```json\n{"intent":"analysis","tickers":["NVDA"]}\n```'):
        p = I.parse("英伟达怎么样")
    check("(d) FC 失败 → 第2层 JSON 命中",
          p["intent"] == "analysis" and p["tickers"] == ["NVDA"])

    with mock.patch.object(I, "chat_tool_calls", return_value=None), \
         mock.patch.object(I, "chat", return_value="这不是 JSON"):
        p = I.parse("茅台的历史走势")
    check("(d) 两层皆失败 → 第3层规则兜底",
          p["intent"] == "query_history" and "600519.SS" in p["tickers"],
          f"intent={p['intent']} tickers={p['tickers']}")

    with mock.patch.object(I, "chat_tool_calls",
                           return_value=[{"name": "parse_investment_intent",
                                          "arguments": '{"intent":"buy_everything"}'}]), \
         mock.patch.object(I, "chat", return_value=None):
        p = I.parse("苹果现在多少钱")
    check("(d) FC 越界枚举被拦 → 落到规则层 query_price", p["intent"] == "query_price")

    # ── (e) 分析器接线 ────────────────────────────────────────
    from app.analysis.segment import SegmentAnalyzer
    a = SegmentAnalyzer()
    a._client = FakeClient('{"segments":[{"name":"云","revenue_share":"0.6","net_income_share":"60%"},'
                           '{"name":"硬件","revenue_share":0.4,"net_income_share":0.4}]}')
    segs = a.identify_segments({"name": "测试公司", "industry": "科技"})
    check("(e) Segment 占比归一化后求和≈1",
          abs(sum(s["revenue_share"] for s in segs) - 1.0) < 1e-9,
          f"{[round(s['revenue_share'], 3) for s in segs]}")

    from app.analysis.news import NewsAnalyzer
    b = NewsAnalyzer()
    b._client = FakeClient('```json\n{"sentiment":"偏正面","sentiment_score":"0.42",'
                           '"key_drivers":["订单增长"],"summary":"情绪改善"}\n```')
    out = b.analyze("测试公司", articles=[{"title": "t", "summary": "s"}])
    check("(e) News 围栏响应可解析", out["sentiment"] == "偏正面"
          and out["sentiment_score"] == 0.42 and out["summary"] == "情绪改善")

    from app.analysis.meeting import MeetingAnalyzer
    c = MeetingAnalyzer()
    c._client = FakeClient('{"key_proposals":["议案A"],"governance_risks":["股权稀释"],'
                           '"summary":"治理平稳"}')
    out = c.analyze("测试公司", meetings=[{"name": "年度股东大会", "proposals": "议案A"}])
    check("(e) Meeting 正常解析", out["governance_risks"] == ["股权稀释"]
          and out["summary"] == "治理平稳")

    from app.analysis.swot import SwotAnalyzer
    d = SwotAnalyzer()
    d._client = FakeClient('{"swot":{"strengths":[{"point":"毛利高","source":"10-K"}],'
                           '"weaknesses":[],"opportunities":[],"threats":[]},'
                           '"summary":"稳健","rating":"看好"}')
    out = d.analyze("测试公司")
    check("(e) SWOT 正常解析", out["rating"] == "看好"
          and out["swot"]["strengths"][0]["point"] == "毛利高")

    d._client = FakeClient('模型跑飞了，没返回 JSON')
    out = d.analyze("测试公司")
    # P3：失败不再冒充「中性」评级——评级留空 + 显式降级标记
    check("(e) SWOT 失败 → 空 SWOT 兜底且评级留空（不抛错、不编造评级）",
          out["swot"]["strengths"] == [] and out["rating"] is None
          and out.get("degraded") is True and bool(out.get("reason")))

    # ── (f) 自主深度调查：假设/证据契约 ───────────────────────
    # 注意：parse_llm_json 返回的是 **归一化后的 dict/list**（adapter.dump_python），
    # 不是 pydantic 实例——消费方一律按下标取值。
    from app.llm.schemas import HypothesisSet, RoundPlan, Hypothesis, EvidenceProposal

    # 容器严格：hypotheses 是必需键，缺了就回退 default
    out = parse_llm_json('{"foo": 1}', model=HypothesisSet, default=None,
                         context="测试(假设集)")
    check("(f) 假设集：缺必需键 → 回退 default（容器严格）", out is None)
    out = parse_llm_json('{"hypotheses": []}', model=HypothesisSet, default=None,
                         context="测试(假设集)")
    check("(f) 假设集：空数组仍是合法契约",
          isinstance(out, dict) and out["hypotheses"] == [])

    # 关键：family 用裸 Str 而非 Literal —— 越界族不能让整份结果崩掉
    out = parse_llm_json(
        '{"hypotheses":['
        '{"id":"H1","family":"需求走弱","statement":"渠道去库存","prior_confidence":"high"},'
        '{"id":"H2","family":"模型编出来的不存在的族","statement":"X"}]}',
        model=HypothesisSet, default=None, context="测试(假设集)")
    check("(f) 越界 family 不毁整份结果（刻意不用 Literal 的原因）",
          isinstance(out, dict) and len(out["hypotheses"]) == 2
          and out["hypotheses"][1]["family"] == "模型编出来的不存在的族"
          and out["hypotheses"][0]["prior_confidence"] == "high",
          "" if not isinstance(out, dict) else str(len(out["hypotheses"])))

    # 叶子宽容：int→str、标量→[标量]、list→str 都不抛
    out = parse_llm_json(
        '{"hypotheses":[{"id":7,"statement":["a","b"],"predicted_observables":"单条"}]}',
        model=HypothesisSet, default=None, context="测试(假设集)")
    h = out["hypotheses"][0]
    check("(f) 叶子宽容：id int→str、str→[str]、list→str 均不抛",
          bool(h["id"] == "7" and h["predicted_observables"] == ["单条"]
               and isinstance(h["statement"], str) and h["statement"]),
          f"{h['id']!r} {h['statement']!r} {h['predicted_observables']!r}")

    # 证据指认：索引归一到 int
    out = parse_llm_json(
        '{"actions":[],"attributions":[{"hypothesis_id":"H1","relation":"supports",'
        '"source_ref_index":"2","rationale":"原文提到去库存"}]}',
        model=RoundPlan, default=None, context="测试(轮计划)")
    check("(f) 轮计划：索引字符串归一到 int",
          isinstance(out, dict) and out["attributions"][0]["source_ref_index"] == 2
          and out["attributions"][0]["relation"] == "supports")

    # 索引不可解析 / 是 bool → None（bool 是 int 子类，必须单独挡）
    out = parse_llm_json('{"attributions":[{"source_ref_index":"abc"}]}',
                         model=RoundPlan, default=None, context="测试(轮计划)")
    check("(f) 索引不可解析 → None（不抛，越界留给 loop 判）",
          out["attributions"][0]["source_ref_index"] is None)
    out = parse_llm_json('{"attributions":[{"source_ref_index":true}]}',
                         model=RoundPlan, default=None, context="测试(轮计划)")
    check("(f) 索引为 bool → None（bool 是 int 子类，必须单独挡）",
          out["attributions"][0]["source_ref_index"] is None)

    # actions 是宽容器：形状异常不能崩；空 dict 也是合法契约（= 模型认为可以收尾）
    out = parse_llm_json(
        '{"actions":[{"tool":"get_news","args":{"ticker":"AAPL"}},"裸字符串"]}',
        model=RoundPlan, default=None, context="测试(轮计划)")
    check("(f) actions 宽容器：形状异常不崩",
          isinstance(out, dict) and len(out["actions"]) == 2)
    out = parse_llm_json('{}', model=RoundPlan, default=None, context="测试(轮计划)")
    check("(f) 轮计划：空 dict 合法（无动作=可收尾）",
          isinstance(out, dict) and out["actions"] == [])

    # 整份不可解析 → default（loop 据此以 llm_error 收尾，不静默 promote）
    out = parse_llm_json("模型今天不想返回 JSON", model=RoundPlan, default=None,
                         context="测试(轮计划)")
    check("(f) 整份不可解析 → None", out is None)

    # __all__ 里已登记（导入即可用）
    check("(f) 新模型已导出",
          Hypothesis.__name__ == "Hypothesis" and EvidenceProposal.__name__ == "EvidenceProposal")

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
