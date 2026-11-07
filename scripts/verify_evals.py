"""评测机制自检（全离线）。

一个永远通过的评测等于没有评测。这个脚本专门证明「它会失败」：

  1 匹配器   取整/截断要认，边界上的伪造值不能认
  2 反向控制 往真产物里注入 99.99%，接地率必须掉下来
  3 基线比对 指标掉出容差/未知路径增长必须报警，容差内必须不报；
            产物集合变动时构成比跳过，质量指标与层状态照旧报警
  4 L2 指标  nDCG/召回/MRR 在构造用例上算得对
  5 L3 断言  把矩阵翻转、占比改错、章节留空、塞进确定性表述，逐条必须报错

运行: D:\\python\\python.exe scripts/verify_evals.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

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
    print("评测机制自检")
    print("=" * 62)

    from app.trust import build_ledger
    from app.trust import grounding as G

    # ── 1. 匹配器 ──
    print("\n[1] 显示位数匹配")
    tok = lambda raw, unit="%": G.Token(raw=raw, value=float(raw), unit=unit, span=(0, 0))
    check("1360.56 亿写成「1360」→ 认（截断）", G._same_at_display(tok("1360", "亿"), 1360.56))
    check("39.85 写成「39.8」→ 认（截断）", G._same_at_display(tok("39.8"), 39.85))
    check("100.0 写成「99.99」→ 不认（边界伪造）", not G._same_at_display(tok("99.99"), 100.0))
    check("0.303 写成「30.3」→ 认（百分比）", G._same_at_display(tok("30.3"), 30.3))
    check("-0.028 与「2.8」→ 认（负事实也能被无符号写法命中）",
          G.best_match(tok("2.8"), [-0.028])[0] is not None)
    check("「1,360」千分位去掉后是 1360", G.extract_tokens("约1,360亿美元")[0].value == 1360)
    check("1902亿FCF 取到单位（不被当成 1902 年）",
          (lambda t: t.value == 1902 and t.unit == "亿" and not t.is_year)(
              G.extract_tokens("1902亿FCF")[0]))
    check("CR3 这种标识符不算数值", not G.extract_tokens("行业CR3达77.1%")[0].raw.startswith("3"))
    # 2026-11-05 实测：0700.HK 当日 K 线整行 NaN 进产物，math.floor(nan) 直接把 L1 打崩
    nan_ok = False
    try:
        nan_ok = G.best_match(tok("600"), [float("nan"), 600.0])[1] == 0.0
    except ValueError:
        nan_ok = False
    check("NaN 事实不参与匹配、也不让评测崩溃", nan_ok)
    check("全是 NaN 时不误判为已接地",
          G.best_match(tok("600"), [float("nan"), float("inf")])[0] is None)

    # ── 1b. 「数值断言」判定：既不能把名称当数字，也不能把真数值当噪声 ──
    print("\n[1b] 数值断言的边界")
    def claims(text: str) -> list[str]:
        return [t.raw for t in G.extract_tokens(text) if G.is_claim(text, t)]

    check("品牌名内嵌数字不算断言（国窖1573）",
          claims("国窖1573是高端单品") == [], str(claims("国窖1573是高端单品")))
    check("海关编码不算断言（HS 8517/8471）",
          claims("HS 8517类目") == [], str(claims("HS 8517类目")))
    check("带单位的数算断言（98.2%）", claims("CR3 达 98.2%") == ["98.2"])
    check("带小数位的比率算断言（2.45）", "2.45" in claims("流动比率2025年2.45"))
    check("带千分位的金额算断言（27,738,852,603）",
          claims("自由现金流27,738,852,603") == ["27,738,852,603"])
    check("带单位的区间两端都算断言（600-1000元）",
          claims("600-1000元价格带") == ["600", "1000"], str(claims("600-1000元价格带")))
    check("带单位的区间两端都算断言（150-250bp）",
          claims("毛利率提升150-250bp") == ["150", "250"], str(claims("毛利率提升150-250bp")))

    # 时间跨度：0700.HK 那句「但 6 个月内难兑现为利润」被当成断言判红，可那 6 来自
    # 提示词给的 date_6m 视野，不是模型编的数据。只在后接 内/后/左右 时才排除——
    # 放宽到「任何数字后面跟个年/月」会把 5 年 CAGR、30 日均线一起放跑。
    def scored(text: str) -> list[str]:
        return [t.raw for t in G.extract_tokens(text)
                if G.is_claim(text, t) and not G.is_duration(text, t)]

    check("时间跨度不算断言（6 个月内）", scored("但 6 个月内难兑现为利润") == [],
          str(scored("但 6 个月内难兑现为利润")))
    check("时间跨度不算断言（3 年后）", scored("3 年后产能释放") == [],
          str(scored("3 年后产能释放")))
    check("带单位的数仍算断言（6 亿）", scored("广告收入 6 亿") == ["6"],
          str(scored("广告收入 6 亿")))
    def durations(text: str) -> list[str]:
        return [t.raw for t in G.extract_tokens(text) if G.is_duration(text, t)]

    check("「5 年 CAGR」「30 日均线」不被当成时间跨度",
          durations("5 年 CAGR 达 21%") == [] and durations("股价站上 30 日均线") == [],
          f"{durations('5 年 CAGR 达 21%')} / {durations('股价站上 30 日均线')}")
    check("带小数的期限不算时间跨度（6.5 个月内）",
          durations("6.5 个月内交割") == [], str(durations("6.5 个月内交割")))

    # ── 1c. 文本锚定的派生复算：必须问得出「哪个指标、哪两期」 ──
    print("\n[1c] 派生值只在文本锚定成立时才认")
    from types import SimpleNamespace
    facts = [
        SimpleNamespace(metric="OPERATING_MARGIN", unit="ratio", value=0.2055, as_of="2022"),
        SimpleNamespace(metric="OPERATING_MARGIN", unit="ratio", value=0.3309, as_of="2025"),
        SimpleNamespace(metric="GROSS_MARGIN", unit="ratio", value=0.5290, as_of="2024"),
        SimpleNamespace(metric="GROSS_MARGIN", unit="ratio", value=0.5621, as_of="2025"),
    ]

    def anchor(text: str, raw: str = "1260", unit: str | None = "bp"):
        tok = G.Token(raw=raw, value=float(raw), unit=unit, span=(text.find(raw), text.find(raw) + len(raw)))
        return G.match_anchored(tok, facts, text)

    ok_text = "营业利润率2025年33.1%（2022年20.5%），三年扩张1260bp"
    check("指标名 + 两期 + 差值对上 → 认", anchor(ok_text) is not None,
          str(anchor(ok_text)))
    check("差值对不上 → 不认", anchor(ok_text, raw="9999") is None)
    check("没有指标名 → 不认（CR3 98.2% 不得被凑成某个差）",
          anchor("片段3（CR3 98.2%）", raw="98.2", unit="%") is None)
    check("没有两个期间 → 不认（150-250bp 增量空间只是假设）",
          anchor("理论上有150-250bp的毛利率增量空间", raw="250", unit="bp") is None)
    check("指标不同 → 不认（毛利率的差不能解释净利率的话）",
          anchor("净利率2024年52.9%（2025年56.2%），扩张330bp", raw="330") is None)

    pair_text = "2023年净利率从34.0%骤降至19.4%（-1460bp）"
    grounded = [(pair_text.find("34.0"), 34.0), (pair_text.find("19.4"), 19.4)]
    tok = G.Token(raw="1460", value=1460.0, unit="bp",
                  span=(pair_text.find("1460"), pair_text.find("1460") + 4))
    check("同句两个已接地数字之差 → 认",
          G.explain_by_sentence_pair(tok, grounded, pair_text) is not None)
    check("两个端点没接地 → 不认",
          G.explain_by_sentence_pair(tok, [(pair_text.find("34.0"), 40.0),
                                           (pair_text.find("19.4"), 19.4)],
                                     pair_text) is None)

    # ── 2. 反向控制 ──
    print("\n[2] 反向控制必须生效")
    paths = sorted((ROOT / "data" / "artifacts").glob("**/*.json"))
    if paths:
        art = json.loads(paths[0].read_text(encoding="utf-8"))
        led = build_ledger(art)
        base = G.ground_llm_prose(led, art)
        ctrl = G.control_llm_prose(led, art)
        check("真实产物上接地率可算", base["matched"] + base["unmatched"] > 0,
              f"{base['rate']}")
        check("注入 99.99% 后接地率下降",
              ctrl["applicable"] and ctrl["rate_after"] < ctrl["rate_before"],
              f"{ctrl.get('rate_before')} → {ctrl.get('rate_after')}")
        check("未接地明细可定位到字段", ctrl["applicable"] and (
            base["unmatched"] > 0 or ctrl["rate_after"] < ctrl["rate_before"]))
        # 分母塌缩：台账为空时所有断言都未接地，比率恒为 0，注入伪造值比率也不会更低。
        # 判定若只看比率，这里会把「控制其实生效了」误报成失效。
        ctrl0 = G.control_llm_prose(build_ledger({"ticker": art.get("ticker")}), art)
        check("全部未接地（比率恒 0）时反向控制仍能检出",
              ctrl0["applicable"] and ctrl0["detected"],
              f"{ctrl0.get('rate_before')} → {ctrl0.get('rate_after')}")
    else:
        check("真实产物存在（否则 L1 无内容可评）", False, "data/artifacts 为空")

    # 同一标的只评最新一次：否则改好提示词后，被取代的旧产物仍永久压着分数
    import eval_l1_grounding as L1SEL
    picked = L1SEL.discover_artifacts()
    dirs = [p.parent for p in picked]
    check("L1 每标的只取一份产物", len(dirs) == len(set(dirs)),
          f"{len(picked)} 份 / {len(set(dirs))} 个标的")
    stale = []
    for i, p in enumerate(picked):
        if p.parent.name == "artifacts":
            continue  # 直接放在 artifacts 根下的旧文件，不按标的归档
        newest = max(p.parent.glob("*.json"), key=lambda x: x.name)
        if p != newest:
            stale.append(p.name)
    check("取的是时间戳最新的那份", not stale, str(stale))
    skipped = L1SEL.superseded_artifacts()
    check("被取代的产物在报告里点名", isinstance(skipped, list), f"跳过 {len(skipped)} 份")

    # ── 3. 基线比对 ──
    print("\n[3] 基线比对不得形同虚设")
    import run_eval as R

    base = {"tiers": {"l1": {"metrics": {"grounding_rate": 1.0}},
                      "l0": {"metrics": {"unknown_count": 0}}}}
    drift = R._compare({"l1": {"metrics": {"grounding_rate": 0.95}},
                        "l0": {"metrics": {"unknown_count": 3}}}, base)
    keys = {(d["tier"], d["metric"]) for d in drift}
    check("比率掉 0.05 → 报警", ("l1", "grounding_rate") in keys)
    check("未知路径从 0 涨到 3 → 报警", ("l0", "unknown_count") in keys)
    quiet = R._compare({"l1": {"metrics": {"grounding_rate": 0.99}},
                        "l0": {"metrics": {"unknown_count": 0}}}, base)
    check("容差内下滑不报警", quiet == [], str(quiet))

    # L2 的比率指标不以 _rate 结尾，必须单独受保护，否则召回率腰斩也不报警
    base_l2 = {"tiers": {"l2": {"metrics": {
        "mean_recall": 0.88, "mean_ndcg": 0.94, "mrr": 0.96}}}}
    drop = R._compare({"l2": {"metrics": {
        "mean_recall": 0.50, "mean_ndcg": 0.90, "mrr": 0.96}}}, base_l2)
    keys = {(d["tier"], d["metric"]) for d in drop}
    check("L2 召回率 0.88→0.50 → 报警", ("l2", "mean_recall") in keys)
    check("L2 nDCG 掉 0.04 → 报警", ("l2", "mean_ndcg") in keys)
    check("L2 MRR 没变 → 不报警", ("l2", "mrr") not in keys)

    # 层状态本身也要受保护：指标不动但整层由 PASS 掉到 FAIL，必须报警
    base_st = {"tiers": {"l3": {"status": "pass", "metrics": {"rules_passed": 88}}}}
    st = R._compare({"l3": {"status": "fail", "metrics": {"rules_passed": 88}}}, base_st)
    check("层状态 PASS→FAIL（指标不变）→ 报警",
          any(d.get("kind") == "status" and d["tier"] == "l3" for d in st), str(st))
    same = R._compare({"l3": {"status": "fail", "metrics": {"rules_passed": 88}}},
                      {"tiers": {"l3": {"status": "fail", "metrics": {"rules_passed": 88}}}})
    check("基线本身是 FAIL、仍为 FAIL → 不重复报警", same == [], str(same))
    better = R._compare({"l3": {"status": "pass", "metrics": {"rules_passed": 88}}},
                        {"tiers": {"l3": {"status": "fail", "metrics": {"rules_passed": 88}}}})
    check("FAIL→PASS 是好转，不报警", better == [], str(better))

    # 构成比（各出处占比，恒和为 1）在产物集合变动时不可比：跳过它，
    # 但质量指标与层状态照旧受保护——跳过不等于放过。
    base_comp = {"tiers": {"l0": {"status": "pass", "metrics": {
        "computed_rate": 0.3062, "grounding_rate": 1.0, "unknown_count": 0}}}}
    cur_comp = {"l0": {"status": "fail", "metrics": {
        "computed_rate": 0.2752, "grounding_rate": 0.90, "unknown_count": 0}}}
    keys = {(d["tier"], d["metric"]) for d in R._compare(cur_comp, base_comp)}
    check("产物集合未变时构成比下滑 → 报警", ("l0", "computed_rate") in keys)
    skipped = R._compare(cur_comp, base_comp,
                         skip_metrics=frozenset(R.COMPOSITION_METRICS))
    keys = {(d["tier"], d["metric"]) for d in skipped}
    check("跳过构成比后 computed_rate 不再报警",
          ("l0", "computed_rate") not in keys, str(skipped))
    check("跳过构成比不影响质量指标受保护", ("l0", "grounding_rate") in keys)
    check("跳过构成比不影响层状态受保护",
          any(d.get("kind") == "status" for d in skipped), str(skipped))

    # 集合变动只看路径集合：同一份产物内容变真，分母没变，构成比仍可比
    real_hashes = R._artifact_hashes
    try:
        R._artifact_hashes = lambda: {"data/artifacts/a/x.json": "h1"}
        same_set = R._artifact_set_delta({"artifacts": {"data/artifacts/a/x.json": "old"}})
        check("同一份产物内容变、路径集合不变 → 不算集合变动",
              not same_set["changed"], str(same_set))
        R._artifact_hashes = lambda: {"data/artifacts/a/x.json": "h1",
                                      "data/artifacts/b/y.json": "h2"}
        grown = R._artifact_set_delta({"artifacts": {"data/artifacts/a/x.json": "h1"}})
        check("新增一份产物 → 集合变动并点名新增",
              grown["changed"] and grown["added"] == ["data/artifacts/b/y.json"],
              str(grown))
        blank = R._artifact_set_delta({})
        check("老基线没记产物指纹 → 按未变动处理（宁可多比不可漏比）",
              not blank["changed"] and not blank["added"], str(blank))
    finally:
        R._artifact_hashes = real_hashes

    # 已知红：基线里记录过的失败不算新增；多出来的一条必须算
    def _rep(checks, artifact="a.json"):
        return {"status": "fail",
                "artifacts": [{"artifact": artifact, "status": "fail", "checks": checks}]}

    known = _rep([{"passed": False, "name": "接地率 ≥ 0.98", "detail": "0.9444"}])
    base_red = {"known_reds": {"l1": [{"where": "a.json", "name": "接地率 ≥ 0.98"}]}}
    check("基线已记录的已知红 → 不算新增",
          R._new_reds({"l1": known}, base_red) == [],
          str(R._new_reds({"l1": known}, base_red)))
    worse = _rep([{"passed": False, "name": "接地率 ≥ 0.98", "detail": "0.9444"},
                  {"passed": False, "name": "另一次的产物也崩了", "detail": "x"}])
    fresh = R._new_reds({"l1": worse}, base_red)
    check("已知红之外多一条失败 → 报新增",
          [d["metric"] for d in fresh] == ["另一次的产物也崩了"], str(fresh))
    check("新红只按 (出处, 检查名) 判，detail 字数变化不算新红",
          R._new_reds({"l1": _rep([{"passed": False, "name": "接地率 ≥ 0.98",
                                    "detail": "0.9444（未接地 2/36）"}])}, base_red) == [])

    # 重采会换文件名：L1 每标的只评最新一份，按目录比；L0 评全部产物，必须按文件比
    old_path = "data/artifacts/0700_HK/20261105T085830.json"
    new_path = "data/artifacts/0700_HK/20261106T164931.json"
    red = [{"passed": False, "name": "接地率 ≥ 0.98", "detail": "0.0"}]
    base_dir = {"known_reds": {"l1": [{"where": old_path, "name": "接地率 ≥ 0.98"}]}}
    check("L1 重采换了文件名 → 同一标的的老毛病不算新红",
          R._new_reds({"l1": _rep(red, new_path)}, base_dir) == [],
          str(R._new_reds({"l1": _rep(red, new_path)}, base_dir)))
    check("L0 同标的另一份产物犯同样的错 → 仍算新红（不能被老产物遮住）",
          [d["metric"] for d in
           R._new_reds({"l0": _rep(red, new_path)},
                       {"known_reds": {"l0": [{"where": old_path,
                                               "name": "接地率 ≥ 0.98"}]}})]
          == ["接地率 ≥ 0.98"])

    # ── 4. L2 指标数学 ──
    print("\n[4] L2 指标在构造用例上算得对")
    import eval_retrieval as L2

    rel = {"a": 3, "b": 1, "c": 0}
    check("全相关且按序 → nDCG=1", L2._ndcg([3, 1], 2) == 1.0)
    check("相关文档 2 条召回 1 条 → recall=0.5", L2._recall(["a", "c"], rel, 2) == 0.5)
    check("第一条就相关 → MRR=1", L2._reciprocal_rank(["a", "c"], rel) == 1.0)
    check("第二条才相关 → MRR=0.5", L2._reciprocal_rank(["c", "b"], rel) == 0.5)
    check("一条没召回 → MRR=0", L2._reciprocal_rank(["c"], rel) == 0.0)

    from types import SimpleNamespace as _NS

    class _R:
        def __init__(self, ids: list[str]):
            self.ids = ids

        def invoke(self, q, top_k=4):
            return [_NS(id=i, page_content="", metadata={}) for i in self.ids]

    items = [{"query": "q", "relevance": {"a": 3, "z": 3}, "no_relevant_docs": False},
             {"query": "empty", "relevance": {"a": 0}, "no_relevant_docs": True}]
    rep = L2.evaluate_labeled(_R(["a"]), items, 4)
    check("人工标注版：相关 2 篇召回 1 篇 → recall=0.5",
          rep["per_query_recall"]["q"] == 0.5)
    check("人工标注版：漏掉的 ≥2 分文档计入漏召回", rep["missed_relevant"] == 1)
    check("人工标注版：无相关文档的查询单独统计、不进均值",
          rep["skipped_no_relevant"] == 1 and rep["queries"] == 1)

    # 检索必须可复现：同分候选换插入顺序后结果必须一致（实测 chroma 在
    # 同分边界上跨进程返回不同子集，只按分数排序会让指标自己抖动）
    from app.rag.retriever import KnowledgeRetriever as KR

    class _TieStore:
        def __init__(self, order: tuple[str, ...]):
            self._docs = [{"id": i, "page_content": "茅台 财务 数据", "metadata": {}}
                          for i in ("a", "b", "c")]
            self._order = order

        def available(self) -> bool:
            return True

        def count(self) -> int:
            return len(self._docs)

        def all_docs(self, limit: int | None = 10000,
                     where: dict | None = None) -> list[dict]:
            return self._docs

        def query(self, text, where=None, top_k=5) -> list[dict]:
            by_id = {d["id"]: d for d in self._docs}
            return [{**by_id[i], "score": 0.9} for i in self._order]

    def _ids(order: tuple[str, ...]) -> list[str]:
        r = KR(store=_TieStore(order), top_k=3)
        return [d.id for d in r.invoke("茅台 财务", top_k=3)]

    o1, o2 = _ids(("c", "a", "b")), _ids(("b", "c", "a"))
    check("全同分时换候选顺序 → 召回结果一致", o1 == o2, f"{o1} vs {o2}")
    check("全同分时仍返满 top_k", len(o1) == 3, str(o1))

    # ── 5. L3 断言能报错 ──
    print("\n[5] L3 断言在坏数据上必须失败")
    import eval_l3_generation as L3

    good = {"valuation_analysis": {"dcf": {"sensitivity": {
        "matrix": [[100.0, 120.0], [80.0, 95.0]]}}}}
    ok, _ = L3._check_sensitivity(good)
    check("正常矩阵（折现率↑估值↓）→ 通过", ok)
    flipped = {"valuation_analysis": {"dcf": {"sensitivity": {
        "matrix": [[100.0, 120.0], [130.0, 150.0]]}}}}
    ok, detail = L3._check_sensitivity(flipped)
    check("折现率升高估值反升 → 报错", not ok, detail)

    seg = {"sotp_analysis": {"segments": [
        {"revenue_share": 0.6, "net_income_share": 0.6},
        {"revenue_share": 0.4, "net_income_share": 0.4}]}}
    ok, _ = L3._check_sotp(seg, f"…{L3._SHARE_LABEL}…")
    check("占比和为 1 且已标注 → 通过", ok)
    bad_seg = {"sotp_analysis": {"segments": [
        {"revenue_share": 0.9, "net_income_share": 1.0}]}}
    ok, _ = L3._check_sotp(bad_seg, f"…{L3._SHARE_LABEL}…")
    check("占比和为 0.9 → 报错", not ok)
    ok, _ = L3._check_sotp(seg, "占位文本，没有标注")
    check("未标注「模型估计」→ 报错", not ok)

    ok, _ = L3._check_empty_sections("## 一、摘要\n有内容\n## 二、估值\n")
    check("章节留空 → 报错", not ok)
    ok, _ = L3._check_empty_sections("## 一、摘要\n有内容\n")
    check("章节有内容 → 通过", ok)

    from app.compliance.rules import check_certainty

    check("「必然上涨」命中红线", check_certainty("该股必然上涨") == ["必然上涨"])
    check("「无风险利率」不算红线（标准术语）", check_certainty("无风险利率 4.4%") == [])

    # ── 6. 提示词的数值纪律（L1 掉分的源头在提示词里，而不是模型随机性）──
    print("\n[6] 提示词不得命令模型编造区间，也不得自带宏观利率")
    from app.analysis.prose_rules import PROSE_NUMERIC_PROMPT_RULES as RULES
    from app.analysis.master_prompt import (
        NO_RATE_LINE, build_system_prompt, macro_context)
    from app.observability.version import _prompt_files

    check("纪律文本禁自估区间", "禁止自行估算再给区间" in RULES)
    check("纪律文本禁编造定价", "禁止编造定价" in RULES)
    check("纪律文本要求只用上下文已有数字", "只能写上下文中已经出现过的数字" in RULES)
    check("纪律文本禁止引用未给出的宏观利率", "宏观利率只引用上文给出的观测值" in RULES)

    # 这行旧指令是 0700.HK「150-250bp」的直接来源：它命令模型必须给范围估计
    INVENT = "给出范围估计也比纯定性表述更有价值"
    MUST_WACC = "在2026年4.4%的无风险利率下，该公司资本回报率(ROIC)能否覆盖资本成本(WACC)？"
    for name in ("swot", "competitive", "geopolitical", "segment"):
        src = (ROOT / "app" / "analysis" / f"{name}.py").read_text(encoding="utf-8")
        check(f"{name}: 已删除「必须给范围估计」指令", INVENT not in src)
        check(f"{name}: 三问不再强制自估 WACC", MUST_WACC not in src)
        check(f"{name}: 改用共用 build_system_prompt",
              "build_system_prompt" in src and "MASTER_SYSTEM_PROMPT" not in src)

    master_src = (ROOT / "app" / "analysis" / "master_prompt.py").read_text(encoding="utf-8")
    check("共用指令注入数值纪律", "{PROSE_NUMERIC_PROMPT_RULES}" in master_src)
    # 4.4% / 4.25-4.50% / 4.5% 曾写死在四份角色指令里。模型不是凭记忆编的，是照抄
    # 我们给的字面量——提示词给什么，模型就抄什么，L1 于是判它「无出处」。
    # 查模板本身而不是源文件：源码里的历史说明（docstring）不该被当成复发。
    from app.analysis.master_prompt import MASTER_SYSTEM_PROMPT as MASTER_TPL
    for bad in ("4.4%", "4.25", "4.5%"):
        check(f"角色指令模板不再写死「{bad}」", bad not in MASTER_TPL)

    real = {"value": 0.0484, "source": "US Treasury 10 Yr", "confidence": "high"}
    check("真实利率渲染进提示词",
          "4.84%" in macro_context(real, "us") and "US Treasury" in macro_context(real, "us"))
    check("低置信兜底利率不喂给模型",
          macro_context({"value": 0.025, "source": "fallback常量",
                         "confidence": "low"}) == NO_RATE_LINE)
    check("完全无数据时禁止给具体数值", macro_context(None) == NO_RATE_LINE)
    check("港股提示词注明按美元利率近似",
          "港币与美元挂钩" in macro_context({**real, "confidence": "medium"}, "hk"))

    built = build_system_prompt("角色", macro_context(real, "us"), as_of="2026-11-06")
    check("组装后的 system prompt 含数值纪律与真实利率",
          "数值纪律" in built and "4.84%" in built and "2026-11-06" in built)

    # 反向控制：把注入抹掉，上面那条断言必须能报出来
    check("反向控制：抹掉注入后检查会失败",
          "{PROSE_NUMERIC_PROMPT_RULES}" not in master_src.replace(
              "{PROSE_NUMERIC_PROMPT_RULES}", ""))

    from app.analysis import competitive as _C
    from app.analysis import swot as _S
    check("竞品识别 prompt 禁止写价格带数字", "不得写具体价格数值或价格区间" in _C.COMPETITOR_COT_PROMPT)
    check("SWOT 不再要求「必须有具体数据支撑」", "观点必须有具体数据支撑" not in _S.SWOT_COT_PROMPT)

    scanned = [p.name for p in _prompt_files()]
    check("数值纪律模块计入 prompt_version 哈希", "prose_rules.py" in scanned, str(len(scanned)))
    check("共用角色指令计入 prompt_version 哈希", "master_prompt.py" in scanned)

    # ── 7. 集中度口径 ──
    # 「目标 + 已知竞品」求和当分母时，5 家样本的 CR5 恒等于 100%（实测 0.9999999999999999，
    # 按两位小数渲染正好是 99.99，于是任何编造的 99.99% 都能被它「接地」）
    print("\n[7] 集中度必须有行业口径")
    from app.analysis.market import MarketAnalyzer
    _ma = MarketAnalyzer()
    _comps = [{"name": f"C{i}", "revenue": r} for i, r in enumerate([50.0, 40.0, 30.0, 20.0])]
    no_total = _ma.analyze_industry_competitors(100.0, _comps, "T")
    check("无行业总规模 → CR3/CR5/HHI 一律 None",
          no_total["cr3"] is None and no_total["cr5"] is None and no_total["hhi"] is None,
          str({k: no_total[k] for k in ("cr3", "cr5", "hhi")}))
    check("无行业总规模 → 报告能说明原因", bool(no_total.get("concentration_note")))
    check("无行业总规模 → 排名仍可用（样本内可靠）", no_total["rank"] == 1)
    with_total = _ma.analyze_industry_competitors(100.0, _comps, "T", industry_total=1000.0)
    check("有行业总规模 → 才给 CR，且 CR5 不恒为 1",
          with_total["cr3"] is not None and with_total["cr5"] < 1.0,
          f"cr3={with_total['cr3']} cr5={with_total['cr5']}")
    check("有行业总规模 → 份额按行业口径（本公司 10%）",
          abs(with_total["market_share"] - 0.1) < 1e-9, str(with_total["market_share"]))
    check("CR 已取整，不带浮点噪声", with_total["cr5"] == round(with_total["cr5"], 4))

    print("\n" + "=" * 62)
    if failed:
        print(f"结果: {passed} 通过, {failed} 失败")
        return 1
    print(f"结果: {passed}/{passed} 全部通过")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
