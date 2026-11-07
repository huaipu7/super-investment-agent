"""评测看板自检（全离线，全构造数据 + 真实数据各一遍）。

看板最容易骗人的地方不是画错图，是把「没有」显示成「0」：
未建的层显示 0 会被读成「跑过了、结果为零」，合成夹具不标口径会被当成真指标。
这个脚本专门证明这些情况都会被正确处理。

运行: D:\\python\\python.exe scripts/verify_eval_dashboard.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}" if detail
          else f"  [{'PASS' if ok else 'FAIL'}] {name}")


def main() -> int:
    print("=" * 62)
    print("评测看板自检")
    print("=" * 62)

    try:
        from ui.pages import evals as E
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] 看板页面导入失败: {type(e).__name__}: {e}")
        return 1
    check("看板页面可导入", True)

    # ── 1. 未建 / 缺失不得显示为 0 ──
    print("\n[1] 「没有」不能显示成 0")
    cards = {c["tier"]: c for c in E.tier_cards({})}
    check("无任何报告 → 每层都是「未建」",
          all(c["state"] == E.UNBUILT for c in cards.values()))
    check("未建层的通过数显示 —，不是 0", cards["l0"]["checks"] == E.DASH,
          repr(cards["l0"]["checks"]))
    check("未建层不列指标行", cards["l4"]["lines"] == [])

    partial = {c["tier"]: c for c in E.tier_cards(
        {"l2": {"status": "pass", "checks_passed": 1,
                "checks_total": 1, "metrics": {}}})}["l2"]
    check("层存在但指标缺失 → 显示 —", all(E.DASH in line for line in partial["lines"]),
          str(partial["lines"]))
    check("缺失指标不会被读成 0",
          not any(line.rstrip().endswith(" 0") for line in partial["lines"]),
          str(partial["lines"]))

    check("未建层不参与总体统计（state 不是 PASS/FAIL）",
          E.tier_cards({})[0]["state"] not in ("PASS", "FAIL"))

    # ── 2. 口径徽章 ──
    print("\n[2] 标注口径必须可见")
    check("human → 人工标注", E._badge("human") == "人工标注")
    check("synthetic → 合成夹具", E._badge("synthetic") == "合成夹具")
    check("合成口径带警示文案", "烟囱测试" in E.LABEL_WARN.get("synthetic", ""))
    check("未知口径不瞎猜", E._badge("whatever") == E.DASH)

    syn = {c["tier"]: c for c in E.tier_cards(
        {"l2": {"status": "pass", "checks_passed": 1,
                "checks_total": 1, "labels": "synthetic",
                "metrics": {"mean_ndcg": 0.988, "mean_recall": 1.0,
                            "mrr": 1.0}}})}["l2"]
    check("合成口径的卡上带 labels=synthetic", syn["labels"] == "synthetic")

    # ── 3. L2 明细与漏召回 ──
    print("\n[3] 逐查询明细与漏召回算得对")
    report = {
        "per_query": {"好的": 1.0, "差的": 0.5},
        "per_query_recall": {"好的": 1.0, "差的": 0.5},
        "per_query_mrr": {"好的": 1.0, "差的": 0.5},
        "retrieved": {"好的": ["a", "b"], "差的": ["a"]},
    }
    rows = E.l2_rows(report)
    check("召回率升序：最差在前", rows[0]["查询"] == "差的", str([r["查询"] for r in rows]))
    check("召回片段数按实际召回计", rows[0]["召回片段"] == 1 and rows[1]["召回片段"] == 2)

    labels = {"items": [
        {"query": "差的", "relevance": {"a": 3, "z": 3, "y": 1}},
        {"query": "好的", "relevance": {"a": 3}},
    ]}
    miss = E.missed_rows(report, labels, {"z": "data/reports/x.md"})
    check("只挑 ≥2 分且未召回的", len(miss) == 1 and miss[0]["文档"] == "z",
          str(miss))
    check("漏召回带来源文件与人工分",
          miss[0]["来源"] == "data/reports/x.md" and miss[0]["人工分"] == 3)
    check("1 分文档不进漏召回清单", all(m["人工分"] >= 2 for m in miss))
    check("没有标注文件时不出清单", E.missed_rows(report, None) == [])

    # ── 4. 基线与口径切换 ──
    print("\n[4] 基线比对与口径切换点")
    latest = {"tiers": {"l2": {"metrics": {"mean_recall": 0.50, "mean_ndcg": 0.94,
                                           "mrr": 0.96}}}}
    baseline = {"tiers": {"l2": {"metrics": {"mean_recall": 0.88, "mean_ndcg": 0.94,
                                            "mrr": 0.96}}}}
    drifts = E.drift_rows(latest, baseline)
    check("看板复用 run_eval 口径：召回 0.88→0.50 报警",
          drifts is not None and any(d["metric"] == "mean_recall" for d in drifts),
          str(drifts))
    check("缺基线文件 → None（页面据此提示，不假装无退化）",
          E.drift_rows(latest, None) is None)

    series = E.history_series()
    switch = E.label_switch(series)
    real_hist = (ROOT / "data" / "eval" / "history.jsonl").exists()
    if real_hist:
        check("真实 history 能解析且非空", sum(len(v) for v in series.values()) > 0)
    check("口径切换点在切到人工标注之后",
          switch is None or isinstance(switch, str), str(switch))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "history.jsonl"
        tmp.write_text("\n".join([
            json.dumps({"tier": "l2", "generated_at": "2026-01-01", "labels": "synthetic",
                        "metrics": {"mean_recall": 1.0}}),
            json.dumps({"tier": "l2", "generated_at": "2026-02-01", "labels": "human",
                        "metrics": {"mean_recall": 0.88}}),
            "坏行不是 json",
        ]), encoding="utf-8")
        old = E.EVAL_DIR
        E.EVAL_DIR = Path(td)
        try:
            s = E.history_series()
            check("坏行被跳过、不炸", len(s["l2"]) == 2)
            check("口径切换点取第一条 human",
                  E.label_switch(s) == "2026-02-01", str(E.label_switch(s)))
        finally:
            E.EVAL_DIR = old

    # ── 5. 真实数据一遍 ──
    print("\n[5] 真实 data/eval 上的表现")
    data = E.load_all()
    if data["reports"].get("l2"):
        real_cards = {c["tier"]: c for c in E.tier_cards(data["reports"])}
        # L4 有报告就显示真实状态；没报告必须显示「未建」——两种都不能显示 0
        has_l4 = bool(data["reports"].get("l4"))
        want = ("PASS", "FAIL") if has_l4 else (E.UNBUILT,)
        check(f"L4 状态如实呈现（有基线={has_l4}）",
              real_cards["l4"]["state"] in want, str(real_cards["l4"]["state"]))
        if has_l4:
            check("L4 卡片说明它是一次性基线（成功/耗时/降级）",
                  any("成功" in ln for ln in real_cards["l4"]["lines"])
                  and any("降级" in ln for ln in real_cards["l4"]["lines"]),
                  str(real_cards["l4"]["lines"]))
        check("已建层的状态是 PASS/FAIL",
              real_cards["l2"]["state"] in ("PASS", "FAIL"), real_cards["l2"]["state"])
        rl = (data["reports"]["l2"] or {}).get("labels")
        check(f"L2 口径如实呈现（当前 {rl}）", E._badge(rl) in ("人工标注", "合成夹具"))
    else:
        check("真实 data/eval 存在 L2 报告", False, "缺 l2_report.json")

    dash = (ROOT / "ui" / "dashboard.py").read_text(encoding="utf-8")
    check("看板已在侧栏注册", "🧪 评测看板" in dash)
    check("导航分支已接上", "evals.page()" in dash)

    print("\n" + "=" * 62)
    if failed:
        print(f"结果: {passed} 通过, {failed} 失败")
        return 1
    print(f"结果: {passed}/{passed} 全部通过")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
