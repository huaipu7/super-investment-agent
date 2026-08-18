"""Phase A3 验证：输出合规闸门（检测/免责声明/复核队列/expressor 接入）。

运行: D:\\python\\python.exe scripts\\verify_compliance.py
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


def main():
    tmp = Path(tempfile.mkdtemp(prefix="verify_comp_"))
    comp_db = tmp / "compliance.db"

    class _TmpSettings:
        compliance_db = comp_db

    with mock.patch("app.compliance.queue.get_settings", lambda: _TmpSettings()):
        from app.compliance import queue
        from app.compliance.rules import (
            COMPLIANCE_DISCLAIMER, apply_gate, check_advice,
        )

        queue.init_db()

        # ── 1. 规则检测 ──
        flagged, hits = check_advice("当前建议买入并持有，目标价 100 元。")
        check("命中建议买入", flagged and "建议买入" in hits, str(hits))
        check("命中目标价", "目标价" in hits)

        flagged2, hits2 = check_advice("公司发布财报，营收同比增长 20%，毛利率改善。")
        check("无害文本不命中", not flagged2, str(hits2))

        # ── 2. apply_gate：命中 → 免责声明 + 落队列 ──
        text, h, fl = apply_gate("建议买入该股", source="test")
        check("apply_gate 命中标记", fl)
        check("免责声明已追加", text.endswith(COMPLIANCE_DISCLAIMER))
        rows = queue.list_flagged()
        check("队列落库 1 条", len(rows) == 1)
        r0 = rows[0]
        check("队列字段正确", r0["advice_flag"] == 1 and r0["reviewed"] == 0
              and "建议买入" in r0["hit_words"] and r0["source"] == "test")

        # ── 3. apply_gate：未命中 → 不改写、不落库 ──
        clean = "公司发布财报，营收同比增长 20%。"
        text2, h2, fl2 = apply_gate(clean, source="test")
        check("未命中不追加", not fl2 and text2 == clean)
        check("未命中不落库", len(queue.list_flagged()) == 1)

        # ── 4. 复核流 ──
        ok = queue.mark_reviewed(r0["id"])
        check("标记已审", ok and len(queue.list_flagged(reviewed=1)) == 1)

        # ── 5. expressor 端到端（query_price 分支，离线不调 LLM）──
        from app.agent.expressor import generate_report

        execution = {
            "results": [{
                "step": {"action": "get_quote", "description": "报价",
                         "params": {"ticker": "TEST"}},
                "data": {"ticker": "TEST", "company_name": "建议买入测试股",
                         "current_price": 10.0, "currency": "USD",
                         "change_percent": 0.5},
                "error": None,
            }],
            "summary": {"total": 1, "success": 1},
        }
        intent = {"intent": "query_price", "tickers": ["TEST"]}
        report = generate_report(execution, intent)
        check("报告含免责声明", COMPLIANCE_DISCLAIMER in report)
        chat_rows = [x for x in queue.list_flagged() if x["source"] == "chat_report"]
        check("chat_report 已落队列", len(chat_rows) >= 1)

        # ── 6. 干净文本走 expressor 不触发 ──
        execution2 = {
            "results": [{
                "step": {"action": "get_quote", "description": "报价",
                         "params": {"ticker": "TEST"}},
                "data": {"ticker": "TEST", "company_name": "测试股份",
                         "current_price": 10.0, "currency": "USD",
                         "change_percent": 0.5},
                "error": None,
            }],
            "summary": {"total": 1, "success": 1},
        }
        report2 = generate_report(execution2, intent)
        check("干净报告无免责声明", COMPLIANCE_DISCLAIMER not in report2)

        check("compliance.db 文件存在", comp_db.exists())

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
