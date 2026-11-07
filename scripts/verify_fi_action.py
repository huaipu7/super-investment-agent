# -*- coding: utf-8 -*-
"""Phase 6 验证 —— 建议转行动 + 风险信号 + 合规复核队列（JD 职责③第二部分）。

三部分：
  A. 真实引擎输入：FI.tool_executor('action_plan', {'ticker':'F'}) —— 复用信用/利率/
     政策工具真实输出 → synthesize_action。断言含真实数值（F 的 Z''/O；FOMC 偏鸽 → 久期倾向）。
  B. 分支单元（synthetic，标注非真实数据）：覆盖 灰区 / 风险区+困境 / 鹰派+倒挂 三条规则的
     触发与严重度。
  C. 合规队列机制：apply_gate 命中投资建议词 → 追加免责声明 + queue.flag 持久化；
     验证 list_flagged / mark_reviewed。

运行：D:\\python\\python.exe scripts/verify_fi_action.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

PASS, FAIL = "PASS", "FAIL"
_checks: list[str] = []


def check(name, cond, detail=""):
    _checks.append((f"{PASS if cond else FAIL}  {name}  {detail}").rstrip())
    print(("  [%s] %s %s" % ("OK" if cond else "XX", name, detail)).rstrip())


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("=" * 78)
    print("Phase 6 固收动作化验证  运行时间:", pd.Timestamp.now())
    print("=" * 78)

    from app.action.fixed_income import synthesize_action
    from app.agent import fixed_income as FI

    # ── A. 真实引擎输入 ──────────────────────────────
    print("\n[A] 真实引擎输入 → action_plan(F)")
    out = json.loads(FI.tool_executor("action_plan", {"ticker": "F"}))
    check("action_plan 返回结构完整", out.get("available"), "available=True")
    cz = out.get("credit_view", {})
    check("信用观点(真实 F)", cz.get("tier") in ("安全区", "灰区", "风险区"),
          f"tier={cz.get('tier')} Z''={cz.get('z')} O={cz.get('o')} Merton={cz.get('merton_available')}")
    rv = out.get("rate_view", {})
    check("久期倾向(由真实 FOMC 政策分)", rv.get("duration_tilt") is not None,
          f"policy={rv.get('policy_stance')} score={rv.get('policy_score')} → {rv.get('duration_tilt')}")
    check("风险信号列表非空", len(out.get("risk_signals", [])) > 0,
          f"n={len(out.get('risk_signals', []))}")
    check("合规复核队列非空", len(out.get("compliance_queue", [])) > 0,
          f"n={len(out.get('compliance_queue', []))}")
    check("理由清单 + 行动文本", bool(out.get("reasons")) and bool(out.get("advice_text")),
          f"reasons={len(out.get('reasons', []))}")

    # ── B. 分支单元（synthetic 输入，覆盖规则）────────
    print("\n[B] 分支规则单元（synthetic 输入，非真实数据，仅验证规则）")
    # 灰区：1.10 <= Z'' < 2.60
    p1 = synthesize_action(issuer="SYN", credit={"altman_z": 2.0, "ohlson_o": 0.3,
                                                 "merton": {"available": False, "reason": "x"}})
    check("灰区规则", p1["credit_view"]["tier"] == "灰区"
          and any(r["name"] == "主体信用走弱（灰区）" for r in p1["risk_signals"]),
          f"tier={p1['credit_view']['tier']}")
    # 风险区 + 困境：Z''<1.10 且 O>0.5，Merton 可算 → 无 Merton 数据缺口信号
    p2 = synthesize_action(issuer="SYN", credit={"altman_z": 0.8, "ohlson_o": 0.7,
                                                 "merton": {"available": True, "dd": 8, "pd": 0.0001}})
    names2 = [r["name"] for r in p2["risk_signals"]]
    check("风险区+困境规则", p2["credit_view"]["tier"] == "风险区"
          and "主体违约/结构信用风险" in names2 and "Ohlson 财务困境信号" in names2
          and not any("Merton" in n for n in names2),
          f"tier={p2['credit_view']['tier']} signals={names2}")
    # 鹰派 + 曲线倒挂
    p3 = synthesize_action(issuer="SYN", policy={"score": 0.4, "stance": "偏鹰"},
                           curve={"level": 5.5, "slope": 1.2, "as_of": "2026-08-31"})
    names3 = [r["name"] for r in p3["risk_signals"]]
    check("鹰派+倒挂规则", p3["rate_view"]["duration_tilt"].startswith("缩短久期")
          and p3["rate_view"]["curve_shape"] == "倒挂"
          and any("政策紧缩" in n for n in names3) and "收益率曲线倒挂" in names3,
          f"dur={p3['rate_view']['duration_tilt']} shape={p3['rate_view']['curve_shape']}")

    # ── C. 合规队列机制 ─────────────────────────────
    print("\n[C] 合规复核队列机制")
    from app.compliance import queue as q
    from app.compliance.rules import apply_gate

    # 命中投资建议词 → 追加免责声明 + 落复核队列（source=test_compliance）
    sample = "建议买入该债券，目标价 4.50，止损位 4.80。"
    txt, h, flagged = apply_gate(sample, source="test_compliance")
    check("apply_gate 命中投资建议模式", flagged and "合规提示" in txt and "投资有风险" in txt,
          f"flagged={flagged}")
    try:
        rows = q.list_flagged(reviewed=0, limit=20)
        srcs = {r["source"] for r in rows}
        check("复核队列已持久化 (queue.flag)", "test_compliance" in srcs,
              f"pending entries={sum(1 for r in rows if r['reviewed'] == 0)}")
        # 标记一条已复核
        tid = next((r["id"] for r in rows if r["source"] == "test_compliance"), None)
        if tid is not None:
            ok = q.mark_reviewed(tid)
            check("mark_reviewed 可标记", ok, f"id={tid}")
    except Exception as e:
        check("复核队列已持久化 (queue.flag)", False, f"{type(e).__name__}: {e}")

    # 收尾
    print("\n" + "=" * 78)
    total = len(_checks)
    npass = sum(1 for c in _checks if c.startswith(PASS))
    print(f"结果：{npass}/{total} PASS")
    for c in _checks:
        if c.startswith(FAIL):
            print("  未通过： " + c)
    return 0 if npass == total else 1


if __name__ == "__main__":
    sys.exit(main())
