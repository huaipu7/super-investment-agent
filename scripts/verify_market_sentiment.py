"""市场情绪验证（Step 3）：评分/标签/诚实缺失/真实数据 + 活体 curl。

Part A —— 单元：label_for 边界、_clip、_pct_rank。
Part B —— 诚实缺失：信号获取失败 → 该维度 score=None + 标注；全失败 → 总分 None + "数据不足"。
Part C —— 真实数据：compute 三市场（llm=False），断言评分 ∈[-100,100]、
         标签映射、as_of、breakdown 结构、每市场 ≥1 真实信号。
Part D —— 活体 curl：GET /api/v1/market/sentiment?market=all&llm=false。

依赖后端已启动: D:\\python\\python.exe api/main.py
运行: PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\verify_market_sentiment.py
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

from app.analysis import market_sentiment as ms  # noqa: E402

_results: list[dict] = []


def record(name: str, ok: bool, detail: str = ""):
    _results.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


# ── Part A：单元 ─────────────────────────────────────────

def check_unit():
    print("\n=== Part A: 评分/标签单元 ===")
    mapping = {v: ms.label_for(v) for v in [-60, -30, 0, 25, 60, None]}
    exp = {-60: "恐慌", -30: "谨慎", 0: "中性", 25: "偏暖", 60: "贪婪", None: "数据不足"}
    record("label_for 边界映射", mapping == exp, f"{mapping}")

    record("_clip 截断", ms._clip(120) == 100.0 and ms._clip(-120) == -100.0
           and ms._clip(30) == 30.0, "clip(±120, 30)")

    import pandas as pd
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    p_mid = ms._pct_rank(s, 3.0)
    p_hi = ms._pct_rank(s, 9.0)
    record("_pct_rank 经验分位", abs(p_mid - 0.6) < 1e-9 and p_hi == 1.0,
           f"rank(3)= {p_mid}, rank(9)= {p_hi}")


# ── Part B：诚实缺失 ─────────────────────────────────────

def check_missing():
    print("\n=== Part B: 缺失信号诚实标注 ===")
    orig = ms._BUILDERS["a_share"]

    def boom(ctx):
        raise RuntimeError("intentional")
    boom._dim = "margin"

    # 单信号失败 → 该维度 score=None，其余正常合成
    ms._BUILDERS["a_share"] = [orig[0], orig[1], orig[2], boom, orig[4], orig[5]]
    try:
        r = ms.compute_market_sentiment("a_share", llm=False)
    except Exception as e:
        record("单信号失败→None", False, str(e))
    else:
        margin = next(d for d in r["breakdown"] if d["dim"] == "margin")
        record("单信号失败→score None",
               margin["score"] is None and margin.get("note", "") != ""
               and margin["source"] == "（信号获取失败）",
               f"margin={margin}")
        record("单信号失败→总分仍合成",
               r["score"] is not None and r["label"] not in ("数据不足",),
               f"score={r['score']} label={r['label']}")
    finally:
        ms._BUILDERS["a_share"] = orig

    # 全信号失败 → 总分 None + "数据不足"
    def boom2(ctx):
        raise RuntimeError("intentional")
    boom2._dim = "momentum"
    ms._BUILDERS["a_share"] = [boom2, boom2, boom2, boom2, boom2, boom2]
    try:
        r2 = ms.compute_market_sentiment("a_share", llm=False)
    except Exception as e:
        record("全信号失败→数据不足", False, str(e))
    else:
        record("全信号失败→数据不足",
               r2["score"] is None and r2["label"] == "数据不足"
               and all(d["score"] is None for d in r2["breakdown"])
               and r2["sources"] == [],
               f"score={r2['score']} label={r2['label']}")
    finally:
        ms._BUILDERS["a_share"] = orig


# ── Part C：真实数据三市场 ───────────────────────────────

_ALLOWED_LABELS = {"恐慌", "谨慎", "中性", "偏暖", "贪婪", "数据不足"}


def _valid_signal(d: dict) -> bool:
    return all(k in d for k in ("dim", "label", "value", "score", "source", "as_of", "note"))


def check_live():
    print("\n=== Part C: 真实数据三市场 ===")
    for m in ("a_share", "us", "hk"):
        r = ms.compute_market_sentiment(m, llm=False)
        ok_structure = (r.get("market") == m and r.get("market_label")
                        and r.get("label") in _ALLOWED_LABELS
                        and isinstance(r.get("breakdown"), list)
                        and all(_valid_signal(d) for d in r["breakdown"])
                        and "generated_at" in r and "llm_interpretation" in r)
        score = r.get("score")
        ok_score = (score is None) or (-100.0 <= score <= 100.0)
        ok_sources = isinstance(r.get("sources"), list) and len(r["sources"]) >= 1
        ok_asof = isinstance(r.get("as_of"), str) and len(r["as_of"]) >= 8
        ok_label_consistent = (score is None) == (r.get("label") == "数据不足")
        # 标签与分数一致性：非 None 时按边界断言
        if score is not None:
            ok_label_consistent = ok_label_consistent and ms.label_for(score) == r.get("label")
        ok = all([ok_structure, ok_score, ok_sources, ok_asof, ok_label_consistent])
        detail = (f"score={score} label={r.get('label')} as_of={r.get('as_of')} "
                  f"sources={len(r['sources'])} dims={len(r['breakdown'])}")
        record(f"{m} 真实数据", ok, detail)


# ── Part D：活体 curl ────────────────────────────────────

def check_curl():
    print("\n=== Part D: 活体 curl market=all ===")
    try:
        res = subprocess.run(
            ["curl", "-s", "-G", "http://127.0.0.1:8000/api/v1/market/sentiment",
             "-d", "market=all", "-d", "llm=false"],
            capture_output=True, timeout=240,
        )
        out = res.stdout.decode("utf-8", errors="replace")
        data = json.loads(out)
    except Exception as e:
        record("curl market=all", False, f"{type(e).__name__}: {e}")
        return
    markets = data.get("markets") or {}
    ok = set(markets) == {"a_share", "us", "hk"}
    for m, v in markets.items():
        ok = ok and -100.0 <= (v.get("score") or 0) <= 100.0 and bool(v.get("as_of"))
    record("curl market=all", ok,
           f"markets={list(markets)} as_of={data.get('as_of')}")


if __name__ == "__main__":
    check_unit()
    check_missing()
    check_live()
    check_curl()

    passed = sum(1 for r in _results if r["ok"])
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"市场情绪验证: {passed}/{total} 通过")
    for r in _results:
        if not r["ok"]:
            print(f"  FAIL: {r['name']} {r['detail']}")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)
