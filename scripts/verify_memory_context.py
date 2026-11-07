"""验证：长时记忆（画像 / 会话摘要）真正注入 prompt，而非只落库不读。

覆盖：
  1. 无数据 → 空串（不编造占位内容，约束#7）
  2. 有画像 → 渲染进上下文
  3. 有摘要 → 渲染进上下文
  4. DB 异常 → 不抛、不阻断，记 warning
  5. _build_context_summary 无 history 时仍注入记忆（修复的回归点）
  6. intent.parse 的 llm_input 含记忆前缀
  7. lifespan 用库中历史回填 chat_history，且不重复落库

运行: D:\\python\\python.exe scripts\\verify_memory_context.py
（全离线，不调真实 LLM）
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
    tmp = Path(tempfile.mkdtemp(prefix="verify_memctx_"))

    class _TmpSettings:
        memory_db = tmp / "memory.db"

    from app.memory import db as memory_db
    from app.memory.context import build_memory_context
    from app.agent import intent as intent_mod
    from app.agent.expressor import _build_context_summary

    with mock.patch("app.memory.db.get_settings", lambda: _TmpSettings()):
        memory_db.init_db()

        # ── 1. 无数据：必须空串，不得编造 ──
        check("无数据返回空串", build_memory_context() == "",
              repr(build_memory_context()))

        # ── 2. 画像注入 ──
        memory_db.set_profile("risk_preference", "稳健")
        memory_db.set_profile("holdings", "AAPL,600519")
        ctx = build_memory_context()
        check("画像渲染", "【用户画像】" in ctx and "风险偏好：稳健" in ctx, ctx[:80])
        check("画像含持仓", "持仓：AAPL,600519" in ctx)
        check("画像标注为非复述材料", "不要原样复述" in ctx)

        # ── 3. 摘要注入 ──
        memory_db.update_session("用户关注苹果与茅台盈利能力")
        ctx2 = build_memory_context()
        check("摘要渲染", "【近期对话摘要】用户关注苹果与茅台盈利能力" in ctx2)

        # ── 4. DB 异常不阻断 ──
        with mock.patch("app.memory.db.get_all_profiles",
                        side_effect=RuntimeError("boom")):
            ctx3 = build_memory_context()  # 不应抛
        check("DB 异常不抛且降级", "【用户画像】" not in ctx3 and "【近期对话摘要】" in ctx3)

        # ── 5. 报告上下文：无 history 仍注入记忆（回归点） ──
        s_none = _build_context_summary(None)
        s_empty = _build_context_summary([])
        check("无 history 仍注入记忆", "风险偏好：稳健" in s_none and "风险偏好：稳健" in s_empty,
              repr(s_none[:60]))
        s_hist = _build_context_summary([{"role": "user", "text": "查苹果股价"}])
        check("有 history 时记忆+语境并存",
              "风险偏好：稳健" in s_hist and "6. 注意对话上下文" in s_hist)
        check("无数据时 history 块仍正常",
              "6. 注意对话上下文" in _build_context_summary(
                  [{"role": "user", "text": "x"}]))

        # ── 6. intent.parse 注入前缀 ──
        captured = {}

        def fake_fc(llm_input):
            captured["input"] = llm_input
            return {"intent": "query_price", "tickers": ["AAPL"],
                    "timeframe": None, "analysis_type": None}

        with mock.patch("app.agent.intent._parse_with_function_calling", fake_fc):
            out = intent_mod.parse("查苹果股价")
        check("intent 前缀含画像", "风险偏好：稳健" in captured.get("input", ""))
        check("intent 前缀含摘要", "【近期对话摘要】" in captured.get("input", ""))
        check("intent 前缀含原始问句", "查苹果股价" in captured.get("input", ""))
        check("intent 解析结果不受影响", out["intent"] == "query_price"
              and out["tickers"] == ["AAPL"])

        # ── 7. lifespan 回填 chat_history，不重复落库 ──
        memory_db.clear_messages()
        memory_db.add_message("user", "查苹果股价")
        memory_db.add_message("assistant", "AAPL 当前 180.0")

        from fastapi.testclient import TestClient
        from api.main import app
        from api.routes_chat import chat_history

        with TestClient(app) as client:
            check("lifespan 回填内存历史", len(chat_history) == 2,
                  f"实际 {len(chat_history)}")
            check("回填未重复落库", memory_db.count_messages() == 2,
                  f"实际 {memory_db.count_messages()}")
            check("回填内容正确",
                  chat_history[0].get("role") == "user"
                  and "查苹果股价" in chat_history[0].get("text", ""))
            hist = client.get("/api/v1/chat/history").json().get("history") or []
            check("/history 与回填一致", len(hist) == 2)

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
