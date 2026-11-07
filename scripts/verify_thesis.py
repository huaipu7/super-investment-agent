"""A1 验证：论点台账（存储 / 校验 / 证伪判定）。

运行: D:\\python\\python.exe scripts\\verify_thesis.py
全离线，用临时库；不联网、不调 LLM、不写项目内的 data/。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _raises(fn, exc=ValueError) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001 抛了别的异常也算没按契约失败
        return False
    return False


def main():
    tmp = Path(tempfile.mkdtemp(prefix="verify_thesis_"))

    class _TmpSettings:
        thesis_db = tmp / "thesis.db"

    from app.thesis import db as tdb
    from app.thesis import checks
    from app.trust.ledger import build_ledger

    with mock.patch("app.thesis.db.get_settings", lambda: _TmpSettings()):

        # ── 1. 建表幂等 ──
        tdb.init_db()
        tdb.init_db()
        check("init_db 幂等", tdb.count_theses() == 0)

        # ── 2. 创建 / 读回 / JSON 字段往返 ──
        tid = tdb.create_thesis(
            statement="茅台提价周期未结束，2026 年营收增速仍可维持两位数",
            ticker="600519.SS", stance="bullish", horizon="12-18 个月",
            key_assumptions=["飞天出厂价不再上调", "经销商库存低于 1 个月"],
            falsifiers=[
                {"metric": "valuation_analysis.fair_value", "op": "<", "value": 1200.0},
                {"metric": "financial_analysis.ratios.GROSS_MARGIN.value",
                 "op": "<", "value": 0.85, "note": "毛利率跌破 85%"},
            ],
            conviction=4, artifact_ref="data/artifacts/600519.SS/x.json")
        check("create_thesis 返回 id", isinstance(tid, str) and len(tid) == 32, tid[:8])

        t = tdb.get_thesis(tid)
        check("get_thesis 命中", t is not None and t["ticker"] == "600519.SS")
        check("假设往返为列表", t["key_assumptions"] == ["飞天出厂价不再上调", "经销商库存低于 1 个月"])
        check("证伪条件往返为对象列表",
              len(t["falsifiers"]) == 2
              and t["falsifiers"][1]["op"] == "<"
              and t["falsifiers"][1]["value"] == 0.85
              and t["falsifiers"][1]["note"] == "毛利率跌破 85%")
        check("默认 status=active", t["status"] == "active")

        # ── 3. 字段校验 ──
        check("空 statement 拒绝",
              _raises(lambda: tdb.create_thesis(statement="  ")))
        check("非法 stance 拒绝",
              _raises(lambda: tdb.create_thesis(statement="x", stance="long")))
        check("非法 conviction 拒绝",
              _raises(lambda: tdb.create_thesis(statement="x", conviction=9)))
        check("非法比较符拒绝",
              _raises(lambda: tdb.create_thesis(
                  statement="x", falsifiers=[{"metric": "a", "op": "≈", "value": 1}])))
        check("缺 metric 拒绝",
              _raises(lambda: tdb.create_thesis(
                  statement="x", falsifiers=[{"op": ">", "value": 1}])))
        check("非数字 value 拒绝",
              _raises(lambda: tdb.create_thesis(
                  statement="x", falsifiers=[{"metric": "a", "op": ">", "value": "高"}])))

        # ── 4. 更新白名单 ──
        check("update 生效",
              tdb.update_thesis(tid, statement="改后", status="confirmed")
              and tdb.get_thesis(tid)["statement"] == "改后"
              and tdb.get_thesis(tid)["status"] == "confirmed")
        check("update 拒绝非白名单字段", tdb.update_thesis(tid, id="hack") is False)
        check("update 空调用返回 False", tdb.update_thesis(tid) is False)
        check("update 非法 status 拒绝",
              _raises(lambda: tdb.update_thesis(tid, status="done")))
        check("updated_at 变化",
              tdb.get_thesis(tid)["updated_at"] >= tdb.get_thesis(tid)["created_at"])

        # ── 5. 列表 / 过滤 ──
        tdb.create_thesis(statement="AAPL 服务业务毛利撑住估值", ticker="AAPL",
                          stance="neutral")
        check("list 全部", len(tdb.list_theses()) == 2)
        check("list 按 ticker 过滤",
              len(tdb.list_theses(ticker="aapl")) == 1
              and tdb.list_theses(ticker="aapl")[0]["ticker"] == "AAPL")
        check("list 按 status 过滤", len(tdb.list_theses(status="confirmed")) == 1)

        # ── 6. 证据 ──
        eid = tdb.add_evidence(tid, "valuation_analysis.fair_value",
                               kind="fact", direction="supports", note="当前 1500")
        tdb.add_evidence(tid, "https://example.com/a", kind="doc",
                         direction="contradicts", note="渠道调研")
        ev = tdb.list_evidence(tid)
        check("证据落库 2 条", len(ev) == 2 and ev[0]["id"] == eid)
        check("证据方向正确", [e["direction"] for e in ev] == ["supports", "contradicts"])
        check("证据非法 kind 拒绝",
              _raises(lambda: tdb.add_evidence(tid, "x", kind="rumor")))
        check("证据挂不存在的论点拒绝",
              _raises(lambda: tdb.add_evidence("nope", "x")))
        check("删除证据", tdb.delete_evidence(eid) and len(tdb.list_evidence(tid)) == 1)

        # ── 7. 复盘 ──
        tdb.add_review(tid, "weakens", snapshot={"fair_value": 1500.0}, note="估值已反映")
        rv = tdb.list_reviews(tid)
        check("复盘落库", len(rv) == 1 and rv[0]["verdict"] == "weakens")
        check("复盘快照往返", rv[0]["snapshot"] == {"fair_value": 1500.0})
        check("非法 verdict 拒绝",
              _raises(lambda: tdb.add_review(tid, "great")))

        # ── 8. 证伪判定 ──
        ledger = build_ledger(
            {
                "valuation_analysis": {"fair_value": 1500.0, "upside": 0.2},
                "trade_plan": {"price": float("nan")},
                "unknown_section": {"weird_metric": 1.0},
            },
            ticker="TEST",
        )
        check("手工台账 facts > 0", ledger.total > 0, f"{ledger.total} 条")

        def state(spec):
            return checks.evaluate_falsifier(spec, ledger).state

        r_hold = checks.evaluate_falsifier(
            {"metric": "valuation_analysis.fair_value", "op": "<", "value": 1200.0}, ledger)
        check("未命中 → holding", r_hold.state == "holding" and r_hold.observed == 1500.0)
        check("holding 带回出处", r_hold.provenance == "computed" and r_hold.unit == "currency")

        r_breach = checks.evaluate_falsifier(
            {"metric": "valuation_analysis.fair_value", "op": ">", "value": 1200.0}, ledger)
        check("命中 → breached", r_breach.state == "breached" and r_breach.observed == 1500.0)

        check(">= 边界",
              state({"metric": "valuation_analysis.fair_value", "op": ">=", "value": 1500.0})
              == "breached")
        check("== 边界",
              state({"metric": "valuation_analysis.fair_value", "op": "==", "value": 1500.0})
              == "breached")
        check("!= 边界",
              state({"metric": "valuation_analysis.fair_value", "op": "!=", "value": 1500.0})
              == "holding")

        r_missing = checks.evaluate_falsifier(
            {"metric": "valuation_analysis.does_not_exist", "op": "<", "value": 1}, ledger)
        check("路径不存在 → unknown",
              r_missing.state == "unknown" and r_missing.reason == "path_not_in_ledger")

        r_nan = checks.evaluate_falsifier(
            {"metric": "trade_plan.price", "op": "<", "value": 1}, ledger)
        check("NaN → unknown（不当成未触发）",
              r_nan.state == "unknown" and r_nan.reason == "non_finite")

        r_unk = checks.evaluate_falsifier(
            {"metric": "unknown_section.weird_metric", "op": ">", "value": 0}, ledger)
        check("台账 UNKNOWN → unknown",
              r_unk.state == "unknown" and r_unk.reason == "ledger_unknown")

        r_nole = checks.evaluate_falsifier(
            {"metric": "valuation_analysis.fair_value", "op": "<", "value": 1}, None)
        check("无台账 → unknown",
              r_nole.state == "unknown" and r_nole.reason == "no_ledger")

        # ── 9. 总体结论 ──
        v_falsified = checks.evaluate(
            {"id": "t1", "falsifiers": [
                {"metric": "valuation_analysis.fair_value", "op": ">", "value": 1200.0}]},
            ledger)
        check("有命中 → falsified",
              v_falsified["verdict"] == "falsified"
              and v_falsified["counts"]["breached"] == 1)

        v_hold = checks.evaluate(
            {"id": "t2", "falsifiers": [
                {"metric": "valuation_analysis.fair_value", "op": "<", "value": 1200.0}]},
            ledger)
        check("全未命中 → holding", v_hold["verdict"] == "holding")

        v_inc = checks.evaluate(
            {"id": "t3", "falsifiers": [
                {"metric": "valuation_analysis.fair_value", "op": "<", "value": 1200.0},
                {"metric": "nope.path", "op": "<", "value": 1}]},
            ledger)
        check("存在取不到值 → inconclusive（不报 holding）",
              v_inc["verdict"] == "inconclusive"
              and v_inc["reason"] == "unknown_falsifier"
              and v_inc["counts"]["unknown"] == 1)

        v_none = checks.evaluate({"id": "t4", "falsifiers": []}, ledger)
        check("没写证伪条件 → inconclusive",
              v_none["verdict"] == "inconclusive" and v_none["reason"] == "no_falsifiers")

        # ── 10. 从真实产物重建台账 ──
        real = sorted((ROOT / "data" / "artifacts").glob("*/*.json"))
        if real:
            art = real[-1]
            lg = checks.load_ledger(art)
            check("真实产物可重建台账", lg is not None and lg.total > 0,
                  f"{art.parent.name}/{art.name} → {lg.total if lg else 0} 条")
            # 真实产物里 DCF 的公平价值在 valuation_analysis.dcf.fair_value
            fv = lg.find("valuation_analysis.dcf.fair_value") if lg else None
            if fv is not None:
                res = checks.evaluate_falsifier(
                    {"metric": "valuation_analysis.dcf.fair_value",
                     "op": ">", "value": 0}, lg)
                check("真实产物上的判定不回退成 unknown",
                      res.state in ("breached", "holding"),
                      f"{res.state} 观测值={res.observed}")
            else:
                check("真实产物含 dcf.fair_value", False,
                      f"可用路径示例：{[f.path for f in lg.facts[:3]] if lg else []}")
        else:
            check("真实产物存在", False, "data/artifacts 为空")

        check("坏文件 → None",
              checks.load_ledger(tmp / "nope.json") is None)

        # ── 11. 删除级联 ──
        check("delete 返回 True", tdb.delete_thesis(tid))
        check("级联删除证据", len(tdb.list_evidence(tid)) == 0)
        check("级联删除复盘", len(tdb.list_reviews(tid)) == 0)
        check("删除后读不到", tdb.get_thesis(tid) is None)
        check("重复删除返回 False", tdb.delete_thesis(tid) is False)

        check("thesis.db 文件存在", (tmp / "thesis.db").exists())

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
