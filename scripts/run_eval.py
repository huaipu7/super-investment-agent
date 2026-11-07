"""五层评测统一入口。

L0–L3 全部离线跑在已落盘的产物上：零 LLM 调用、可复现、秒级。L4（端到端）
要真实跑管线，只在显式要求且确认时才跑，默认不碰。

用法:
    run_eval.py                       # 默认 l0,l1,l2,l3
    run_eval.py --tier l0,l3          # 只跑指定层
    run_eval.py --tier all --confirm-l4   # 含 l4（真实 LLM，需显式确认）
    run_eval.py --json                # 写 data/eval/latest.json
    run_eval.py --save-baseline       # 用本次结果生成/覆盖基线
    run_eval.py --baseline            # 与基线对比，指标退化即失败

基线解决的是「数字会变，但没人知道」：每次改代码跑一遍，任何一层指标掉出
容差就报出来，并指明是哪一层、哪个指标、掉了多少。

退出码: 0 全通过 / 1 有层失败或基线漂移 / 2 用法错误
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

EVAL_DIR = ROOT / "data" / "eval"
LATEST_PATH = EVAL_DIR / "latest.json"
BASELINE_PATH = EVAL_DIR / "baseline.json"

TIERS = ("l0", "l1", "l2", "l3")
# 比率类指标允许的绝对下滑；超出即视为退化
RATE_TOLERANCE = 0.02
# 必须严格不增长的计数类指标
MUST_NOT_GROW = ("unknown_count",)
# 名字不以 _rate 结尾、但同样是「越小越差」的比率指标（L2 检索质量）。
# 漏掉这组的话，召回率从 0.88 掉到 0.50 基线也不会报警。
RATIO_METRICS = ("mean_ndcg", "mean_recall", "mrr")
# 构成比：各出处（computed/source/llm/imputed/degraded）在事实总数里的占比，
# 五项恒加起来为 1。产物集合一变，分母就变——多采一份带两千条 source 事实的
# 产物，computed_rate 必然被稀释，而逐产物检查全过、unknown_count=0。
# 它度量的是「这批产物由什么组成」，不是「这套代码产出的东西变差了」，
# 所以只在产物集合与基线一致时才拿来比。
COMPOSITION_METRICS = ("computed_rate", "source_rate", "llm_rate",
                       "imputed_rate", "degraded_rate")


def _runner(tier: str):
    """按层取脚本模块。延迟导入：只跑 l2 时不必加载台账与报告渲染。"""
    if tier == "l0":
        import eval_l0_ledger
        return eval_l0_ledger
    if tier == "l1":
        import eval_l1_grounding
        return eval_l1_grounding
    if tier == "l2":
        import eval_retrieval
        return eval_retrieval
    if tier == "l3":
        import eval_l3_generation
        return eval_l3_generation
    if tier == "l4":
        import eval_l4_e2e
        return eval_l4_e2e
    raise KeyError(tier)


def _l2_snapshot() -> Path | None:
    """L2 的考题（人工标注）是对着冻结快照标的，指标基线也在快照上。

    拿正式库跑 L2 等于换了考题：外部语料 2 万多条切片会把标注里的相关文档挤出
    top-4，nDCG/召回必然大跌——那不是退化，是口径错配。所以默认认快照。
    """
    lock = EVAL_DIR / "l2_v1.lock.json"
    if not lock.exists():
        return None
    try:
        dst = (json.loads(lock.read_text(encoding="utf-8")) or {}).get("dst")
    except Exception:
        return None
    if not dst:
        return None
    path = Path(dst)
    path = path if path.is_absolute() else ROOT / path
    return path if path.exists() else None


def run_tier(tier: str) -> dict:
    module = _runner(tier)
    if tier == "l2":
        db = _l2_snapshot()
        if db is None:
            print("    !! 找不到 L2 冻结快照（data/eval/l2_v1.lock.json 或目录缺），"
                  "改跑正式库——结果与基线不可比")
            report = module.run()
        else:
            report = module.run(db=db)
            print(f"    语料: {report.get('db')}"
                  f"（{report.get('corpus_total')} 切片，只读快照）")
    else:
        report = module.run()
    # l0/l1/l3 有落盘报告，l2 也有；统一保存，UI 直接读
    if hasattr(module, "save"):
        module.save(report)
    return report


def _artifact_hashes() -> dict[str, str]:
    """产物指纹：产物变了，指标漂移属预期，比对时要说清楚。"""
    from app.trust.ledger import content_hash

    out: dict[str, str] = {}
    root = ROOT / "data" / "artifacts"
    if not root.exists():
        return out
    for p in sorted(root.glob("**/*.json")):
        if p.name.startswith("_"):
            continue
        try:
            out[str(p.relative_to(ROOT))] = content_hash(
                json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _meta() -> dict:
    from app.config import get_settings
    from app.trust.ledger import git_sha

    return {"git_sha": git_sha(), "model": get_settings().LLM_MODEL}


def _compare(reports: dict[str, dict], baseline: dict,
             skip_metrics: frozenset[str] = frozenset()) -> list[dict]:
    """逐层逐指标比对，返回漂移清单（空 = 没退化）。

    `skip_metrics` 里的指标不参与比对，由调用方在产物集合变动时填入构成比。
    跳过不等于放过：逐产物检查（`_new_reds`）与 `unknown_count` 照旧比，
    质量退化仍然报得出来。
    """
    drifts: list[dict] = []
    base_tiers = baseline.get("tiers") or {}
    for tier in TIERS:
        if tier not in reports:
            continue
        base_tier = base_tiers.get(tier) or {}
        # 层状态本身也要比：只看指标的话，基线建在 FAIL 上时再退一步也无人报警。
        base_status = base_tier.get("status")
        cur_status = reports[tier].get("status")
        if base_status == "pass" and cur_status and cur_status != "pass":
            drifts.append({"tier": tier, "metric": "layer_status", "kind": "status",
                           "baseline": base_status, "current": cur_status,
                           "delta": None, "rule": "层状态由 PASS 退步"})
        base = base_tier.get("metrics") or {}
        cur = reports[tier].get("metrics") or {}
        for key, old in base.items():
            if key in skip_metrics:
                continue
            if not isinstance(old, (int, float)) or key not in cur:
                continue
            new = cur[key]
            if not isinstance(new, (int, float)):
                continue
            delta = round(new - old, 6)
            if key in MUST_NOT_GROW:
                if new > old:
                    drifts.append({"tier": tier, "metric": key, "baseline": old,
                                   "current": new, "delta": delta,
                                   "rule": "不得增长"})
            elif key.endswith("_rate") or key in RATIO_METRICS:
                if new < old - RATE_TOLERANCE:
                    drifts.append({"tier": tier, "metric": key, "baseline": old,
                                   "current": new, "delta": delta,
                                   "rule": f"下滑超过 {RATE_TOLERANCE}"})
    return drifts


def _failing_names(report: dict) -> list[dict]:
    """报告里所有没过的检查项（含按产物嵌套的），只留名字与出处，便于写进基线。"""
    out: list[dict] = []

    def _scan(checks, where: str = "") -> None:
        for c in checks or []:
            if not c.get("passed"):
                out.append({"where": where, "name": c.get("name"),
                            "detail": c.get("detail")})

    _scan(report.get("checks"))
    for a in report.get("artifacts") or []:
        if a.get("status") != "pass":
            _scan(a.get("checks"), str(a.get("artifact") or ""))
    return out


def _red_key(tier: str, item: dict) -> tuple:
    """已知红的比对键。

    L1 每标的只评最新一份产物，重采一次文件名就变了；按文件名比会把「同一个标的
    的同一个老毛病」当成新红，于是每次重采都要重录基线。L1 按标的目录比才对应它
    实际度量的对象。L0 评的是全部产物，必须按文件名比，否则同标的的第二份产物
    犯同样的错会被前一份的已知红遮住。
    """
    where = str(item.get("where") or "").replace("\\", "/")
    if tier == "l1" and "/" in where:
        where = where.rsplit("/", 1)[0]
    return where, item.get("name")


def _new_reds(reports: dict[str, dict], baseline: dict) -> list[dict]:
    """当前新出现的红，排除基线 `known_reds` 里已记录的那些。

    只比 (出处, 检查名)，不比 detail——detail 里带数值，指标在容差内小幅波动
    也会改字面，那不是新红。
    """
    base_reds = baseline.get("known_reds") or {}
    out: list[dict] = []
    for tier, r in reports.items():
        cur = {_red_key(tier, i) for i in _failing_names(r)}
        base = {_red_key(tier, i) for i in (base_reds.get(tier) or [])}
        for where, name in sorted(cur - base, key=lambda x: (str(x[0]), str(x[1]))):
            out.append({"tier": tier, "kind": "new_red", "where": where or "",
                        "metric": name})
    return out


def _baseline_changed_artifacts(baseline: dict) -> list[str]:
    old = baseline.get("artifacts") or {}
    new = _artifact_hashes()
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def _artifact_set_delta(baseline: dict) -> dict:
    """产物路径集合相对基线的增删。

    只认路径集合，不认内容指纹：重采一次产物会写进新文件名，集合变了；而同一批
    产物里某个数变真（利率从回退常量换成实测值），分母没变，构成比照样可比。
    """
    old = set(baseline.get("artifacts") or {})
    new = set(_artifact_hashes())
    # 老基线没记指纹就判断不了，按「没变」处理：宁可多比，不可漏比。
    # 此时也不列增删——old 为空会把全部产物说成「新增」，误导读报告的人。
    if not old:
        return {"changed": False, "added": [], "removed": []}
    return {"changed": old != new,
            "added": sorted(new - old), "removed": sorted(old - new)}


def _print_tier(report: dict) -> None:
    tier = report.get("tier", "?")
    status = report.get("status", "fail").upper()
    print(f"[{tier}] {report.get('title', '')}  {status}  "
          f"{report.get('checks_passed', 0)}/{report.get('checks_total', 0)}")
    metrics = report.get("metrics") or {}
    shown = ", ".join(f"{k}={v}" for k, v in metrics.items())
    if shown:
        print(f"    {shown}")
    if report.get("recall_saturated"):
        print("    注: 检索召回率已饱和（k 覆盖整个语料），该值不度量任何东西")
    if report.get("labels") == "synthetic":
        print(f"    注: {report.get('note', '标注为自造夹具')}")
    for a in report.get("artifacts") or []:
        if a.get("status") != "pass":
            print(f"    FAIL {a['artifact']}")
            for c in a.get("checks", []):
                if not c["passed"]:
                    print(f"      !! {c['name']}  ({c['detail']})")
    for c in report.get("checks") or []:
        if not c["passed"]:
            print(f"    !! {c['name']}  ({c['detail']})")


def main() -> int:
    ap = argparse.ArgumentParser(description="五层评测统一入口")
    ap.add_argument("--tier", default="l0,l1,l2,l3",
                    help="逗号分隔，或 all（含 l4）")
    ap.add_argument("--json", action="store_true", help="写 data/eval/latest.json")
    ap.add_argument("--baseline", action="store_true", help="与基线比对")
    ap.add_argument("--save-baseline", action="store_true", help="用本次结果生成基线")
    ap.add_argument("--confirm-l4", action="store_true",
                    help="确认执行 L4（真实 LLM，5–10 分钟，产生 API 费用）")
    args = ap.parse_args()

    wanted = [t.strip().lower() for t in args.tier.split(",") if t.strip()]
    if wanted == ["all"]:
        wanted = list(TIERS) + ["l4"]
    unknown = [t for t in wanted if t not in TIERS and t != "l4"]
    if unknown:
        print(f"未知的层: {unknown}，可选 {list(TIERS)} / all")
        return 2
    if "l4" in wanted and not args.confirm_l4:
        print("L4 端到端要真实调用 LLM 跑完整管线（三支标的，5–10 分钟），")
        print("不在离线门禁内。确认执行请加 --confirm-l4，或直接跑")
        print("    scripts/eval_l4_e2e.py --yes")
        return 2

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    reports: dict[str, dict] = {}
    for tier in wanted:
        try:
            reports[tier] = run_tier(tier)
        except Exception as e:  # 单层崩了不能拖垮整轮汇总
            reports[tier] = {"tier": tier, "title": tier, "status": "error",
                             "checks_passed": 0, "checks_total": 0,
                             "metrics": {}, "error": f"{type(e).__name__}: {e}"}

    print("=" * 64)
    print("SIA 五层评测")
    print("=" * 64)
    for tier in wanted:
        _print_tier(reports[tier])
        if reports[tier].get("error"):
            print(f"    错误: {reports[tier]['error']}")

    aggregate = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tier_requested": wanted,
        **_meta(),
        "tiers": {t: {"status": r.get("status"),
                      "checks_passed": r.get("checks_passed"),
                      "checks_total": r.get("checks_total"),
                      "metrics": r.get("metrics")} for t, r in reports.items()},
        "status": ("pass" if all(r.get("status") == "pass" for r in reports.values())
                   else "fail"),
    }

    if args.json:
        LATEST_PATH.write_text(
            json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写 {LATEST_PATH.relative_to(ROOT)}")

    if args.save_baseline:
        baseline = {
            "version": 1,
            "created_at": aggregate["generated_at"],
            "git_sha": aggregate["git_sha"],
            "model": aggregate["model"],
            "artifacts": _artifact_hashes(),
            "rate_tolerance": RATE_TOLERANCE,
            # L4 是一次性端到端基线，耗时与 token 受当天网络限流影响，
            # 进回归基线只会制造假告警——只存离线四层。
            "tiers": {t: v for t, v in aggregate["tiers"].items() if t in TIERS},
            # 重录基线时如实记下「建基线那一刻就没过的项」，否则下一个人无法区分
            # 「基线本来如此」和「有人悄悄把失败录成基线」。
            "known_reds": {t: _failing_names(r) for t, r in reports.items()
                           if r.get("status") != "pass"},
        }
        BASELINE_PATH.write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"基线已写 {BASELINE_PATH.relative_to(ROOT)}")

    ok = aggregate["status"] == "pass"

    if args.baseline:
        if not BASELINE_PATH.exists():
            print(f"\n没有基线：先跑一次 run_eval.py --save-baseline")
            return 2
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        print("\n" + "-" * 64)
        print(f"与基线比对（基线建于 {baseline.get('created_at')}，"
              f"git {baseline.get('git_sha')}）")
        reds = baseline.get("known_reds") or {}
        if reds:
            print("基线本身含已知红（建基线时即存在，视为接受）：")
            for t, items in reds.items():
                for it in items:
                    print(f"    [{t}] {it.get('name')}  {it.get('where') or ''}")
        changed = _baseline_changed_artifacts(baseline)
        if changed:
            print(f"产物已变动 {len(changed)} 份，指标漂移属预期，仍需人工确认:")
            for c in changed[:5]:
                print(f"    {c}")
        delta = _artifact_set_delta(baseline)
        skip = frozenset(COMPOSITION_METRICS) if delta["changed"] else frozenset()
        if skip:
            print(f"产物集合与基线不同（+{len(delta['added'])} / -{len(delta['removed'])} 份）："
                  f"跳过构成比 {', '.join(sorted(skip))}")
            print("    构成比的分母随产物集合变化，不是质量退化；"
                  "逐产物检查与 unknown_count 仍在比对")
        drifts = _compare(reports, baseline, skip_metrics=skip)
        new_reds = _new_reds(reports, baseline)
        if drifts:
            ok = False
            print("指标退化:")
            for d in drifts:
                if d.get("kind") == "status":
                    print(f"    [{d['tier']}] 层状态: {d['baseline']} → {d['current']}"
                          f"  {d['rule']}")
                else:
                    print(f"    [{d['tier']}] {d['metric']}: "
                          f"{d['baseline']} → {d['current']} ({d['delta']:+})  {d['rule']}")
        if new_reds:
            ok = False
            print("新增失败项（不在基线已知红里）:")
            for d in new_reds:
                print(f"    [{d['tier']}] {d['metric']}  {d['where']}")
        if not drifts and not new_reds:
            # 与基线比没有退化就是过了：已知红是录基线时就接受的，
            # 不能让它们把门禁永久钉在非 0 退出码上。
            ok = True
            print("无退化：所有受保护指标都在容差内")
            if aggregate["status"] != "pass":
                print("总体 FAIL 只来自基线已记录的已知红，不算新增问题")

    print("=" * 64)
    print(f"总体: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
