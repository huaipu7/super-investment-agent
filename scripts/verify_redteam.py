"""B3 验证：红队（没有引用就不许说话）。

Part A —— 引用池：只收「真能核对」的事实；未采信证据不进池。
Part B —— 校验：引用对不上 / 没引用 → 整条丢弃并留档，绝不留进产物。
Part C —— 不够就说不够：存活条数 < 3 时 status=insufficient_evidence，不凑数。
Part D —— 模型不可用：反方论证留空，确定性部分（脆弱假设/未解问题）照常给出。
Part E —— 防止退化：任何状态下 validate_redteam 都不许出现「无引用的反方论证」。
Part F —— L3 断言接线：无 redteam 直接通过；有坏 redteam 必须失败。

运行: D:\\python\\python.exe scripts/verify_redteam.py
（全离线：模型用 ask 桩替换，不联网、不调 LLM）
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

passed = failed = 0


def check(name: str, ok, detail: str = ""):
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _run_fixture() -> dict:
    """一份调查 run：两条可采信证据 + 一条未采信 + 两个假设。"""
    ev = [
        {"id": "E1", "hypothesis_id": "H1", "source_kind": "financial_statement",
         "attribution_valid": True, "source_ref": "现金流量表", "snippet": "经营现金流 +12%",
         "relation": "support"},
        {"id": "E2", "hypothesis_id": "H2", "source_kind": "news",
         "attribution_valid": True, "source_ref": "https://example.com/a", "snippet": "渠道库存上升",
         "relation": "contradict"},
        {"id": "E3", "hypothesis_id": "H1", "source_kind": "news",
         "attribution_valid": False, "source_ref": "编的出处", "snippet": "看起来像真的",
         "relation": "support"},
    ]
    hyp = [
        {"id": "H1", "statement": "提价周期支撑毛利率", "status": "verified",
         "support_count": 1, "contradict_count": 0, "evidence": [ev[0], ev[2]]},
        {"id": "H2", "statement": "渠道库存下滑", "status": "unverified",
         "support_count": 0, "contradict_count": 1, "evidence": [ev[1]]},
    ]
    return {"ticker": "600519.SS", "depth": "deep", "hypotheses": hyp, "evidence": ev}


def _ask(payload: dict):
    return lambda user, system: json.dumps(payload, ensure_ascii=False)


def main():
    from app.agent import redteam as R

    run = _run_fixture()

    # ── Part A：引用池 ───────────────────────────────────
    print("\n=== Part A: 引用池只收能核对的事实 ===")
    pool = R.ref_pool(run)
    check("调查形状：采信证据进池", set(pool) == {"E1", "E2"}, str(sorted(pool)))
    check("未采信证据不进池", "E3" not in pool)
    check("池内带来源与片段",
          pool["E1"]["value"] == "经营现金流 +12%" and pool["E1"]["source_ref"] == "现金流量表")

    arts = sorted((ROOT / "data" / "artifacts").glob("*/*.json"))
    if arts:
        art = json.loads(arts[-1].read_text(encoding="utf-8"))
        apool = R.ref_pool(art)
        check("产物形状：台账全部可取数的路径进池", len(apool) > 100, f"{len(apool)} 条")
        check("产物池里没有取不到值的路径",
              all(v.get("value") is not None for v in apool.values()))
        check("产物池里有 DCF 公平价值", "valuation_analysis.dcf.fair_value" in apool,
              str(list(apool)[:3]))
    else:
        art = {"ticker": "X", "valuation_analysis": {"dcf": {"fair_value": 100.0}}}
        check("产物形状：台账路径进池", "valuation_analysis.dcf.fair_value" in R.ref_pool(art))

    # ── Part B：引用校验 ─────────────────────────────────
    print("\n=== Part B: 引用对不上就丢弃 ===")
    payload = {
        "counter_arguments": [
            {"point": "经营现金流改善可能来自营运资本释放，不可持续", "evidence_ref": "E1",
             "why": "现金流与利润增速不匹配"},
            {"point": "渠道库存上升与结论矛盾", "evidence_ref": "E2", "why": "反证已存在"},
            {"point": "这个观点我编的", "evidence_ref": "E3", "why": "未采信证据"},
            {"point": "这个连引用都没有", "why": "空口"},
            {"point": "引用指向不存在的 id", "evidence_ref": "E99", "why": "编的"},
        ],
        "fragile_assumption": {"assumption": "提价能持续", "evidence_ref": "E1",
                               "why": "只有一条证据"},
        "open_questions": ["竞争对手的提价节奏不清楚"],
    }
    rt = R.build_redteam(run, ask=_ask(payload))
    check("留下引用站得住的 2 条", len(rt["counter_arguments"]) == 2,
          str([c["evidence_ref"] for c in rt["counter_arguments"]]))
    check("丢弃的 3 条全部留档并写明原因",
          len(rt["dropped"]) == 3 and all(d["reason"] for d in rt["dropped"]),
          str(rt["dropped"]))
    check("丢弃项带得出可读原因",
          any("没有引用" in d["reason"] for d in rt["dropped"])
          and any("E99" in d["reason"] for d in rt["dropped"]),
          str([d["reason"] for d in rt["dropped"]]))
    check("被采纳的条目标注了证据实体",
          rt["counter_arguments"][0]["evidence"]["source_ref"] == "现金流量表")
    check("不够 3 条 → insufficient_evidence", rt["status"] == R.STATUS_INSUFFICIENT,
          rt["status"])
    check("说明里写清几条 / 要求几条", "2/3" in rt["note"], rt["note"])
    check("模型给的脆弱假设被采纳", rt["fragile_assumption"]["evidence_ref"] == "E1")

    # ── Part C：够数才叫 ok ──────────────────────────────
    print("\n=== Part C: 够 3 条才 status=ok ===")
    full = {"counter_arguments": [
        {"point": "现金流改善来自营运资本", "evidence_ref": "E1"},
        {"point": "库存反证已被记录", "evidence_ref": "E2"},
        {"point": "单一证据不足以支撑提价周期", "evidence_ref": "E1"},
    ], "open_questions": []}
    rt = R.build_redteam(run, ask=_ask(full))
    check("3 条全部有效 → ok", rt["status"] == R.STATUS_OK and len(rt["counter_arguments"]) == 3,
          rt["status"])
    only_one = {**full, "counter_arguments": [full["counter_arguments"][0]]}
    rt2 = R.build_redteam(run, ask=_ask(only_one))
    check("只给 1 条 → 仍是 insufficient",
          rt2["status"] == R.STATUS_INSUFFICIENT and len(rt2["counter_arguments"]) == 1,
          rt2["status"])

    # ── Part D：模型不可用 ───────────────────────────────
    print("\n=== Part D: 模型不可用时不编 ===")

    def boom(user, system):
        raise RuntimeError("LLM 挂了")

    rt = R.build_redteam(run, ask=boom)
    check("status=llm_unavailable", rt["status"] == R.STATUS_NO_LLM, rt["status"])
    check("反方论证为空（不拿推测充当）", rt["counter_arguments"] == [])
    check("未解决问题仍来自真实数据",
          any("渠道库存下滑" in q for q in rt["open_questions"]), str(rt["open_questions"]))
    check("说明了为什么空", "不可用" in rt["note"] or "未返回" in rt["note"], rt["note"])
    check("确定性脆弱假设仍给出（已验假设里证据最少的那条）",
          (rt["fragile_assumption"] or {}).get("evidence_ref") == "E1",
          str(rt["fragile_assumption"]))

    rt = R.build_redteam({"ticker": "X"}, ask=_ask(full))
    check("没有任何事实池 → 不调模型、直接说无从谈起",
          rt["counter_arguments"] == [] and "无从谈起" in rt["note"], rt["note"])

    # ── Part E：防退化 ───────────────────────────────────
    print("\n=== Part E: 任何状态都不许出现无引用的反方论证 ===")
    for label, r in (("ok", R.build_redteam(run, ask=_ask(full))),
                     ("insufficient", R.build_redteam(run, ask=_ask(payload))),
                     ("llm_unavailable", R.build_redteam(run, ask=boom))):
        probs = R.validate_redteam(r, R.ref_pool(run))
        check(f"{label}: validate 无违规", probs == [], str(probs))
        check(f"{label}: 每条反方论证都有引用",
              all(c.get("evidence_ref") for c in r["counter_arguments"]))

    check("validate 抓得住缺引用",
          any("没有 evidence_ref" in p for p in R.validate_redteam(
              {"status": "insufficient_evidence", "open_questions": [],
               "counter_arguments": [{"point": "空口"}]}, {})))
    check("validate 抓得住引用不在池",
          any("不在事实池" in p for p in R.validate_redteam(
              {"status": "insufficient_evidence", "open_questions": [],
               "counter_arguments": [{"point": "x", "evidence_ref": "E99"}]}, {"E1": {}})))
    check("validate 抓得住 status=ok 却条数不够",
          any("status=ok" in p for p in R.validate_redteam(
              {"status": "ok", "min_counter": 3, "open_questions": [],
               "counter_arguments": [{"point": "x", "evidence_ref": "E1"}]}, {"E1": {}})))
    check("validate(None) 通过（默认不触发）", R.validate_redteam(None) == [])

    # ── Part F：L3 断言接线 ──────────────────────────────
    print("\n=== Part F: L3 断言接线 ===")
    sys.path.insert(0, str(ROOT / "scripts"))
    from eval_l3_generation import _check_redteam

    ok, detail = _check_redteam({"ticker": "X"})
    check("无 redteam → 直接通过", ok and "无红队段落" in detail, detail)

    art_rt = json.loads(arts[-1].read_text(encoding="utf-8")) if arts else {}
    if art_rt:
        good_pool_path = sorted(R.ref_pool(art_rt))[0]
        art_rt["redteam"] = {
            "status": "insufficient_evidence", "min_counter": 3,
            "counter_arguments": [{"point": "反方", "evidence_ref": good_pool_path}],
            "fragile_assumption": None, "open_questions": [],
            "dropped": [], "note": "",
        }
        ok, detail = _check_redteam(art_rt)
        check("引用能在本产物取到 → 通过", ok, detail)

        art_rt["redteam"]["counter_arguments"].append(
            {"point": "挂一条别处来的引用", "evidence_ref": "valuation_analysis.不存在的路径"})
        ok, detail = _check_redteam(art_rt)
        check("引用取不到 → L3 必须失败", not ok and "不在事实池" in detail, detail)

        art_rt["redteam"]["counter_arguments"] = [{"point": "空口"}]
        ok, detail = _check_redteam(art_rt)
        check("缺 evidence_ref → L3 必须失败", not ok and "没有 evidence_ref" in detail, detail)

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
