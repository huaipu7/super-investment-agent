"""B4 验证：红队段落在看板上怎么显示。

Part A —— 三种状态都画得出，且文案说人话（ok / 证据不足 / 模型不可用）。
Part B —— 被丢弃的条目可见但不计入结论；「最脆弱的一环」与「未解决问题」各自成段。
Part C —— 没有红队（quick/standard 档）时不画空壳。
Part D —— 研究页与调查页都接了同一段渲染。

运行: D:\\python\\python.exe scripts/verify_redteam_ui.py
（全离线：纯渲染，不发请求、不调模型）
"""
from __future__ import annotations

import io
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


OK_RT = {
    "status": "ok",
    "counter_arguments": [
        {"point": "现金流改善来自营运资本释放，不可持续", "evidence_ref": "E1",
         "why": "现金流与利润增速不匹配",
         "evidence": {"kind": "financial_statement", "value": "经营现金流 +12%"}},
        {"point": "渠道库存上升与结论矛盾", "evidence_ref": "E2", "why": "反证已存在",
         "evidence": {"kind": "news", "value": "渠道库存上升"}},
        {"point": "单一来源不足以支撑提价周期", "evidence_ref": "E1", "why": "证据单薄",
         "evidence": {"kind": "financial_statement", "value": "经营现金流 +12%"}},
    ],
    "dropped": [{"point": "这条我编的", "evidence_ref": "E99",
                 "reason": "引用 E99 不在事实池里"}],
    "fragile_assumption": {"assumption": "提价周期支撑毛利率", "evidence_ref": "E1",
                           "why": "只拿到 1 条可采信证据",
                           "evidence": {"value": "经营现金流 +12%"}},
    "open_questions": ["竞争对手的提价节奏不清楚", "未验证：渠道库存下滑（unverified）"],
    "note": "3 条反方论证全部有真实引用",
}

WEAK_RT = {**OK_RT, "status": "insufficient_evidence",
           "note": "只有 1/3 条反方论证挂得住引用，另 2 条因引用对不上被丢弃——证据不足就说不足，不凑数。",
           "counter_arguments": OK_RT["counter_arguments"][:1]}

NO_LLM_RT = {**OK_RT, "status": "llm_unavailable", "counter_arguments": [],
             "dropped": [],
             "note": "模型不可用或未返回可解析结果；反方论证留空，不拿推测充当。"}


def _render(rt) -> tuple[str, list[str]]:
    """跑一次纯渲染，返回 (合并文本, 所有控件的值)。

    用 from_string 而不是 from_function：后者会把函数源码抽出来重跑，
    闭包里的 rt 就丢了，只能在脚本里内联夹具。
    """
    import json as _json

    from streamlit.testing.v1 import AppTest

    script = (
        "import json\n"
        "from ui.components import render_redteam\n"
        f"render_redteam(json.loads({_json.dumps(_json.dumps(rt, ensure_ascii=False))}))\n"
    )
    at = AppTest.from_string(script, default_timeout=120)
    at.run(timeout=120)
    if at.exception:
        raise AssertionError(str(at.exception))
    vals = []
    for attr in ("markdown", "caption", "info", "warning", "error",
                 "success", "subheader", "dataframe"):
        for el in (getattr(at, attr, []) or []):
            v = getattr(el, "value", "")
            # dataframe 的 value 是 DataFrame，`v or ""` 会触发真值歧义
            vals.append("" if v is None else str(v))
    return "\n".join(vals), vals


def main():
    print("\n=== Part A: 三种状态都说人话 ===")
    blob, _ = _render(OK_RT)
    check("ok：标题带状态徽章", "🥊 红队" in blob and "反方论证全部有真实引用" in blob, blob[:80])
    check("ok：三条反方论证都显示",
          all(c["point"] in blob for c in OK_RT["counter_arguments"]))
    check("ok：显示引用标识与依据",
          "E1" in blob and "经营现金流 +12%" in blob)

    blob, _ = _render(WEAK_RT)
    check("证据不足：明确说不够", "证据不足" in blob and "1/3" in blob, blob[:80])
    check("证据不足：不假装是 ok", "反方论证全部有真实引用" not in blob)

    blob, _ = _render(NO_LLM_RT)
    check("模型不可用：直说没给可核对的说法",
          "模型不可用" in blob and "没给出可核对的说法" in blob, blob[:120])

    print("\n=== Part B: 三段各自成段 ===")
    blob, _ = _render(OK_RT)
    check("被丢弃的条目可见且标明不计入",
          "这条我编的" in blob and "不计入任何结论" in blob)
    check("丢弃原因可读", "E99 不在事实池里" in blob)
    check("最脆弱的一环独立成段",
          "最脆弱的一环" in blob and "提价周期支撑毛利率" in blob)
    check("未解决问题独立成段",
          "未解决问题" in blob and "竞争对手的提价节奏不清楚" in blob)

    print("\n=== Part C: 没有红队就不画空壳 ===")
    for label, value in (("None", None), ("空字典", {}), ("非字典", "x")):
        blob, vals = _render(value)
        check(f"{label} → 不渲染任何东西", blob.strip() == "", blob[:60])

    print("\n=== Part D: 两个页面都接上了 ===")
    src_inv = io.open(ROOT / "ui" / "pages" / "investigation.py", encoding="utf-8").read()
    src_res = io.open(ROOT / "ui" / "pages" / "research.py", encoding="utf-8").read()
    check("调查页渲染 redteam", "render_redteam(rep.get(\"redteam\"))" in src_inv)
    check("研究页渲染 redteam", "render_redteam(result.get(\"redteam\"))" in src_res)
    check("研究页只在有红队时才画", "if result.get(\"redteam\"):" in src_res)

    from app.agent.redteam import validate_redteam
    check("固件本身通过红队校验", validate_redteam(OK_RT) == [], str(validate_redteam(OK_RT)))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
