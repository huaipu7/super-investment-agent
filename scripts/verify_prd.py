"""PRD 自检 —— 证明 PRD 不是文档摆设，而是评测的目录。

PRD 最容易坏在看不见的地方：
  · 写了需求，但验收标准是「体验流畅」这类无法判定的话
  · 需求引用了断言，但断言名与实际评测报告对不上（改了名文档没跟）
  · 有一条 L 层断言，找不到任何需求归口（说明需求写漏了）
  · 文档里的数字与 data/eval/*.json 不一致（手抄的旧值）
  · 把「单用户 / 样本个位数」的边界含糊掉，读起来像生产系统

这个脚本逐条证明这些情况不会静默发生。

运行: D:\\python\\python.exe scripts/verify_prd.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
PRD = ROOT / "docs" / "PRD_SIA_v2.md"
README = ROOT / "README.md"
EVAL_DIR = ROOT / "data" / "eval"

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}" if detail
          else f"  [{'PASS' if ok else 'FAIL'}] {name}")


def _report(tier: str) -> dict:
    path = EVAL_DIR / f"{tier}_report.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _assertion_names(tier: str, report: dict) -> set[str]:
    """报告中出现过的断言名（顶层 checks + 各产物 checks）。"""
    names: set[str] = set()
    for c in report.get("checks") or []:
        if c.get("name"):
            names.add(c["name"])
    for a in report.get("artifacts") or []:
        for c in a.get("checks") or []:
            if c.get("name"):
                names.add(c["name"])
    return names


def main() -> int:
    print("=" * 64)
    print("PRD 自检")
    print("=" * 64)

    if not PRD.exists():
        print(f"  [FAIL] 缺少 {PRD.relative_to(ROOT)}")
        return 1
    text = PRD.read_text(encoding="utf-8")

    # ── 1. 结构 ──
    print("\n[1] 九个章节与附录")
    for i in range(1, 10):
        check(f"第 {i} 章存在", re.search(rf"^## {i}\.", text, re.M) is not None)
    check("附录 A（FR↔断言映射）", "## 附录 A" in text)
    check("附录 B（术语）", "## 附录 B" in text)

    # ── 2. 需求块与验收钩子 ──
    print("\n[2] 每条需求都挂可执行断言")
    blocks = re.findall(r"#### (FR-\d+)(.*?)(?=\n#### |\n## |\Z)", text, re.S)
    check("需求条数 ≥ 10", len(blocks) >= 10, str(len(blocks)))
    no_accept = [fr for fr, body in blocks if "验收" not in body]
    check("每条需求都有「验收」段落", not no_accept, str(no_accept))
    no_layer = [fr for fr, body in blocks
                if not re.search(r"L[0-4]|P5|门禁", body)]
    check("每条需求都标了对应层", not no_layer, str(no_layer))

    # ── 3. 断言名与报告逐字一致（正向）──
    print("\n[3] 需求引用的断言名与评测报告逐字一致")
    reports = {t: _report(t) for t in ("l0", "l1", "l2", "l3", "l4")}
    by_layer = {t: _assertion_names(t, r) for t, r in reports.items()}
    all_names = set().union(*by_layer.values())
    check("能从报告中取到断言清单", len(all_names) >= 20, f"{len(all_names)} 条")

    for fr, body in blocks:
        # 门禁 / 飞轮类需求的验收物是守卫脚本，不是 L 层断言
        if "验收（门禁" in body:
            ok = "verify_evals.py" in body and (ROOT / "scripts" / "verify_evals.py").exists()
            check(f"{fr} 挂了门禁守卫脚本", ok)
            continue
        if "验收（P5" in body:
            ok = ("verify_feedback.py" in body
                  and (ROOT / "scripts" / "verify_feedback.py").exists())
            check(f"{fr} 挂了飞轮守卫脚本", ok)
            continue
        hits = [n for n in all_names if n in body]
        check(f"{fr} 引用了真实断言", bool(hits), f"{len(hits)} 条命中")

    # ── 4. 反向覆盖：没有无主断言 ──
    print("\n[4] 反向覆盖：每条断言都有需求归口")
    orphans: dict[str, list[str]] = {}
    for tier, names in by_layer.items():
        miss = sorted(n for n in names if n not in text)
        if miss:
            orphans[tier] = miss
    check("全部 L 层断言都在 PRD 中出现", not orphans, str(orphans))

    # ── 5. 数字与报告一致（不许手抄旧值）──
    print("\n[5] 文档数字取自真实评测报告")
    l0 = reports["l0"].get("metrics") or {}
    l1 = reports["l1"].get("metrics") or {}
    l2 = reports["l2"].get("metrics") or {}
    expects = {
        "L0 事实总数": (str(l0.get("fact_total")), f"fact_total={l0.get('fact_total')}"),
        "L0 unknown=0": (f"`unknown_count = {l0.get('unknown_count')}`",
                         f"unknown={l0.get('unknown_count')}"),
        "L0 降级率": (str(l0.get("degraded_rate")), "degraded_rate"),
        "L0 插补率": (str(l0.get("imputed_rate")), "imputed_rate"),
        "L0 as_of 覆盖": (str(l0.get("as_of_coverage")), "as_of_coverage"),
        "L1 接地率": (str(l1.get("grounding_rate")), "grounding_rate"),
        "L2 nDCG": (str(l2.get("mean_ndcg")), "mean_ndcg"),
        "L2 recall": (str(l2.get("mean_recall")), "mean_recall"),
        "L2 MRR": (str(l2.get("mrr")), "mrr"),
    }
    # 文档里按可读习惯写千分位（24,877），报告里是裸数字（24877）——比对前去逗号，
    # 只消除排版差异，不放宽「数值必须一致」这件事。
    text_num = text.replace(",", "")
    for label, (needle, detail) in expects.items():
        check(f"{label} 与报告一致", needle in text_num, detail)
    for tier in ("l0", "l1", "l2", "l3"):
        rep = reports[tier]
        frac = f"{rep.get('checks_passed')}/{rep.get('checks_total')}"
        check(f"{tier.upper()} 断言数与报告一致", frac in text, frac)
    check("L1 未达标时如实标注",
          (reports["l1"].get("status") == "pass") or ("未达标" in text))
    check("北极星分母与磁盘上的研报数一致",
          str(len(list((ROOT / "data" / "reports").glob("*.pdf")))) in text,
          f"{len(list((ROOT / 'data' / 'reports').glob('*.pdf')))} 份 PDF")

    # ── 6. 非目标写死（否则范围会悄悄膨胀）──
    print("\n[6] 非目标明确")
    for kw in ("多用户", "权限", "部署", "移动端", "CI", "交易执行"):
        check(f"非目标含「{kw}」", kw in text)

    # ── 7. 诚实边界 ──
    print("\n[7] 诚实边界写在文档里")
    check("声明单用户、样本个位数", "单用户" in text and "个位数量级" in text)
    check("不声称飞轮带来提升", "不声称「飞轮带来" in text)
    check("L2 语料口径（自生成研报 / 只读快照）",
          "1,907" in text and "knowledge_db_v1" in text and "不是外部权威语料" in text)
    check("L2 召回为池化下界", "池化下界" in text)
    check("L1 门槛为何不是 1.0 有说明", "阈值为何不是 1.0" in text)
    check("SOTP 缺失区块缺口已登记", "已知缺口" in text and "SOTP" in text)
    check("插补不进报告正文的缺口已登记",
          "插补" in text and "报告正文没有" in text)

    # ── 8. L4 一次性基线 ──
    print("\n[8] L4 端到端基线")
    l4 = reports["l4"]
    check("L4 报告存在", bool(l4), "data/eval/l4_report.json")
    if l4:
        check("L4 通过", l4.get("status") == "pass", str(l4.get("status")))
        names = by_layer["l4"]
        doc_names = {"全链路成功 3/3", "降级均可解释", "台账闭合（L4 产物复跑 L0）",
                     "耗时与 token 完整记录"}
        check("L4 四条断言名与 PRD 逐字一致", names == doc_names, str(sorted(names)))
        check("L4 不进日常门禁",
              l4.get("in_daily_gate") is False and l4.get("baseline") is True)
        hist = l4.get("criterion_history") or []
        check("判据变更可追溯（v1 记为未通过）",
              len(hist) >= 2 and hist[0].get("version") == 1
              and hist[0].get("passed") is False, str(hist[:1]))
        check("判据 v2 下降级仍被计数而非抹掉",
              (l4.get("metrics") or {}).get("degraded_facts", 0) > 0,
              f"degraded_facts={(l4.get('metrics') or {}).get('degraded_facts')}")
        m4 = l4.get("metrics") or {}
        check("L4 成功了 3/3", m4.get("success_rate") == 1.0,
              f"success_rate={m4.get('success_rate')}")
        check("L4 记录了 token 与耗时",
              bool(m4.get("tokens_in_total")) and bool(m4.get("wall_seconds_mean")),
              f"in={m4.get('tokens_in_total')} out={m4.get('tokens_out_total')} "
              f"mean_wall={m4.get('wall_seconds_mean')}s")

    # ── 9. README 指向 PRD ──
    print("\n[9] README 已更新")
    if README.exists():
        rd = README.read_text(encoding="utf-8")
        check("README 链接到 PRD", "docs/PRD_SIA_v2.md" in rd)
        check("README 有五层评测说明", "L0" in rd and "L1" in rd and "L4" in rd)
        check("README 提到数据飞轮", "飞轮" in rd)
        check("README 给出评测命令", "run_eval.py" in rd)
    else:
        check("README 存在", False)

    print("\n" + "=" * 64)
    if failed:
        print(f"结果: {passed} 通过, {failed} 失败")
        return 1
    print(f"结果: {passed}/{passed} 全部通过")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
