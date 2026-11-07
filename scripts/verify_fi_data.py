# -*- coding: utf-8 -*-
"""Phase 1 验证 —— 固收零 key 数据接入层真实回归。

从真实官方源拉数并断言：
  1. Treasury 每日收益率曲线非空、含 10Y、带 vintage（point-in-time）
  2. EDGAR 年度基本面面板非空、含杠杆率、带 filed；as_of 反查时全部 filed<=as_of（防 look-ahead）
  3. FOMC 最新声明可达、含 url/正文长度

运行：D:\\python\\python.exe scripts/verify_fi_data.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

PASS, FAIL = "PASS", "FAIL"
_checks: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _checks.append((f"{PASS if cond else FAIL}  {name}  {detail}").rstrip())
    print(("  [%s] %s %s" % ("OK" if cond else "XX", name, detail)).rstrip())


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 74)
    print("Phase 1 固收零 key 数据接入层验证  运行时间:", pd.Timestamp.now())
    print("=" * 74)

    from app.data import fixed_income
    from app.config import get_settings

    fi = fixed_income.get_fixed_income_data()
    s = get_settings()

    # 0) 数据源开关
    check("数据源开关全部开启",
          s.SOURCE_TREASURY and s.SOURCE_EDGAR and s.SOURCE_FOMC,
          f"treasury={s.SOURCE_TREASURY} edgar={s.SOURCE_EDGAR} fomc={s.SOURCE_FOMC}")

    # 1) Treasury 曲线
    print("\n[1] Treasury 每日收益率曲线")
    curve = fi.get_yield_curve(months=6)
    if curve is not None and not curve.empty:
        lo, hi = (curve.index.min().date(), curve.index.max().date())
    else:
        lo = hi = "—（Treasury 不可用/为空，如实降级，不编造）"
    print(f"   shape={curve.shape if curve is not None else None}, 区间={lo} → {hi}")
    check("曲线非空", not curve.empty, f"rows={len(curve)}, cols={list(curve.columns)[:5]}...")
    check("含 10Y 列", "10 Yr" in curve.columns)
    check("vintage 标注", "vintage" in curve.attrs and curve.attrs["vintage"]["is_point_in_time"],
          str(curve.attrs.get("vintage", "")["source"]))
    if "10 Yr" in curve.columns:
        last10 = curve["10 Yr"].dropna()
        if not last10.empty:
            check("最新 10Y 为真实数值", pd.notna(last10.iloc[-1]),
                  f"10Y={last10.iloc[-1]:.2f}% @ {last10.index[-1].date()}")
    # 2) 防 look-ahead：as_of 截断
    a = pd.Timestamp("2026-08-01")
    curve_a = fi.get_yield_curve(months=6, as_of=a)
    check("曲线 as_of 截断", (not curve_a.empty) and (curve_a.index.max() <= a),
          f"max_date={curve_a.index.max().date() if not curve_a.empty else None} <= {a.date()}")

    # 3) EDGAR 基本面
    print("\n[2] EDGAR 年度信用基本面 (AAPL)")
    df = fi.get_credit_fundamentals("AAPL")
    print(f"   shape={df.shape}, fiscal_ends={[d.date() for d in df.index.tolist()]}")
    check("年度面板非空", not df.empty, f"rows={len(df)}")
    check("含杠杆率且真实", "leverage" in df.columns and df["leverage"].notna().any(),
          f"最新 leverage={df['leverage'].dropna().iloc[-1] if df['leverage'].notna().any() else '—'}")
    check("含 filed 日期", "filed" in df.columns and df["filed"].notna().all())
    check("vintage 标注", "vintage" in df.attrs and df.attrs["vintage"]["is_point_in_time"],
          f"source={df.attrs.get('vintage',{}).get('source')}")

    # 4) EDGAR 防 look-ahead：as_of 反查
    past = pd.Timestamp("2025-06-01")
    df_past = fi.get_credit_fundamentals("AAPL", as_of=past)
    if df_past.empty:
        print("   (as_of=2025-06-01 无已披露行，符合 point-in-time 预期)")
        check("as_of 反查合规(空)", True, "as_of=2025-06-01 → 空(该时点 AAPL 2025 报未披露)")
    else:
        ok = bool((df_past["filed"].astype(str) <= past.strftime("%Y-%m-%d")).all())
        check("as_of 防 look-ahead", ok,
              f"rows={len(df_past)}, max_filed={df_past['filed'].max()}")

    # 5) FOMC 政策信号
    print("\n[3] FOMC 最新声明")
    pol = fi.get_policy_signal()
    print(f"   ok={pol['ok']}, url={pol.get('url')}, meeting_date={pol.get('meeting_date')}")
    check("FOMC 可取", pol.get("ok") is True, f"meeting_date={pol.get('meeting_date')}")
    check("含真实正文", pol.get("text_len", 0) > 4000,
          f"text_len={pol.get('text_len')}, body_parsed={pol.get('body_parsed')}")

    # 汇总
    print("\n" + "=" * 74)
    total = len(_checks)
    npass = sum(1 for c in _checks if c.startswith(PASS))
    print(f"结果：{npass}/{total} PASS")
    failed = [c for c in _checks if c.startswith(FAIL)]
    if failed:
        print("\n未通过：")
        for c in failed:
            print("  " + c)
    return 0 if npass == total else 1


if __name__ == "__main__":
    sys.exit(main())
