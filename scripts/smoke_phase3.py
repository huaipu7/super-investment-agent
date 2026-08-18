"""
Phase 3 冒烟测试：四大师专家评分引擎。

对 600519 跑专家评分，并逐项比对 Violet Z 原逻辑（直接从 Stock_Assistant/app.py
的源码中 ast 提取评分函数运行，确保比对对象是"原代码"而非抄写副本）。

运行: PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\smoke_phase3.py
"""
import ast
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis import expert_scoring
from app.data.sources.akshare_source import AKShareSource

ORIGINAL_APP = Path(
    r"D:\Desktop\ClaudeProjects\Stock_Assistant\app.py"
)

# 从原项目源码中提取的纯评分函数名（不依赖 flask/akshare 全局）
ORIG_FUNCS = [
    "score_buffett", "score_munger", "score_duan", "score_dalio",
    "get_decision", "generate_summary",
]

_results: list[dict] = []


def record(name: str, ok: bool, detail: str = ""):
    _results.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def load_original_scorers() -> dict:
    """从 Stock_Assistant/app.py 源码中 ast 提取并执行评分函数，返回 {name: func}。"""
    src = ORIGINAL_APP.read_text(encoding="utf-8")
    tree = ast.parse(src)
    ns: dict = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in ORIG_FUNCS:
            mod = ast.Module(body=[node], type_ignores=[])
            exec(compile(mod, ORIGINAL_APP.name, "exec"), ns)
    missing = [n for n in ORIG_FUNCS if n not in ns]
    assert not missing, f"原项目缺少函数: {missing}"
    return ns


def compare_against_original(fin: dict, market_cap: float) -> int:
    """对同一 fin dict，逐项比对迁移版与原版四位评分。返回通过项数。"""
    orig = load_original_scorers()
    n_ok = 0

    for name in ("buffett", "munger", "duan", "dalio"):
        mig_fn = getattr(expert_scoring, f"score_{name}")
        orig_fn = orig[f"score_{name}"]

        kwargs = {"market_cap": market_cap} if name == "duan" else {}
        mig_result = mig_fn(fin, **kwargs)
        orig_result = orig_fn(fin, **kwargs)

        same = mig_result == orig_result
        score = mig_result[0]
        record(f"对比 600519 {name} 评分",
               same,
               f"score={score} 优势{len(mig_result[1])}条 风险{len(mig_result[2])}条"
               f"{'（与原版一致）' if same else ' → 与原版不一致! ' + repr((mig_result, orig_result))}")
        n_ok += 1 if same else 0

    # get_decision / generate_summary 全档位遍历
    for s in (0, 1, 3, 4, 5, 6, 7, 8, 9, 10):
        a = expert_scoring.get_decision(s)
        b = orig["get_decision"](s)
        record(f"get_decision({s})", a == b, f"{a}")
        n_ok += 1 if a == b else 0
        for nm in ("巴菲特", "芒格", "段永平", "达利欧"):
            sa = expert_scoring.generate_summary(nm, s, [], [])
            sb = orig["generate_summary"](nm, s, [], [])
            record(f"summary({nm},{s})", sa == sb, "")
            n_ok += 1 if sa == sb else 0

    return n_ok


def main():
    print("=" * 60)
    print("Phase 3 验证: 四大师专家评分引擎 (600519)")
    print("=" * 60)

    t0 = time.time()
    result = expert_scoring.run_expert_analysis("600519")
    dt = round(time.time() - t0, 1)

    stock = result["stock"]
    experts = result["experts"]
    comparison = result["comparison"]
    print(f"\n耗时: {dt}s | 公司: {stock['name']} | 行业: {result['industry_name']}")
    print(f"现价 {stock['price']}  涨跌 {stock['change_pct']}%  换手 {stock['turnover']}%  "
          f"振幅 {stock['amplitude']}%  PE {stock['pe']}  PB {stock['pb']}  市值 {stock['market_cap']}")

    # ── 结构校验 ──
    record("行情字段齐全", all(k in stock for k in (
        "code", "name", "price", "change_pct", "change_amount", "volume",
        "amount", "high", "low", "open", "pre_close", "pe", "pb",
        "market_cap", "turnover", "amplitude", "industry")), f"现价 {stock['price']}")
    record("四位专家完整", len(experts) == 4, f"{len(experts)} 位")
    record("指标对比非空", len(comparison) >= 6, f"{len(comparison)} 项")

    for e in experts:
        record(f"专家 {e['id']} 输出完整",
               all(k in e for k in ("id", "name", "icon", "score", "decision",
                                    "decision_color", "summary", "advantages", "risks")),
               f"{e['name']} → {e['score']}/10 ({e['decision']}) 优势{len(e['advantages'])} 风险{len(e['risks'])}")

    # ── 与 Violet Z 原逻辑逐项比对 ──
    print("\n" + "=" * 60)
    print("与 Violet Z 原逻辑逐项比对（同一 fin 输入）")
    print("=" * 60)
    fin = AKShareSource().get_financial_indicators("600519")
    market_cap = stock.get("market_cap")
    print(f"\n财务指标: ROE={fin.get('roe')}% 负债率={fin.get('debt_ratio')}%  "
          f"毛利率={fin.get('gross_margin')}% 净利率={fin.get('net_margin')}%  "
          f"市值={market_cap}")
    print(f"输入源: {ORIGINAL_APP}")

    n_orig_ok = compare_against_original(fin, market_cap)

    # ── 汇总 ──
    passed = sum(1 for r in _results if r["ok"])
    print("\n" + "=" * 60)
    print(f"汇总: 通过 {passed} / {len(_results)}（含与原版逐项比对 {n_orig_ok} 项）")
    for r in _results:
        print(f"  [{'PASS' if r['ok'] else '--'}] {r['name']}")
    print("=" * 60)

    if passed != len(_results):
        sys.exit(1)
    print("\nPhase 3 核心验证通过")


if __name__ == "__main__":
    main()
