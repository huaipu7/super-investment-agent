"""行情时点守卫：`pricing` 块 / 台账补期间 / 刷新混血 / 报告口径（全部离线）。

覆盖：
- 打桩行情：`price_as_of` == 最后一个有效收盘所在行的日期
- 当日 K 线缺失：跳过该行，不取「今天的空壳日期」
- 无 vintage：不猜源，note 明写「行情源未标注」
- 空行情：`price_as_of=None`，不抛异常
- `pricing` 零数值叶子：加不加它，台账 fact_total 完全相等
- 台账：五条价格派生路径补 `as_of`，其余事实逐条不变
- 老产物兼容：没有 `pricing` 时行为与改动前一致
- 刷新混血：分位自报时点、current_pe 与全量管线同精度、pricing 与 effective_price_date 一致
- `hist_pe_periods` 与 `hist_pe` 一一对应
- 报告文本：分位句带样本年份与现价日期，缺 pricing 时不写空占位

运行: D:\\python\\python.exe scripts/verify_pricing.py
退出码: 0 = 全部通过；1 = 存在失败。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.analysis.financial import FinancialAnalyzer  # noqa: E402
from app.analysis.pricing import build_pricing  # noqa: E402
from app.analysis.refresh import refresh_price  # noqa: E402
from app.analysis.valuation import FAIR_VALUE_PRICE_BAND  # noqa: E402
from app.report import markdown as md  # noqa: E402
from app.trust.ledger import _PRICE_ANCHORED_PATHS, build_ledger  # noqa: E402

ART = ROOT / "data" / "artifacts"
_PASS = 0
_FAIL = 0

# 改动前生成的全量产物（无 pricing，且五条价格派生事实俱在），钉住「老产物行为不变」
_OLD_ARTIFACT = ART / "600519_SS" / "20261105T001635.json"
# 刷新产物（DCF 已被倍数闸降级），用作刷新用例的基线
_REFRESH_BASE = ART / "600519_SS" / "20261106T223715.json"


def _ok(name: str, cond: bool, detail: str = ""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def _frame(dates, closes, *, attrs=None) -> pd.DataFrame:
    df = pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                       "Close": closes, "Volume": [1_000.0] * len(closes)},
                      index=pd.to_datetime(dates))
    if attrs:
        df.attrs.update(attrs)
    return df


def _vintage(**kw) -> dict:
    v = {"requested_as_of": None, "data_observation_date": "2026-11-04",
         "source": "yfinance", "is_point_in_time": True, "note": ""}
    v.update(kw)
    return v


def _test_price_as_of() -> None:
    print("== pricing 块 ==")
    df = _frame(["2026-11-03", "2026-11-04"], [100.0, 101.0],
                attrs={"vintage": _vintage()})
    p = build_pricing(df)
    _ok("price_as_of = 最后一根 K 线日期", p["price_as_of"] == "2026-11-04", str(p))
    _ok("source 来自 vintage", p["source"] == "yfinance", str(p))
    _ok("is_point_in_time 来自 vintage", p["is_point_in_time"] is True)

    # 当日 K 线整行缺失（未成交）：Close 为 NaN，必须跳回前一个有效交易日
    gap = pd.concat([df, pd.DataFrame(
        {"Open": [float("nan")], "High": [float("nan")], "Low": [float("nan")],
         "Close": [float("nan")], "Volume": [float("nan")]},
        index=pd.to_datetime(["2026-11-05"]))])
    gap.attrs.update({"vintage": _vintage()})
    _ok("当日整行 NaN 时跳回上一交易日",
        build_pricing(gap)["price_as_of"] == "2026-11-04", str(build_pricing(gap)))
    _ok("空壳当日不被当成行情日", build_pricing(gap)["price_as_of"] != "2026-11-05")

    no_v = _frame(["2026-11-04"], [99.0])
    p2 = build_pricing(no_v)
    _ok("无 vintage 不猜源", p2["source"] == "" and p2["is_point_in_time"] is False)
    _ok("无 vintage 明写未标注", "行情源未标注" in p2["note"], p2["note"])

    p3 = build_pricing(pd.DataFrame())
    _ok("空行情 price_as_of=None", p3["price_as_of"] is None, str(p3))
    _ok("空行情也标注原因", "无有效收盘价" in p3["note"], p3["note"])


def _leaves(obj, path="") -> list[tuple[str, object]]:
    if isinstance(obj, dict):
        return [x for k, v in obj.items() for x in _leaves(v, f"{path}.{k}")]
    if isinstance(obj, list):
        return [x for i, v in enumerate(obj) for x in _leaves(v, f"{path}[{i}]")]
    return [(path, obj)]


def _test_no_facts() -> None:
    print("\n== pricing 不产生事实 ==")
    base = json.loads(_OLD_ARTIFACT.read_text(encoding="utf-8"))
    base.pop("pricing", None)
    with_pricing = copy.deepcopy(base)
    with_pricing["pricing"] = build_pricing(_frame(
        ["2026-11-04"], [1453.0], attrs={"vintage": _vintage()}))

    _ok("pricing 只有字符串/布尔/None",
        all(isinstance(v, (str, bool)) or v is None
            for _, v in _leaves(with_pricing["pricing"])),
        str([(p, type(v).__name__) for p, v in _leaves(with_pricing["pricing"])]))
    _ok("fact_total 不变",
        build_ledger(with_pricing).total == build_ledger(base).total,
        f"{build_ledger(with_pricing).total} vs {build_ledger(base).total}")
    _ok("unknown 数不变",
        len(build_ledger(with_pricing).unknown()) == len(build_ledger(base).unknown()))


def _test_ledger_anchor() -> None:
    print("\n== 台账补期间 ==")
    base = json.loads(_OLD_ARTIFACT.read_text(encoding="utf-8"))
    base.pop("pricing", None)
    old_ledger = build_ledger(base)
    for path in sorted(_PRICE_ANCHORED_PATHS):
        f = old_ledger.find(path)
        _ok(f"老产物 {path} as_of 仍为空", f is not None and f.as_of is None,
            "" if f is None else f"as_of={f.as_of}")

    new = copy.deepcopy(base)
    new["pricing"] = build_pricing(_frame(
        ["2026-11-04"], [1453.0], attrs={"vintage": _vintage()}))
    new_ledger = build_ledger(new)
    for path in sorted(_PRICE_ANCHORED_PATHS):
        f = new_ledger.find(path)
        _ok(f"{path} as_of = 2026-11-04", f is not None and f.as_of == "2026-11-04",
            "" if f is None else f"as_of={f.as_of}")

    drifted = []
    for a, b in zip(old_ledger.facts, new_ledger.facts):
        if a.path == b.path and a.path in _PRICE_ANCHORED_PATHS:
            continue
        if a != b:
            drifted.append(a.path)
    _ok("其余事实逐条不变", not drifted, f"{len(drifted)} 条: {drifted[:3]}")
    _ok("事实条数不变", old_ledger.total == new_ledger.total)


def _test_ledger_pe_date() -> None:
    print("\n== 刷新产物的分位自报时点 ==")
    base = json.loads(_OLD_ARTIFACT.read_text(encoding="utf-8"))
    r = copy.deepcopy(base)
    r["pricing"] = build_pricing(_frame(
        ["2026-11-06"], [1453.0], attrs={"vintage": _vintage()}))
    r["financial_analysis"]["valuation_context"]["pe_percentile_as_of"] = "2026-11-05"
    f = build_ledger(r).find("financial_analysis.valuation_context.pe_percentile")
    _ok("分位用 pe_percentile_as_of，不用现价日期",
        f is not None and f.as_of == "2026-11-05", "" if f is None else f.as_of)
    f2 = build_ledger(r).find("financial_analysis.valuation_context.current_pe")
    _ok("PE 用现价日期", f2 is not None and f2.as_of == "2026-11-06")


def _test_pe_periods() -> None:
    print("\n== hist_pe_periods 与 hist_pe 对齐 ==")
    years = [2021, 2022, 2023, 2024]
    aligned = pd.DataFrame({"NET_INCOME": [1e9, 1.2e9, 1.4e9, 1.6e9]}, index=years)
    price = _frame(
        [f"{y}-06-30" for y in years] + ["2025-06-30"],
        [10.0, 12.0, 14.0, 16.0, 20.0])
    out = FinancialAnalyzer()._compute_pe_percentile(
        aligned, years, price, {"shares_outstanding": 1e8})
    _ok("有 hist_pe_periods", isinstance(out.get("hist_pe_periods"), list), str(out))
    _ok("与 hist_pe 等长",
        len(out.get("hist_pe_periods") or []) == len(out.get("hist_pe") or []), str(out))
    _ok("逐年对应",
        out.get("hist_pe_periods") == [str(y) for y in years], str(out.get("hist_pe_periods")))
    _ok("note 说明样本口径", "全年均价" in (out.get("note") or ""), out.get("note"))
    _ok("note 不复述年份数字", "2021" not in (out.get("note") or ""))

    short = FinancialAnalyzer()._compute_pe_percentile(
        aligned.iloc[:1], years[:1], price, {"shares_outstanding": 1e8})
    _ok("样本不足时不给分位", short.get("pe_percentile") is None, str(short))


def _test_refresh() -> None:
    print("\n== 刷新混血修复 ==")
    prev = json.loads(_REFRESH_BASE.read_text(encoding="utf-8"))
    ph = prev["price_history"]
    df = pd.DataFrame({"Open": ph["open"], "High": ph["high"], "Low": ph["low"],
                       "Close": ph["close"], "Volume": ph["volume"]},
                      index=pd.to_datetime(ph["dates"]))
    close = 1560.5
    tail = pd.DataFrame({"Open": [close - 5], "High": [close + 8],
                         "Low": [close - 9], "Close": [close],
                         "Volume": [2_000_000.0]},
                        index=[df.index[-1] + pd.Timedelta(days=1)])
    frame = pd.concat([df, tail])
    frame.attrs.update({"vintage": _vintage(data_observation_date="2026-11-07")})
    new = refresh_price(prev, price_data=frame,
                        base_ref=str(_OLD_ARTIFACT),
                        base_generated_at="2026-11-05T00:16:35")

    p = new.get("pricing") or {}
    _ok("刷新重建 pricing", p.get("price_as_of") is not None, str(p))
    _ok("pricing 与 effective_price_date 一致",
        p.get("price_as_of") == new["refresh"]["effective_price_date"],
        f"{p.get('price_as_of')} vs {new['refresh']['effective_price_date']}")

    vc = new["financial_analysis"]["valuation_context"]
    # 基线自己就是「上一代刷新产物」：它没有 pe_percentile_as_of，分位实际来自它的基线。
    # 直接取它的现价日期会把分位标新一个交易日，所以必须沿 base_artifact 回溯。
    base_art = json.loads(Path(prev["refresh"]["base_artifact"]).read_text(encoding="utf-8"))
    base_date = (base_art.get("price_history") or {}).get("dates", [""])[-1]
    _ok("刷新产物基线无标注时回溯到再上一份",
        vc.get("pe_percentile_as_of") == base_date
        and base_date != (prev.get("price_history") or {}).get("dates", [""])[-1],
        f"{vc.get('pe_percentile_as_of')} vs 回溯 {base_date} / 自身 "
        f"{(prev.get('price_history') or {}).get('dates', [''])[-1]}")
    shares = new["company_info"]["shares_outstanding"]
    ni = [s["value"] for s in new["financial_analysis"]["raw_metrics"]["NET_INCOME"]
          if s.get("value") is not None][-1]
    _ok("current_pe 与全量管线同精度",
        vc["current_pe"] == round(close * shares / ni, 2), f"got={vc['current_pe']}")

    carried = [c for c in new["refresh"]["carried_over"]
               if c.get("section", "").endswith("pe_percentile")]
    _ok("carried_over 写明分位日期",
        bool(carried) and carried[0]["as_of"] == base_date
        and base_date in carried[0]["note"], str(carried))

    f = build_ledger(new).find("financial_analysis.valuation_context.pe_percentile")
    _ok("台账分位用基线日期", f is not None and f.as_of == base_date,
        "" if f is None else f.as_of)
    _ok("台账 unknown=0", len(build_ledger(new).unknown()) == 0)

    dcf = new["valuation_analysis"]["dcf"]
    if dcf.get("fair_value") is not None:
        lo, hi = FAIR_VALUE_PRICE_BAND
        assert lo <= dcf["fair_value"] / close <= hi


def _test_report_text() -> None:
    print("\n== 报告文本 ==")
    fa = json.loads(_OLD_ARTIFACT.read_text(encoding="utf-8"))
    fa = fa["financial_analysis"]
    vc = fa.setdefault("valuation_context", {})
    vc["hist_pe_periods"] = ["2021", "2022", "2023", "2024", "2025"]

    lines = md._section_financial(
        fa, {"price_as_of": "2026-11-06", "source": "yfinance"})
    text = "\n".join(lines)
    _ok("分位句带样本年份", "样本 2021–2025 年度均价" in text, text[-300:])
    _ok("分位句带现价日期", "现价截至 2026-11-06" in text, text[-300:])

    vc["pe_percentile_as_of"] = "2026-11-05"
    text2 = "\n".join(md._section_financial(
        fa, {"price_as_of": "2026-11-06", "source": "yfinance"}))
    _ok("分位与现价不同日时两个日期都说",
        "该分位按 2026-11-05 收盘价计算" in text2 and "现价已更新至 2026-11-06" in text2,
        text2[-300:])

    vc.pop("pe_percentile_as_of")
    vc.pop("hist_pe_periods")
    text3 = "\n".join(md._section_financial(fa, None))
    _ok("缺口径信息时不写空占位",
        "（）" not in text3 and "（；" not in text3 and "—）" not in text3, text3[-300:])
    _ok("缺口径信息时退化为原句",
        "分位。" in text3 and "现价截至" not in text3, text3[-300:])


def main() -> int:
    print("=" * 60)
    print("verify_pricing.py — 行情时点守卫")
    print("=" * 60)
    for p in (_OLD_ARTIFACT, _REFRESH_BASE):
        if not p.is_file():
            print(f"[FAIL] 找不到基线产物 {p}")
            return 1
    _test_price_as_of()
    _test_no_facts()
    _test_ledger_anchor()
    _test_ledger_pe_date()
    _test_pe_periods()
    _test_refresh()
    _test_report_text()
    print("\n" + "=" * 60)
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
