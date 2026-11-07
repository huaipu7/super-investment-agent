"""P1 验收：事实台账地基（离线，零 LLM 调用）。

覆盖：
  1. 对真实产物 data/phase5_full_aapl.json 生成台账，数值叶子数对得上
  2. fact_id 唯一且连续；path 唯一
  3. 构建确定性（两次构建哈希一致）
  4. 未归类路径清单（UNKNOWN）—— 台账的门禁：应为 0
  5. 标记传播：degraded / imputed / available=False 向下覆盖子孙数值
  6. 盲区自证：未知 section 的新字段必须落为 UNKNOWN（证明门禁有效，不是摆设）
  7. as_of 抽取：带 period 的指标条目能取到期间

运行: D:\\python\\python.exe scripts\\verify_trust_ledger.py
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

from app.trust import DEGRADED, IMPUTED, UNKNOWN, build_ledger  # noqa: E402
from app.trust.ledger import Ledger, content_hash  # noqa: E402

ARTIFACT = ROOT / "data" / "phase5_full_aapl.json"

_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def count_numeric_leaves(node) -> int:
    """独立实现（不复用 ledger 的遍历），用来交叉验证台账完整性。"""
    if isinstance(node, bool):
        return 0
    if isinstance(node, (int, float)):
        return 1
    if isinstance(node, dict):
        return sum(count_numeric_leaves(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return sum(count_numeric_leaves(v) for v in node)
    return 0


def main() -> None:
    print("=" * 60)
    print("P1 事实台账地基验收")
    print("=" * 60)

    if not ARTIFACT.exists():
        print(f"[FAIL] 缺少真实产物: {ARTIFACT}")
        sys.exit(1)

    result = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    ledger = build_ledger(result, ticker=result.get("ticker"), model="(artifact)")

    # ── 1. 完整性 ──
    print("\n[1] 台账完整性")
    expected = count_numeric_leaves(result)
    check("数值叶子数与台账条数一致",
          ledger.total == expected, f"台账={ledger.total} 独立计数={expected}")
    check("台账非空", ledger.total > 0)

    # ── 2. 唯一性 ──
    print("\n[2] 标识唯一性")
    ids = [f.fact_id for f in ledger.facts]
    check("fact_id 唯一", len(ids) == len(set(ids)))
    check("fact_id 连续编号",
          ids == [f"F{i:03d}" for i in range(1, len(ids) + 1)])
    paths = [f.path for f in ledger.facts]
    check("path 唯一", len(paths) == len(set(paths)))

    # ── 3. 确定性 ──
    print("\n[3] 构建确定性")
    again = build_ledger(result, ticker=result.get("ticker"), model="(artifact)")
    check("两次构建 fact 序列一致",
          [f.to_dict() for f in again.facts] == [f.to_dict() for f in ledger.facts])
    check("content_hash 稳定", content_hash(result) == ledger.artifact_hash)
    check("run_id 由内容派生", ledger.run_id == ledger.artifact_hash[:12])
    check("git_sha 可获取或诚实为 None", ledger.git_sha is None or len(ledger.git_sha) >= 7)

    # ── 4. 未归类门禁（核心）──
    print("\n[4] 未归类路径（UNKNOWN 必须为 0）")
    unknown = ledger.unknown()
    for f in unknown:
        print(f"  ! UNKNOWN  {f.path}")
    check("无 UNKNOWN 路径", not unknown, f"共 {len(unknown)} 条未归类")

    # ── 5. 出处分布 ──
    print("\n[5] 出处分布")
    counts = ledger.by_provenance()
    for name, n in counts.items():
        print(f"  {name:<10} {n:>4}  ({n / ledger.total:.1%})")
    check("computed 与 source 均存在", "computed" in counts and "source" in counts)

    # ── 6. 标记传播 ──
    print("\n[6] 标记传播（degraded / imputed / available=False）")
    deg = build_ledger({"swot_analysis": {"degraded": True, "confidence": 0.0,
                                          "swot": {"strengths": [{"weight": 3}]}}})
    check("degraded 向下覆盖嵌套数值",
          all(f.provenance == DEGRADED for f in deg.facts), str(deg.by_provenance()))

    imp = build_ledger({"financial_analysis": {"imputed": True,
                                               "ratios": {"ROE": [{"value": 0.3}]}}})
    check("imputed 向下覆盖",
          all(f.provenance == IMPUTED for f in imp.facts), str(imp.by_provenance()))

    unavail = build_ledger({"news_analysis": {"available": False, "sentiment_score": 0.5}})
    check("available=False 视为降级",
          unavail.facts and unavail.facts[0].provenance == DEGRADED)

    # ── 7. 盲区自证 ──
    print("\n[7] 盲区自证（新字段必须落为 UNKNOWN）")
    blind = build_ledger({"brand_new_section": {"some_metric": 1.23}})
    check("未知 section 的新数值 → UNKNOWN",
          blind.facts and blind.facts[0].provenance == UNKNOWN)
    blind2 = build_ledger({"valuation_analysis": {"unknown_margin": 0.42}})
    check("已知 section 下的新字段仍被 section 规则吸收（设计如此）",
          blind2.facts and blind2.facts[0].provenance == "computed")

    # ── 8. as_of 与单位 ──
    print("\n[8] 期间与单位")
    roe = next((f for f in ledger.facts if f.metric == "ROE"), None)
    check("ROE 事实存在", roe is not None)
    if roe:
        check("ROE 取到 as_of 期间", roe.as_of is not None, str(roe.as_of))
        check("ROE 单位=ratio", roe.unit == "ratio", str(roe.unit))
    mc = next((f for f in ledger.facts if f.path == "company_info.market_cap"), None)
    if mc:
        check("市值出处=source", mc.provenance == "source", mc.provenance)
        check("市值单位=currency", mc.unit == "currency", mc.unit)
    share = next((f for f in ledger.facts
                  if f.path.endswith("competitor_data[0].share")), None)
    if share:
        check("竞品份额出处=computed", share.provenance == "computed", share.provenance)

    # 具体规则优先于通用通配：情景概率是代码常量，不是模型产出
    prob = next((f for f in ledger.facts
                 if f.path.startswith("supply_chain_analysis") and f.metric == "probability"),
                None)
    if prob:
        check("供链情景概率=computed（非 llm，未被 *.probability 通配吞掉）",
              prob.provenance == "computed", prob.provenance)
    seg = next((f for f in ledger.facts if f.path.endswith("revenue_share")), None)
    if seg:
        check("分部营收占比=llm", seg.provenance == "llm", seg.provenance)

    # ── 9. 序列化往返 ──
    print("\n[9] 序列化往返")
    out = ledger.to_json(ROOT / "data" / "eval" / "_ledger_selftest.json")
    back = Ledger.load(out)
    check("JSON 往返后事实数一致", back.total == ledger.total)
    check("JSON 往返后哈希一致", back.artifact_hash == ledger.artifact_hash)
    out.unlink(missing_ok=True)

    # ── 10. 列式序列与降级原因 ──
    print("\n[10] 列式序列与降级原因")
    col = build_ledger({"price_history": {
        "available": True,
        "dates": ["2024-01-02", "2024-01-03"],
        "close": [10.0, 11.0],
        "volume": [100, 200],
    }})
    closes = {f.path: f.as_of for f in col.facts if ".close" in f.path}
    check("列式序列逐行取到日期", closes == {"price_history.close[0]": "2024-01-02",
                                            "price_history.close[1]": "2024-01-03"},
          str(closes))
    # 日期轴比数据列短时不能错配日期（宁可留空，不给错日期）
    short = build_ledger({"price_history": {
        "dates": ["2024-01-02"], "close": [10.0, 11.0, 12.0]}})
    short_asof = {f.path: f.as_of for f in short.facts if ".close" in f.path}
    check("日期轴长度不足时后续元素留空而非错配",
          short_asof.get("price_history.close[0]") == "2024-01-02"
          and short_asof.get("price_history.close[1]") is None
          and short_asof.get("price_history.close[2]") is None,
          str(short_asof))

    deg_reason = build_ledger({"news_analysis": {
        "available": False, "reason": "未获取到新闻/公告", "article_count": 0}})
    fact = deg_reason.facts[0]
    check("降级事实携带模块自报原因",
          fact.provenance == DEGRADED and fact.evidence_ref == "degraded:未获取到新闻/公告",
          str(fact.evidence_ref))

    # ── 汇总 ──
    print("\n" + "=" * 60)
    if _failed:
        print(f"结果: {_passed} 通过, {len(_failed)} 失败")
        for f in _failed:
            print(f"  ✗ {f}")
        sys.exit(1)
    print(f"结果: {_passed}/{_passed} 全部通过")
    print(f"台账规模: {ledger.total} 条事实 @ run_id={ledger.run_id} "
          f"git={ledger.git_sha}")
    print("=" * 60)


if __name__ == "__main__":
    main()
