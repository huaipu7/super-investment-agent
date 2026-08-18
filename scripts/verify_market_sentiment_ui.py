"""情绪 UI 验证（Step 4）：看板页 + api_client + 活体 curl。

Part A —— AppTest 无头渲染「🌡 市场情绪」页：导航到新模块 → 断言无异常、
         三市场卡片（情绪评分 metric ×3）、AI 解读 / 信号明细 / 可用性小节存在。
Part B —— api_client.market_sentiment 走真实后端：all 结构 + 单市场结构断言。
Part C —— 活体 curl market=all&llm=true：断言三市场 + llm_interpretation 字段。

依赖后端已启动: D:\\python\\python.exe api/main.py
运行: PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\verify_market_sentiment_ui.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ui import api_client as api  # noqa: E402

_results: list[dict] = []

_MARKET_ORDER = ["a_share", "us", "hk"]
_ALLOWED_LABELS = {"恐慌", "谨慎", "中性", "偏暖", "贪婪", "数据不足"}


def record(name: str, ok: bool, detail: str = ""):
    _results.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


# ── Part A：看板 AppTest ───────────────────────────────

def check_dashboard():
    print("\n=== Part A: 看板「🌡 市场情绪」渲染 ===")
    from streamlit.testing.v1 import AppTest

    app_path = str(ROOT / "ui" / "dashboard.py")
    at = AppTest.from_file(app_path, default_timeout=240)
    try:
        at.run(timeout=240)
        at.radio[0].set_value("🌡 市场情绪")
        at.run(timeout=240)
    except Exception as e:
        record("情绪页渲染", False, f"exception={e}")
        return
    if at.exception:
        record("情绪页渲染", False, f"exception={at.exception}")
        return
    record("情绪页渲染无异常", True, "AppTest 运行无 exception")

    subs = [getattr(s, "value", "") or "" for s in at.subheader]
    need = ["📝 AI 市场解读", "💾 信号明细", "⚠️ 数据可用性"]
    for n in need:
        record(f"小节存在 {n}", any(n in x for x in subs),
               f"subheaders={subs[:6]}")

    metrics = [m for m in at.metric if m.label == "情绪评分"]
    record("三市场卡片(情绪评分×3)", len(metrics) == 3,
           f"count={len(metrics)} values={[m.value for m in metrics]}")


# ── Part B：api_client 真实后端 ─────────────────────────

def check_api():
    print("\n=== Part B: api_client.market_sentiment ===")
    r = api.market_sentiment("all", llm=False)
    markets = r.get("markets") or {}
    record("all 三市场齐全", set(markets) == set(_MARKET_ORDER),
           f"markets={list(markets)}")

    ok_all = True
    for m in _MARKET_ORDER:
        v = markets.get(m) or {}
        dims = v.get("breakdown") or []
        ok_all = ok_all and v.get("market_label") and v.get("label") in _ALLOWED_LABELS \
            and isinstance(dims, list) and "as_of" in v and "sources" in v \
            and all({"dim", "label", "value", "score", "source", "as_of", "note"} <= set(d.keys())
                    for d in dims)
    record("每市场结构完整(label/breakdown/as_of/sources)",
           ok_all, "breakdown 字段齐全")

    r2 = api.market_sentiment("a_share", llm=False)
    record("单市场形态",
           r2.get("market") == "a_share" and r2.get("label") in _ALLOWED_LABELS
           and isinstance(r2.get("breakdown"), list),
           f"market={r2.get('market')} label={r2.get('label')}")

    score = r2.get("score")
    record("单市场评分 ∈[-100,100] 或 None",
           score is None or -100.0 <= score <= 100.0, f"score={score}")


# ── Part C：活体 curl（llm=true 路径）───────────────────

def check_curl():
    print("\n=== Part C: 活体 curl market=all&llm=true ===")
    try:
        res = subprocess.run(
            ["curl", "-s", "-G", "http://127.0.0.1:8000/api/v1/market/sentiment",
             "-d", "market=all", "-d", "llm=true"],
            capture_output=True, timeout=240,
        )
        out = res.stdout.decode("utf-8", errors="replace")
        data = json.loads(out)
    except Exception as e:
        record("curl llm=true", False, f"{type(e).__name__}: {e}")
        return
    markets = data.get("markets") or {}
    ok = set(markets) == set(_MARKET_ORDER) and bool(data.get("generated_at"))
    for m, v in markets.items():
        ok = ok and "llm_interpretation" in v
    record("curl llm=true 三市场 + llm_interpretation 字段", ok,
           f"markets={list(markets)} llm_examples="
           f"{[bool((markets[m] or {}).get('llm_interpretation')) for m in _MARKET_ORDER]}")


if __name__ == "__main__":
    check_dashboard()
    check_api()
    check_curl()

    passed = sum(1 for r in _results if r["ok"])
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"情绪 UI 验证: {passed}/{total} 通过")
    for r in _results:
        if not r["ok"]:
            print(f"  FAIL: {r['name']} {r['detail']}")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)
