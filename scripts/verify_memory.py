"""Phase C1 验证：长时记忆（对话落库 / /history 读库 / profile / 滚动摘要）。

运行: D:\\python\\python.exe scripts\\verify_memory.py
（TestClient 段 mock 掉 LLM 意图/规划/报告链，全离线）
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
    tmp = Path(tempfile.mkdtemp(prefix="verify_mem_"))

    class _TmpSettings:
        memory_db = tmp / "memory.db"

    from app.memory import db as memory_db

    # ── 1. 数据层：消息 / intent 补记 / 画像 / 会话摘要 ──
    with mock.patch("app.memory.db.get_settings", lambda: _TmpSettings()):
        memory_db.init_db()
        memory_db.add_message("user", "查苹果股价")
        memory_db.add_message("assistant", "AAPL 当前 180.0")
        memory_db.tag_last_user_message("query_price", ["AAPL"])

        rows = memory_db.recent_messages(50)
        check("消息落库 ≥2", len(rows) >= 2, f"{len(rows)} 条")
        check("历史正序", rows[0]["role"] == "user" and rows[1]["role"] == "assistant")
        check("intent 补记", rows[0]["intent"] == "query_price" and rows[0]["tickers"] == ["AAPL"],
              str(rows[0]))

        memory_db.set_profile("risk_preference", "稳健")
        memory_db.set_profile("holdings", "AAPL,600519")
        profiles = memory_db.get_all_profiles()
        check("画像写入/读回",
              profiles.get("risk_preference") == "稳健"
              and profiles.get("holdings") == "AAPL,600519")

        memory_db.update_session("会话摘要A")
        check("会话摘要写入", memory_db.latest_session_summary() == "会话摘要A")

        # ── 2. TestClient：发 3 条对话 → 落库；/history 读库一致；/clear 清库 ──
        memory_db.clear_messages()

        from fastapi.testclient import TestClient
        from api.main import app

        intent = {"intent": "query_price", "tickers": ["AAPL"], "timeframe": None,
                  "analysis_type": None, "raw_input": "x"}
        plan = {"steps": [{"action": "get_quote", "params": {"ticker": "AAPL"},
                           "description": "报价"}]}
        exec_ = {"results": [{"step": plan["steps"][0],
                              "data": {"ticker": "AAPL", "current_price": 180.0},
                              "error": None}],
                 "summary": {"total": 1, "success": 1}}

        with mock.patch("api.routes_chat.parse_intent", return_value=intent), \
                mock.patch("api.routes_chat.build_plan", return_value=plan), \
                mock.patch("api.routes_chat.execute_plan", return_value=exec_), \
                mock.patch("api.routes_chat.generate_report",
                           return_value="测试回复报告"), \
                mock.patch("api.routes_chat.check_ambiguity_from_input", return_value=None):
            with TestClient(app) as client:
                for i in range(3):
                    r = client.post("/api/v1/chat", data={"user_input": f"查苹果股价 {i}"})
                    check(f"对话 {i + 1} 状态 200", r.status_code == 200)

                check("3 轮对话落库 6 条", memory_db.count_messages() == 6,
                      f"实际 {memory_db.count_messages()} 条")

                hist = client.get("/api/v1/chat/history").json().get("history") or []
                check("/history 读库一致", len(hist) == 6, f"实际 {len(hist)} 条")
                check("/history 内容正确",
                      hist[0]["role"] == "user" and "查苹果股价" in hist[0]["text"]
                      and hist[1]["role"] == "assistant")

                rp = client.put("/api/v1/profile",
                                json={"key": "risk_preference", "value": "稳健"})
                check("profile PUT 200", rp.status_code == 200)
                gp = client.get("/api/v1/profile").json().get("profile") or {}
                check("profile 读回", gp.get("risk_preference") == "稳健")
                rb = client.put("/api/v1/profile", json={"key": "hack", "value": "x"})
                check("profile 非法字段 400", rb.status_code == 400)

                client.post("/api/v1/chat/clear")
                check("/clear 清库", memory_db.count_messages() == 0)

        # ── 3. 滚动摘要：10 条消息触发一次 ──
        from app.memory.summary import maybe_roll_summary

        for i in range(10):
            memory_db.add_message("user", f"消息 {i}")
        with mock.patch("app.memory.summary._summarize", return_value="滚动摘要内容"):
            got = maybe_roll_summary()
        check("摘要 10 条触发", got == "滚动摘要内容")
        check("摘要落库 sessions", memory_db.latest_session_summary() == "滚动摘要内容")

        # 未到阈值不触发
        memory_db.clear_messages()
        memory_db.add_message("user", "单条")
        with mock.patch("app.memory.summary._summarize", return_value="不应触发"):
            got2 = maybe_roll_summary()
        check("不足阈值不触发", got2 is None and memory_db.latest_session_summary() is None)

        check("memory.db 文件存在", (tmp / "memory.db").exists())

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
