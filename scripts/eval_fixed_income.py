# -*- coding: utf-8 -*-
"""固收评测集 —— 对 ①信用 / ②利率情景 引擎做**诚实**评测。

约束 #7（不编造）下的评测边界：
  - 信用：无商业评级/无真实违约标签 feed → 不宣称 AUC/KS 这类需真标签的指标。
    改为如实给出【覆盖率 / 分区分布 / 数据可用性】统计，以及一个【结构分一致性】：
    Altman Z'' 与 Ohlson O 的横截面秩相关（两者同为财务困境代理，方向应一致）。
    这个一致性度量是可复算、来自真实财务数据的，不代表违约预测力。
  - 利率：用 walk-forward（train<=cutoff<test）回测 level 因子，**必须同时报 VAR 与
    naive 基准**（只有跑赢基准才有增量信息）。真实 Treasury 历史；若数据缺失则如实降级。
  - 政策：报 FOMC 真实声明解析结果或不可用。

输出：stdout 报告 + 写 data/eval_fixed_income.json（含 as_of / 覆盖 / 每 ticker 行）。

运行：D:\\python\\python.exe scripts/eval_fixed_income.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

PASS, FAIL = "PASS", "FAIL"
_checks: list[tuple[str, bool, bool]] = []  # (name, ok, hard)  hard=引擎必须跑对（数据缺失属env，非硬错）


def check(name, cond, detail="", hard=True):
    """hard=True 表示除非降级该点必须为真（引擎功能正确）。"""
    _checks.append((name, bool(cond), hard))
    print(("  [%s] %s %s" % ("OK" if cond else "XX", name, detail)).rstrip())


# 评测主体队列（海外发债样本，覆盖多行业/多信用档）
COHORT = ["F", "AAPL", "KO", "XOM", "TSLA", "JPM", "MSFT", "JNJ", "INTC", "BA"]


def _zone(z):
    if z is None:
        return None
    if z < 1.10:
        return "风险区"
    if z < 2.60:
        return "灰区"
    return "安全区"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("=" * 78)
    print("固收评测集（诚实）  运行时间:", pd.Timestamp.now())
    print("=" * 78)

    from app.data import fixed_income
    from app.credit import features as F

    fi = fixed_income.get_fixed_income_data()

    # ── ① 信用截面：覆盖率 + 分区分布 + 一致性 ───────────────
    print("\n[1] 信用截面（真实 EDGAR 财务，点位时间 filed 防前视）")
    rows = []
    for tk in COHORT:
        rec = {"ticker": tk, "z": None, "o": None, "zone": None,
               "as_of_end": None, "filed": None}
        try:
            panel = fi.get_credit_panel(tk)
            if panel is None or panel.empty:
                rows.append(rec)
                continue
            ff = F.build_feature_frame(panel)
            if ff.empty or ff["altman_z"].dropna().empty:
                rows.append(rec)
                continue
            last = ff.iloc[-1]
            z = None if pd.isna(last["altman_z"]) else round(float(last["altman_z"]), 3)
            o = None if pd.isna(last["ohlson_o"]) else round(float(last["ohlson_o"]), 3)
            rec.update({"z": z, "o": o, "zone": _zone(z),
                        "as_of_end": str(panel.index[-1].date()) if len(panel) else None,
                        "filed": str(last["filed"])[:10] if pd.notna(last.get("filed")) else None})
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
        rows.append(rec)

    covered = [r for r in rows if r["z"] is not None]
    n_cover, n_tot = len(covered), len(COHORT)
    zones = {z: sum(1 for r in covered if r["zone"] == z) for z in ("安全区", "灰区", "风险区")}
    print(f"  主体覆盖：{n_cover}/{n_tot}（EDGAR 财报可算 Altman Z''）")
    print(f"  分区分布：{zones}")
    for r in rows:
        print(f"    {r['ticker']:<6}  Z''={str(r['z']):>7}  O={str(r['o']):>7}  "
              f"{r['zone'] or '—':<4}  end={r['as_of_end'] or '—'}  filed={r['filed'] or '—'}")
    check("信用主体覆盖 >0", n_cover > 0, f"{n_cover}/{n_tot}")

    # 一致性：Z'' 与 O 应负相关（Z'' 越高越安全，O 越高越困境）
    zs = [r["z"] for r in covered if r["z"] is not None and r["o"] is not None]
    os_ = [r["o"] for r in covered if r["z"] is not None and r["o"] is not None]
    if len(zs) >= 5:
        from scipy.stats import spearmanr
        rho, p = spearmanr(zs, os_)
        print(f"  结构分一致性：Spearman(Z'', O) = {rho:.3f}（p={p:.3f}），"
              f"负相关=两代理方向一致；仅一致性度量，非违约预测力。")
        check("Z''-O 方向一致（负相关）", rho < 0, f"rho={rho:.3f}", hard=False)
    else:
        check("Z''-O 方向一致（负相关）", True, "样本不足，跳过一致性", hard=False)
        rho = None

    # ── ② 利率 walk-forward 回测（level 因子；VAR vs naive）────
    print("\n[2] 利率水平因子 walk-forward 回测（防泄漏：train<=cutoff<test）")
    from app.rates import curve as C
    from app.rates import models as M
    curve = fi.get_yield_curve(months=84)
    if curve is None or curve.empty:
        print("  Treasury 历史曲线缺失 → 降级（不编造）——这是诚实输出，非引擎错误")
        rmse_meta = {"error": "Treasury 历史曲线缺失", "degraded": True}
        check("利率回测诚实对照（VAR vs naive）", True,
              "Treasury 缺失，如实降级（非硬错）", hard=False)
    else:
        factors = C.fit_curve_path(curve)
        cols = [c for c in ["level", "slope", "curvature", "curvature2"] if c in factors.columns]
        try:
            rv = M.walk_forward(factors, cols, horizon=5, min_train=60, step=30, use="var")
            rn = M.walk_forward(factors, cols, horizon=5, min_train=60, step=30, use="naive")
            mv, mn = M.backtest_metrics(rv, col="level"), M.backtest_metrics(rn, col="level")
            rmse_meta = {"var": mv.get("rmse"), "naive": mn.get("rmse"),
                         "var_n": mv.get("n"), "naive_n": mn.get("n"),
                         "var_direction_hit": mv.get("direction_hit_abs")}
            better = (rmse_meta["var"] is not None and rmse_meta["naive"] is not None
                      and rmse_meta["var"] < rmse_meta["naive"] * (1 + 1e-9))
            print(f"  VAR  RMSE={rmse_meta['var']}  naive RMSE={rmse_meta['naive']}  "
                  f"(n_var={rmse_meta['var_n']}, n_naive={rmse_meta['naive_n']})")
            print(f"  诚实对照：VAR  {'跑赢' if better else '未跑赢'} naive（level≈随机游走，"
                  f"短窗 naive 常占优；跑赢即有增量信息，否则如实报告。）")
            check("利率回测诚实对照（VAR vs naive 均有 RMSE）",
                  rmse_meta["var"] is not None and rmse_meta["naive"] is not None,
                  f"var={rmse_meta['var']} naive={rmse_meta['naive']}")
        except Exception as e:
            print(f"  利率回测异常 → {type(e).__name__}: {e}")
            rmse_meta = {"error": str(e)}
            check("利率回测诚实对照（VAR vs naive）", False, str(e))

    # ── ③ 政策信号（真实 FOMC 声明）────────────────────────
    print("\n[3] 政策信号（真实 FOMC 原文 → 词典法鸽鹰分）")
    from app.rates import policy as P
    pol = P.PolicySignal().score_statement(fi.get_policy_signal())
    print(f"  会议={pol.get('meeting_date') or '—'}  立场={pol.get('stance') or '—'}  "
          f"鸽鹰分={pol.get('score') or '—'}  命中鹰={pol.get('num_hawk_tokens')} "
          f"鸽={pol.get('num_dove_tokens')}")
    check("政策信号返回立场（或不可用）", pol.get("ok") in (True, False),
          f"stance={pol.get('stance')}")

    # ── 汇总 ────────────────────────────────────────────────
    summary = {
        "as_of": pd.Timestamp.now().isoformat(),
        "credit": {"cohort": COHORT, "covered": n_cover, "total": n_tot,
                   "zones": zones, "rank_corr_z_o": rho},
        "rates": rmse_meta,
        "policy": {"meeting_date": pol.get("meeting_date"),
                   "stance": pol.get("stance"), "score": pol.get("score")},
        "honest_boundary": ("信用无真实违约/商业评级标签 → 不报 AUC/KS；仅覆盖/分布/一致性。"
                            "利率报 VAR vs naive 诚实对照。均来自真实数据或真实计算。"),
    }
    out_path = Path(__file__).resolve().parent.parent / "data" / "eval_fixed_income.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  → 已写 {out_path}")

    print("\n" + "=" * 78)
    npass = sum(1 for _, c, _ in _checks if c)
    hard_fail = [name for name, c, hard in _checks if hard and not c]
    print(f"结果：{npass}/{len(_checks)} CHECK PASS（硬性确认 {len(_checks) - len(hard_fail)} 项）")
    for name, c, _ in _checks:
        if not c:
            print("  （软提示/降级） " + name)
    print(f"硬性失败：{len(hard_fail)}")
    for name in hard_fail:
        print("  HARD-FAIL: " + name)
    # 退出码只按硬性失败判定：数据缺失导致的诚实降级不视为失败。
    return 0 if not hard_fail else 1


if __name__ == "__main__":
    sys.exit(main())
