"""增量刷新 CLI：复用上一份产物，只重取行情与由现价决定的结论，不调 LLM。

为什么：全量跑一次 3–4 分钟，几乎全花在新闻/会议/SWOT 这些一天内不会变的章节上。
每天要看的时候只需要新行情 + 新的技术面 + 新的估值空间。

用法:
  D:\\python\\python.exe scripts/refresh_artifact.py --ticker 600519.SS
  D:\\python\\python.exe scripts/refresh_artifact.py --ticker 600519.SS --dry-run
  D:\\python\\python.exe scripts/refresh_artifact.py --from data/artifacts/600519_SS/20261105T001635.json

退出码: 0 = 成功；1 = 失败（不写任何产物）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.analysis.refresh import (  # noqa: E402
    CARRIED_SECTIONS,
    RECOMPUTED,
    refresh_price,
)
from app.trust import build_ledger  # noqa: E402
from app.trust.factdiff import diff_facts  # noqa: E402

OUT_DIR = ROOT / "data" / "artifacts"


def _dir_for(ticker: str) -> Path:
    return OUT_DIR / ticker.upper().replace(".", "_")


def latest_artifact(ticker: str) -> Path | None:
    """该标的最新一份产物（按文件修改时间）。"""
    d = _dir_for(ticker)
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _print_plan(prev: dict, base: Path) -> None:
    print("  [dry-run] 不联网、不写盘")
    print(f"  基线: {base.relative_to(ROOT)}")
    print("  将重算:")
    for k in RECOMPUTED:
        print(f"    · {k}")
    print("  将沿用:")
    sections = ["financial_analysis", "financial_analysis.valuation_context.pe_percentile"]
    sections += [s for s in CARRIED_SECTIONS if s in prev]
    for s in sections:
        print(f"    · {s}")


def main() -> int:
    ap = argparse.ArgumentParser(description="行情增量刷新（不调 LLM）")
    ap.add_argument("--ticker", default="", help="标的；省略时从 --from 推断")
    ap.add_argument("--from", dest="src", default="", help="指定基线产物路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不联网不写盘")
    ap.add_argument("--no-write", action="store_true", help="计算但不落盘")
    ap.add_argument("--no-cache", action="store_true", help="不写入分析缓存")
    ap.add_argument("--quiet", action="store_true", help="只输出落盘路径")
    args = ap.parse_args()

    base: Path | None
    if args.src:
        base = (ROOT / args.src).resolve() if not Path(args.src).is_absolute() \
            else Path(args.src)
        if not base.is_file():
            print(f"[FAIL] 基线产物不存在: {args.src}")
            return 1
    else:
        if not args.ticker:
            print("[FAIL] 需要 --ticker 或 --from")
            return 1
        base = latest_artifact(args.ticker)
        if base is None:
            print(f"[FAIL] {args.ticker} 没有历史产物，先跑一次全量分析")
            return 1

    prev = _load(base)
    ticker = args.ticker or prev.get("ticker") or ""
    if not ticker:
        print("[FAIL] 基线产物缺少 ticker")
        return 1

    if not args.quiet:
        print(f"\n{'=' * 60}\n增量刷新 {ticker}（不调 LLM）\n{'=' * 60}")

    if args.dry_run:
        _print_plan(prev, base)
        return 0

    base_at = datetime.fromtimestamp(base.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        new = refresh_price(prev, base_ref=str(base.relative_to(ROOT).as_posix()),
                            base_generated_at=base_at)
    except Exception as e:
        print(f"  [FAIL] 刷新失败，未写任何产物: {type(e).__name__}: {e}")
        return 1

    r = new.get("refresh") or {}
    ledger = build_ledger(new, ticker=ticker)
    diff = diff_facts(prev, new)
    s = diff["summary"]

    if not args.quiet:
        print(f"  价格 {new['financial_analysis'].get('current_price')}"
              f"（{r.get('effective_price_date')}）  耗时 {r.get('elapsed')}")
        print(f"  重算 {len(r.get('recomputed') or [])} 项 / 沿用 "
              f"{len(r.get('carried_over') or [])} 项")
        print(f"  相对基线: 实质变化 {s['material']} / 微小 {s['minor']} / "
              f"噪声 {s['noise']}；新增 {s['added']} 消失 {s['removed']}")
        top = [e for e in diff["changes"]["value_changed"] if e["class"] == "material"][:5]
        for e in top:
            pct = "—" if e["rel_delta"] is None else f"{e['rel_delta'] * 100:.1f}%"
            print(f"    {pct:>8}  {e['key']}  {e['old_value']} → {e['new_value']}")
        print(f"  台账 {ledger.total} 条  unknown={len(ledger.unknown())}")

    if args.no_write:
        if not args.quiet:
            print("  [no-write] 未落盘")
        return 0

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out = _dir_for(ticker) / f"{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(new, ensure_ascii=False, indent=2, default=str,
                              allow_nan=False), encoding="utf-8")

    if not args.no_cache:
        try:
            from app.analysis.cache import set_cached_result
            set_cached_result(ticker, new)
        except Exception as e:
            print(f"  [warn] 写分析缓存失败（不影响产物）: {e}")

    print(f"  [OK] 落盘 {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
