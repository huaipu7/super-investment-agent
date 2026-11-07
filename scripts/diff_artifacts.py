"""事实级跨 run 对比 CLI：同一标的这一次和上一次，哪个数字变了。

按「路径 + 期间」对齐两份产物，输出五类变化：新增 / 消失 / 改值 / 换源 / 降级反转。
改值再按幅度分档（noise / minor / material），避免 float32 往返噪声淹没真信号。

用法:
  D:\\python\\python.exe scripts/diff_artifacts.py --ticker 600519.SS
  D:\\python\\python.exe scripts/diff_artifacts.py --ticker AAPL --material-only
  D:\\python\\python.exe scripts/diff_artifacts.py --old data/artifacts/... --new data/artifacts/...
  D:\\python\\python.exe scripts/diff_artifacts.py --ticker 600519.SS --json > diff.json

纯只读：不写产物、不写台账、不联网。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.trust.factdiff import diff_artifacts  # noqa: E402

OUT_DIR = ROOT / "data" / "artifacts"


def _recent(ticker: str, limit: int = 2) -> list[Path]:
    d = OUT_DIR / ticker.upper().replace(".", "_")
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if v != 0 and abs(v) >= 1e9:
            return f"{v:,.0f}"
        if abs(v) < 1e-4 and v != 0:
            return f"{v:.4e}"
        return f"{v:,.6g}"
    return str(v)


def _rows(entries: list[dict], limit: int) -> list[str]:
    lines = ["| 字段 | 旧值 | 新值 | 幅度 | 出处 |", "|---|---|---:|---:|---|"]
    for e in entries[:limit]:
        pct = "—" if e["rel_delta"] is None else f"{e['rel_delta'] * 100:.2f}%"
        prov = e["provenance_old"] or "—"
        if e["provenance_old"] != e["provenance_new"]:
            prov = f"{prov} → {e['provenance_new'] or '—'}"
        lines.append(f"| `{e['key']}` | {_fmt(e['old_value'])} | "
                     f"{_fmt(e['new_value'])} | {pct} | {prov} |")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="事实级跨 run 对比")
    ap.add_argument("--ticker", default="", help="标的；默认取该标的最新两份产物")
    ap.add_argument("--old", default="", help="旧产物路径")
    ap.add_argument("--new", default="", help="新产物路径")
    ap.add_argument("--material-only", action="store_true",
                    help="改值只列 material（相对幅度 ≥ 1%%）")
    ap.add_argument("--limit", type=int, default=20, help="每类最多列多少条")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    ap.add_argument("--out", default="", help="把 Markdown 报告写到文件")
    args = ap.parse_args()

    if args.old and args.new:
        old_p, new_p = Path(args.old), Path(args.new)
    else:
        if not args.ticker:
            print("[FAIL] 需要 --ticker，或同时给 --old 与 --new")
            return 1
        files = _recent(args.ticker)
        if len(files) < 2:
            print(f"[FAIL] {args.ticker} 的产物不足 2 份，无法对比")
            return 1
        new_p, old_p = files[0], files[1]

    if not (old_p.is_file() and new_p.is_file()):
        print(f"[FAIL] 产物不存在: {old_p} / {new_p}")
        return 1

    diff = diff_artifacts(old_p, new_p)

    if args.json:
        print(json.dumps(diff, ensure_ascii=False, indent=2, default=str))
        return 0

    s = diff["summary"]
    ch = diff["changes"]
    shown = [e for e in ch["value_changed"]
             if e["class"] == "material"] if args.material_only else ch["value_changed"]

    lines = [
        f"# 事实级对比 · {args.ticker or new_p.parent.name}",
        "",
        f"- 旧：`{old_p.relative_to(ROOT).as_posix()}` run_id={diff['old']['run_id']} "
        f"事实 {diff['old']['fact_total']}",
        f"- 新：`{new_p.relative_to(ROOT).as_posix()}` run_id={diff['new']['run_id']} "
        f"事实 {diff['new']['fact_total']}",
        "",
        "| 新增 | 消失 | 改值 | 其中实质 | 换源 | 降级反转 |",
        "|---:|---:|---:|---:|---:|---:|",
        f"| {s['added']} | {s['removed']} | {s['value_changed']} | "
        f"{s['material']} | {s['reprovenanced']} | {s['degraded_flip']} |",
        "",
        f"改值幅度分布：material {s['material']} · minor {s['minor']} · "
        f"noise {s['noise']} · 非有限 {s['undefined']}",
    ]
    if shown:
        lines += ["", f"## 改值（{'仅实质' if args.material_only else '全部'}，前 {args.limit} 条）", ""]
        lines += _rows(shown, args.limit)
    for key, title in (("added", "新增"), ("removed", "消失"),
                       ("reprovenanced", "换源"), ("degraded_flip", "降级反转")):
        if ch[key]:
            lines += ["", f"## {title}（前 {args.limit} 条）", ""]
            lines += _rows(ch[key], args.limit)

    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[OK] 写入 {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
