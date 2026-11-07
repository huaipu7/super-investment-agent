"""事实级跨 run 对比离线守卫（不联网）。

覆盖：
- 对齐键：末尾下标 + as_of 才归一；分部/片段路径保持原样
- 三组真实产物对的五类变化计数（回归口径）
- 同一产物自比必须零变化
- NaN 迁移不崩、归入 undefined
- 换源 / 降级反转可由 degraded / imputed 标记触发
- 确定性：同一对产物跑两次结果完全一致

运行: D:\\python\\python.exe scripts/verify_factdiff.py
退出码: 0 = 全部通过；1 = 存在失败。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.trust.factdiff import align_key, diff_artifacts, diff_facts  # noqa: E402

ART = ROOT / "data" / "artifacts"
_PASS = 0
_FAIL = 0


def _ok(name: str, cond: bool, detail: str = ""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name} {detail}")


@dataclass
class _F:
    path: str
    as_of: str | None = None


def _test_align_key() -> None:
    print("== 对齐键 ==")
    cases = [
        (_F("price_history.close[299]", "2026-11-06"), "price_history.close@2026-11-06"),
        (_F("financial_analysis.ratios.ROE[3].value", "2025"),
         "financial_analysis.ratios.ROE@2025.value"),
        # 无 as_of → 保持原样
        (_F("price_history.close[299]", None), "price_history.close[299]"),
        # 末尾不是下标 → 保持原样（分部顺序、片段排序有语义）
        (_F("sotp_analysis.segments[2].revenue_share", "2024"),
         "sotp_analysis.segments[2].revenue_share"),
        (_F("knowledge.snippets[2].metadata.lex_score", "2026-08-12"),
         "knowledge.snippets[2].metadata.lex_score"),
    ]
    for fact, expect in cases:
        got = align_key(fact)
        _ok(f"{fact.path} → {expect}", got == expect, f"got={got}")


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _test_real_pairs() -> None:
    print("\n== 真实产物对（回归口径）==")
    cases = [
        ("AAPL", ART / "AAPL" / "20261104T171711.json", ART / "AAPL" / "20261105T002222.json",
         {"added": 29, "removed": 23, "value_changed": 1380,
          "noise": 1180, "minor": 16, "material": 184, "undefined": 0}),
        ("600519", ART / "600519_SS" / "20261104T151305.json",
         ART / "600519_SS" / "20261105T001635.json",
         {"added": 44, "removed": 0, "value_changed": 68,
          "noise": 3, "minor": 35, "material": 30, "undefined": 0}),
        ("0700", ART / "0700_HK" / "20261105T014111.json",
         ART / "0700_HK" / "20261105T015658.json",
         {"value_changed": 80, "undefined": 2}),
    ]
    for label, old_p, new_p, expect in cases:
        if not (old_p.is_file() and new_p.is_file()):
            print(f"  [SKIP] {label} 产物缺失")
            continue
        s = diff_artifacts(old_p, new_p)["summary"]
        for k, v in expect.items():
            _ok(f"{label} {k}={v}", s[k] == v, f"实际 {s[k]}")

    # 实质变化里必须能抓到 SOTP 分部改值——这是最能说明问题的样本
    d = diff_artifacts(ART / "600519_SS" / "20261104T151305.json",
                       ART / "600519_SS" / "20261105T001635.json")
    keys = {e["key"] for e in d["changes"]["value_changed"]
            if e["class"] == "material"}
    _ok("600519 抓到 SOTP 分部改值",
        "sotp_analysis.segments[2].value_high" in keys, str(sorted(keys)[:3]))
    _ok("600519 抓到舆情分改值", "news_analysis.sentiment_score" in keys)


def _test_self_and_nan() -> None:
    print("\n== 自比 / 非有限值 ==")
    p = ART / "600519_SS" / "20261105T001635.json"
    r = _load(p)
    s = diff_facts(r, r)["summary"]
    _ok("自比九项全 0",
        all(s[k] == 0 for k in ("added", "removed", "value_changed",
                                "reprovenanced", "degraded_flip", "noise",
                                "minor", "material", "undefined")), str(s))

    old_nan = {"financial_analysis": {"current_price": float("nan")}}
    new_ok = {"financial_analysis": {"current_price": 397.2}}
    d = diff_facts(old_nan, new_ok)
    e = d["changes"]["value_changed"]
    _ok("NaN → 数值 不崩且归入 undefined",
        len(e) == 1 and e[0]["class"] == "undefined" and e[0]["old_value"] is None,
        str(e))
    _ok("undefined 不把 NaN 写进 JSON",
        "NaN" not in json.dumps(d, ensure_ascii=False, default=str))

    both_nan = diff_facts({"a": {"x": float("nan")}}, {"a": {"x": float("nan")}})
    _ok("NaN vs NaN 视为同值（不算改值）",
        both_nan["summary"]["value_changed"] == 0)


def _test_provenance_flip() -> None:
    print("\n== 换源 / 降级反转 ==")
    old = {"financial_analysis": {"fcf_history": [1.0, 2.0]},
           "trade_plan": {"current_price": 10.0}}
    new = {"financial_analysis": {"fcf_history": [1.0, 2.0], "imputed": True},
           "trade_plan": {"current_price": 10.0, "degraded": True}}
    d = diff_facts(old, new)
    _ok("imputed 标记 → 换源", d["summary"]["reprovenanced"] == 2,
        str(d["summary"]))
    _ok("degraded 标记 → 降级反转", d["summary"]["degraded_flip"] == 1,
        str(d["summary"]))
    _ok("换源不计入改值", d["summary"]["value_changed"] == 0)


def _test_determinism() -> None:
    print("\n== 确定性 ==")
    a = diff_artifacts(ART / "AAPL" / "20261104T171711.json",
                       ART / "AAPL" / "20261105T002222.json")
    b = diff_artifacts(ART / "AAPL" / "20261104T171711.json",
                       ART / "AAPL" / "20261105T002222.json")
    _ok("同一对产物两次结果一致", a == b)


def main() -> int:
    print("=" * 60)
    print("verify_factdiff.py — 事实级对比守卫")
    print("=" * 60)
    _test_align_key()
    _test_real_pairs()
    _test_self_and_nan()
    _test_provenance_flip()
    _test_determinism()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
