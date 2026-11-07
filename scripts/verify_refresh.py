"""增量刷新离线守卫（默认不联网）。

覆盖：
- 打桩行情刷新：价格 / PE / 空间与手算一致；未刷新章节逐字段不变
- 真实刷新产物自检：对照其 base_artifact，沿用章节等同、无非有限值、带完整性判定
- 刷新链路不 import 任何 LLM 模块（结构上保证不烧 token）

运行: D:\\python\\python.exe scripts/verify_refresh.py
退出码: 0 = 全部通过；1 = 存在失败。
"""
from __future__ import annotations

import copy
import inspect
import json
import math
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.analysis.price_series import build_price_history  # noqa: E402
from app.analysis.refresh import CARRIED_SECTIONS, refresh_price  # noqa: E402
from app.trust.ledger import build_ledger  # noqa: E402

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


def _find_base() -> Path | None:
    """任取一份含 28 期财报的 600519 产物作基线（离线可复现）。"""
    d = ART / "600519_SS"
    for p in sorted(d.glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if len((r.get("financial_analysis") or {}).get("periods") or []) > 10:
            return p
    return None


def _frame_from(prev: dict, extra_close: float) -> pd.DataFrame:
    ph = prev["price_history"]
    df = pd.DataFrame({"Open": ph["open"], "High": ph["high"], "Low": ph["low"],
                       "Close": ph["close"], "Volume": ph["volume"]},
                      index=pd.to_datetime(ph["dates"]))
    tail = pd.DataFrame({"Open": [extra_close - 5], "High": [extra_close + 8],
                         "Low": [extra_close - 9], "Close": [extra_close],
                         "Volume": [2_000_000.0]},
                        index=[df.index[-1] + pd.Timedelta(days=1)])
    return pd.concat([df, tail])


def _strip_refreshed(prev: dict, new: dict) -> list[str]:
    """返回「按约定不该变」却变了的章节名。"""
    changed = []
    for sec in CARRIED_SECTIONS:
        if prev.get(sec) != new.get(sec):
            changed.append(sec)

    a = copy.deepcopy(prev.get("financial_analysis") or {})
    b = copy.deepcopy(new.get("financial_analysis") or {})
    for x in (a, b):
        x.pop("current_price", None)
        if isinstance(x.get("valuation_context"), dict):
            # pe_percentile_as_of 是刷新新增的「分位时点」标注，单独断言其取值
            for k in ("current_pe", "pe_percentile_as_of"):
                x["valuation_context"].pop(k, None)
    if a != b:
        changed.append("financial_analysis(除 current_price/current_pe/分位时点)")

    a = copy.deepcopy(prev.get("valuation_analysis") or {})
    b = copy.deepcopy(new.get("valuation_analysis") or {})
    # 基线 DCF 若被倍数闸判为不可用，刷新会把 fair_value/degraded/reason 一并改写——
    # 这是预期行为，单独在打桩用例里断言，这里只豁免这三个字段。
    degraded = isinstance(b.get("dcf"), dict) and b["dcf"].get("degraded") is True
    for x in (a, b):
        x.pop("summary", None)
        d = x.get("dcf")
        if isinstance(d, dict):
            d.pop("upside", None)
            if degraded:
                for k in ("fair_value", "degraded", "reason"):
                    d.pop(k, None)
    if a != b:
        changed.append("valuation_analysis(除 upside/summary/闸门降级字段)")
    return changed


def _test_stubbed(base: Path) -> None:
    print("== 打桩刷新（不联网）==")
    prev = json.loads(base.read_text(encoding="utf-8"))
    close = 1560.5
    new = refresh_price(prev, price_data=_frame_from(prev, close),
                        base_ref=str(base), base_generated_at="2026-11-05T00:16:35")

    _ok("price_history 末日更新",
        new["price_history"]["dates"][-1] != prev["price_history"]["dates"][-1])
    _ok("current_price = 注入收盘",
        abs(new["financial_analysis"]["current_price"] - close) < 1e-9)

    shares = new["company_info"]["shares_outstanding"]
    ni = [s["value"] for s in new["financial_analysis"]["raw_metrics"]["NET_INCOME"]
          if s.get("value") is not None][-1]
    pe = new["financial_analysis"]["valuation_context"]["current_pe"]
    # 与全量管线同精度：round(..., 2)。原先允许 <1e-6 误差，会放过「22.103340689355033
    # vs 22.10」这种同一指标两条路径写法不一致的情况。
    _ok("current_pe 与公式同精度一致", pe == round(close * shares / ni, 2), f"got={pe}")

    _ok("分位自报基线日期（不冒充今日值）",
        new["financial_analysis"]["valuation_context"].get("pe_percentile_as_of")
        == (prev.get("price_history") or {}).get("dates", [""])[-1],
        str(new["financial_analysis"]["valuation_context"].get("pe_percentile_as_of")))
    _ok("pricing.price_as_of = effective_price_date",
        (new.get("pricing") or {}).get("price_as_of")
        == new["refresh"].get("effective_price_date"),
        str((new.get("pricing") or {}).get("price_as_of")))

    dcf = new["valuation_analysis"]["dcf"]
    if dcf.get("fair_value") is not None:
        _ok("upside = (fair_value − price) / price",
            abs(dcf["upside"] - (dcf["fair_value"] - close) / close) < 1e-12)
    else:
        _ok("DCF 已降级时 upside 为 None", dcf.get("upside") is None)
        _ok("降级带 reason", bool(dcf.get("reason")))

    changed = _strip_refreshed(prev, new)
    _ok("未刷新章节逐字段不变", not changed, f"变了: {changed}")
    _ok("带 refresh 块", isinstance(new.get("refresh"), dict)
        and new["refresh"].get("mode") == "price")
    _ok("带完整性判定", isinstance(new.get("completeness"), dict)
        and "complete" in new["completeness"])
    _ok("台账 unknown=0", len(build_ledger(new).unknown()) == 0)


def _test_artifact_selfcheck() -> None:
    print("\n== 真实刷新产物自检（对照 base_artifact）==")
    target = None
    for p in sorted(ART.glob("*/*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(r.get("refresh"), dict):
            target = (p, r)
    if target is None:
        print("  [SKIP] 还没有刷新产物，先跑 scripts/refresh_artifact.py")
        return

    path, new = target
    ref = (new.get("refresh") or {}).get("base_artifact") or ""
    base = ROOT / ref
    if not ref or not base.is_file():
        print(f"  [SKIP] 基线产物缺失: {ref}")
        return
    prev = json.loads(base.read_text(encoding="utf-8"))
    print(f"  产物: {path.relative_to(ROOT).as_posix()}")
    print(f"  基线: {ref}")

    changed = _strip_refreshed(prev, new)
    _ok("沿用章节与基线一致", not changed, f"变了: {changed}")

    def _nonfinite(obj, path=""):
        if isinstance(obj, float) and not math.isfinite(obj):
            return [path]
        if isinstance(obj, dict):
            return [x for k, v in obj.items() for x in _nonfinite(v, f"{path}.{k}")]
        if isinstance(obj, list):
            return [x for i, v in enumerate(obj) for x in _nonfinite(v, f"{path}[{i}]")]
        return []

    bad = _nonfinite(new)
    _ok("产物无非有限值", not bad, f"{len(bad)} 处: {bad[:3]}")
    _ok("replace 后的台账 unknown=0", len(build_ledger(new).unknown()) == 0)


def _test_no_llm() -> None:
    print("\n== 刷新链路不碰 LLM ==")
    import app.analysis.refresh as m
    src = inspect.getsource(m)
    _ok("源码未引用 LLM 客户端", all(k not in src for k in
        ("app.llm", "LLMClient", "get_llm_client", "parse_llm_json")))
    _ok("app.llm.client 未被加载（结构上不会调模型）",
        "app.llm.client" not in sys.modules)


def main() -> int:
    print("=" * 60)
    print("verify_refresh.py — 增量刷新守卫")
    print("=" * 60)
    base = _find_base()
    if base is None:
        print("[FAIL] 找不到可用的 600519 基线产物")
        return 1
    _test_stubbed(base)
    _test_artifact_selfcheck()
    _test_no_llm()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
