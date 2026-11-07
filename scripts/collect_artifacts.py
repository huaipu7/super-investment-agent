"""产物采集：真实跑一遍分析管线，把 result 原样落盘。

为什么要采：仓库里的历史产物 `phase5_full_aapl.json` 是早期版本留下的，缺
news / meeting / trade_plan 等 section——而 LLM 密集的恰好是这几块。没有真实
产物，L0 只能测到「确定性计算区」，L1 的接地检查也无从下手。

产物是评测的唯一事实来源。评测只在落盘的产物上做，不现跑管线：
  · 可复现——同一产物任何时候评出同一个结果
  · 零成本——L0~L3 全部离线，不烧 token
  · 可对比——产物内容哈希即 run_id，天然支持基线回归

用法:
  D:\\python\\python.exe scripts/collect_artifacts.py
  D:\\python\\python.exe scripts/collect_artifacts.py --tickers 600519.SS,0700.HK
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.trust import build_ledger  # noqa: E402

OUT_DIR = ROOT / "data" / "artifacts"

# 三支标的各走一条数据通路，覆盖尽量不同的取数分支
DEFAULT_TICKERS = [
    "600519.SS",  # A股：Sina → yfinance 双源降级；且是高毛利业务
    "AAPL",       # 美股：东财 F10 通路；与历史产物可比
    "0700.HK",    # 港股：第三方通路
]


def collect(ticker: str) -> dict | None:
    from app.analysis.pipeline import AnalysisOrchestrator

    print(f"\n{'=' * 64}")
    print(f"采集 {ticker}")
    print("=" * 64)

    last_pct = -1

    def on_progress(info: dict) -> None:
        nonlocal last_pct
        pct = int(info.get("pct") or 0)
        if pct == last_pct:
            return
        last_pct = pct
        label = info.get("label") or info.get("step") or ""
        print(f"  [{pct:>3}%] {label}")

    started = time.time()
    try:
        result = AnalysisOrchestrator().run(ticker, progress_cb=on_progress)
    except Exception as exc:  # 管线失败必须如实报，不产出半成品产物
        print(f"  [FAIL] 管线异常: {type(exc).__name__}: {exc}")
        return None
    elapsed = time.time() - started

    if not isinstance(result, dict) or not result:
        print("  [FAIL] 管线返回空结果")
        return None

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out = OUT_DIR / ticker.upper().replace(".", "_") / f"{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # allow_nan=False：管线已净化，这里再兜一道。真漏了非有限值就抛错，
    # 不写出一份 JSON.parse 打不开的产物。
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str,
                      allow_nan=False)
    out.write_text(text, encoding="utf-8")

    ledger = build_ledger(result, ticker=result.get("ticker") or ticker)
    sections = [k for k, v in result.items() if isinstance(v, (dict, list))]
    print(f"  [OK] 落盘 {out.relative_to(ROOT)}")
    print(f"       墙钟 {elapsed:.1f}s  管线自报 {result.get('pipeline_time')}s  "
          f"{len(sections)} 个 section")
    print(f"       台账 {ledger.total} 条  {ledger.by_provenance()}  "
          f"unknown={len(ledger.unknown())}")
    return {"ticker": ticker, "path": str(out), "facts": ledger.total,
            "sections": len(sections), "elapsed": round(elapsed, 1),
            "unknown": len(ledger.unknown())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS),
                    help="逗号分隔的标的列表")
    args = ap.parse_args()
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    print(f"采集 {len(tickers)} 支标的: {', '.join(tickers)}")
    print(f"产物目录: {OUT_DIR.relative_to(ROOT)}")

    ok: list[dict] = []
    failed: list[str] = []
    for t in tickers:
        rec = collect(t)
        (ok.append(rec) if rec else failed.append(t))

    print("\n" + "=" * 64)
    print(f"采集完成: 成功 {len(ok)} / {len(tickers)}")
    for r in ok:
        print(f"  {r['ticker']:<12} facts={r['facts']:<5} sections={r['sections']:<3} "
              f"{r['elapsed']}s")
    for t in failed:
        print(f"  {t:<12} 失败")
    print("=" * 64)

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
