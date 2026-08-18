"""Phase B 验证：as_of_date 历史数据快照 + vintage 诚实标注 + backtest 联动不泄漏。

覆盖（全部离线，mock 数据源）：
  (a) get_quote(as_of) 返回 bar 日期 ≤ as_of，且取 ≤as_of 最后一根
  (b) get_history(as_of) 序列锚定 ≤as_of；vintage 附着；无 as_of 也带 vintage
  (c) 不同 as_of 命中不同缓存 key；_history_df 把 end 透传给数据源
  (d) financials as_of → is_point_in_time=False 且 note 明示无法回退历史快照
  (e) executor 层透传 vintage
  (f) backtest 联动 walk-forward 不报泄漏（data_source=super_investment，mock 行情）

运行: D:\\python\\python.exe scripts\\verify_asof.py
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
from pathlib import Path
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SIA_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SIA_ROOT))

BACKTEST_ROOT = Path("D:/Desktop/ClaudeProjects/backtest_workflow")
sys.path.insert(0, str(BACKTEST_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _synthetic_history(start, end, seed: int = 42) -> pd.DataFrame:
    """确定性日线（工作日），Open/High/Low/Close/Volume 齐备。"""
    idx = pd.bdate_range(pd.to_datetime(start), pd.to_datetime(end))
    if len(idx) == 0:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    rng = np.random.default_rng(seed)
    n = len(idx)
    rets = rng.normal(0.0002, 0.015, n)
    close = 100.0 * np.exp(np.cumsum(rets))
    opn = close * (1 + rng.normal(0, 0.005, n))
    high = np.maximum(opn, close) * (1 + np.abs(rng.normal(0, 0.003, n)))
    low = np.minimum(opn, close) * (1 - np.abs(rng.normal(0, 0.003, n)))
    df = pd.DataFrame(
        {"Open": opn, "High": high, "Low": low, "Close": close,
         "Volume": rng.uniform(1e5, 1e7, n)},
        index=idx,
    )
    df.index.name = "Date"
    return df


def main():
    tmp = Path(tempfile.mkdtemp(prefix="verify_asof_"))

    from app.data.cache import SqliteCache
    from app.data.router import MarketData
    from app.data.sources.akshare_source import AKShareSource
    from app.data.sources.yfinance_source import YFinanceSource

    cache = SqliteCache(db_path=tmp / "cache.db")
    cache_patch = mock.patch("app.data.cache.get_cache", return_value=cache)
    router_patch = mock.patch("app.data.router.get_cache", return_value=cache)

    with cache_patch, router_patch:
        md = MarketData()
        hist = _synthetic_history("2023-01-01", "2023-12-31")

        # ── (a) get_quote(as_of)：≤as_of 最后一根真实 bar ──
        with mock.patch.object(MarketData, "_history_df", return_value=hist):
            q = md.get_quote("AAPL", as_of="2023-06-30")
        check("(a) as_of 行情 date ≤ 2023-06-30", str(q.get("date")) <= "2023-06-30",
              str(q.get("date")))
        check("(a) as_of 行情取最后一根", str(q.get("date")) == "2023-06-30",
              str(q.get("date")))
        check("(a) current_price 非空", q.get("current_price") is not None)
        v = q.get("vintage") or {}
        need = {"requested_as_of", "data_observation_date", "source",
                "is_point_in_time", "note"}
        check("(a) vintage 字段齐全", need <= set(v), str(sorted(set(v))))
        check("(a) vintage 点对点+锚定", v.get("is_point_in_time") is True
              and v.get("requested_as_of") == "2023-06-30"
              and v.get("data_observation_date") == "2023-06-30")

        # as_of 早于首根 bar → 清晰报错（不伪造）
        with mock.patch.object(MarketData, "_history_df", return_value=hist):
            try:
                md.get_quote("AAPL", as_of="2022-01-03")
                ok = False
            except ValueError:
                ok = True
        check("(a) as_of 早于数据 → 清晰报错", ok)

        # ── (b) get_history(as_of)：序列锚定 ≤as_of ──
        with mock.patch.object(MarketData, "_history_df", return_value=hist):
            df = md.get_history("AAPL", "1mo", as_of="2023-06-30")
            df_now = md.get_history("AAPL", "1mo")
        check("(b) history 全部 ≤ as_of", bool(len(df))
              and bool((df.index <= pd.Timestamp("2023-06-30")).all()),
              f"{len(df)} 根, 末根 {df.index[-1].date()}")
        check("(b) history vintage 附着", (df.attrs.get("vintage") or {}).get(
            "is_point_in_time") is True)
        check("(b) history 无 as_of 也带 vintage",
              (df_now.attrs.get("vintage") or {}).get("is_point_in_time") is True)

        # ── (c) 不同 as_of 命中不同缓存 key；end 透传数据源 ──
        calls: list = []

        def fake_history(self, ticker, period, as_of_dt):
            calls.append(as_of_dt)
            end = str(as_of_dt.date()) if as_of_dt is not None else "2023-12-31"
            return _synthetic_history("2023-01-01", end)

        with mock.patch.object(MarketData, "_history_df", fake_history):
            d1 = md.get_history("AAPL", "1mo", as_of="2023-01-31")
            d2 = md.get_history("AAPL", "1mo", as_of="2023-02-28")
            d1b = md.get_history("AAPL", "1mo", as_of="2023-01-31")  # 应命中缓存
        check("(c) 不同 as_of 各取一次", len(calls) == 2, f"实际取数 {len(calls)} 次")
        check("(c) 同 as_of 二次命中缓存",
              str(d1b.index[-1].date()) == "2023-01-31"
              and str(d2.index[-1].date()) == "2023-02-28")

        rec: dict = {}

        def fake_yf_fetch(ticker, start="", end=""):
            rec["start"], rec["end"] = start, end
            return _synthetic_history(start or "2020-01-01", end)

        with mock.patch.object(md._yf, "fetch_price_history", side_effect=fake_yf_fetch):
            md._history_df("AAPL", "1mo", pd.Timestamp("2023-06-30"))
        check("(c) yf as_of 透传 end", rec.get("end") == "2023-06-30", str(rec))

        rec2: dict = {}

        def fake_ak_fetch(code, start="", end=""):
            rec2["start"], rec2["end"] = start, end
            return _synthetic_history(start or "2020-01-01", end)

        with mock.patch.object(md._ak, "fetch_price_history", side_effect=fake_ak_fetch):
            md._history_df("600519.SS", "1mo", pd.Timestamp("2023-06-30"))
        check("(c) ak as_of 透传 end", rec2.get("end") == "2023-06-30", str(rec2))

        # ── (d) financials as_of → is_point_in_time=False + 明确 note ──
        with mock.patch.object(AKShareSource, "get_financial_indicators",
                               return_value={"roe": 12.0, "eps": 2.0}):
            fin = md.get_financials("600519.SS", as_of="2023-06-30")
            fin_now = md.get_financials("600519.SS")
        fv = fin.get("vintage") or {}
        check("(d) 财务 as_of is_point_in_time=False", fv.get("is_point_in_time") is False)
        check("(d) 财务 note 明示无法回退", "无法回退历史财务快照" in (fv.get("note") or ""),
              (fv.get("note") or "")[:60])
        check("(d) 财务 requested_as_of", fv.get("requested_as_of") == "2023-06-30")
        check("(d) 财务无 as_of 点对点",
              (fin_now.get("vintage") or {}).get("is_point_in_time") is True)

        with mock.patch.object(YFinanceSource, "get_financials",
                               return_value={"pe_ratio": 20.0}):
            fin_yf = md.get_financials("AAPL", as_of="2023-06-30")
        check("(d) yf 财务 as_of 同样标注",
              (fin_yf.get("vintage") or {}).get("is_point_in_time") is False)

        # ── (e) executor 层透传 vintage ──
        from app.agent.executor import get_financials as ex_fin
        from app.agent.executor import get_quote as ex_quote

        with mock.patch.object(MarketData, "_history_df", return_value=hist):
            q2 = ex_quote("AAPL", as_of="2023-06-30")
        check("(e) executor as_of quote 透传 vintage",
              "vintage" in q2 and (q2.get("vintage") or {}).get(
                  "is_point_in_time") is True and q2.get("date") == "2023-06-30")

        with mock.patch.object(AKShareSource, "get_financial_indicators",
                               return_value={"roe": 12.0}):
            fin2 = ex_fin("600519.SS", as_of="2023-06-30")
        check("(e) executor 财务 as_of 透传 vintage",
              (fin2.get("vintage") or {}).get("is_point_in_time") is False)

        # ── (f) backtest 联动 walk-forward 不泄漏 ──
        from agents.batch_runner import run_walk_forward

        hist_full = _synthetic_history("2023-01-01", "2024-12-31")
        cfg = {
            "symbol": "AAPL",
            "market": "us_stock",
            "start_date": "2023-01-01",
            "cutoff_date": "2024-06-30",
            "end_date": "2024-12-31",
            "data_source": "super_investment",
            "csv_path": None,
            "projection_agent": {"type": "mock"},
            "super_investment": {"project_path": str(SIA_ROOT)},
            "walk_forward": {"window": 200, "step": 60},
            "data": {"cache_dir": str(tmp / "wf_cache")},
            "report": {"out_dir": str(tmp / "wf_reports")},
        }
        with mock.patch.object(MarketData, "get_history", return_value=hist_full):
            br = run_walk_forward(cfg)
        check("(f) walk-forward 完成", br.mode == "walk_forward" and len(br.runs) >= 1,
              f"{len(br.runs)} 期")
        check("(f) 无 lookahead_risk", all(not r.lookahead_risk for r in br.runs))
        check("(f) vintage 记入 warnings",
              any("vintage" in w for w in br.warnings), str(br.warnings[:2]))
        check("(f) 每期 cutoff 严格 < test 起点",
              all(r.cutoff_date < r.test_start for r in br.runs))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
