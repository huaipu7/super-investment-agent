"""L2 评测：知识库检索质量（nDCG@k / Recall@k / MRR）。

注入小标注语料（个股研报 + 宏观数据），按查询评估 KnowledgeRetriever 的
词法路径（FakeStore 无嵌入后端 → 静默降级词法）。离线确定性，不依赖
chroma 磁盘状态。

**这 3 条查询是自造夹具，不是人工标注集。** 4 篇文档、3 条查询只够发现
「检索坏了」，不足以支撑任何质量结论——报告里 labels="synthetic" 就是这个
意思，不要当成 benchmark。扩到 20–30 条真实查询需要用户人工标注相关性，
标注不得代编（代编标注 = 伪造评测）。

三个指标互补，缺一个就会被刷分：
  nDCG@k   排序质量，相关文档排得越靠前越高
  Recall@k 召回率，标注为相关的文档有多少进了前 k
  MRR      第一个相关结果出现的位置，衡量「翻到第几条才能用」

运行: D:\\python\\python.exe scripts\\eval_retrieval.py
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOCS = [
    {"id": "m1", "page_content": "贵州茅台 2023 年营收 1500 亿元，净利润 700 亿元，毛利率 91%，ROE 30%。",
     "metadata": {"ticker": "600519.SS", "date": "2023-12-31", "kind": "研报", "source": "mock", "section": "财务"}},
    {"id": "m2", "page_content": "贵州茅台 市盈率 PE 30 倍，估值处于历史中位数。",
     "metadata": {"ticker": "600519.SS", "date": "2024-06-30", "kind": "研报", "source": "mock", "section": "估值"}},
    {"id": "wl", "page_content": "五粮液 2023 年营收 800 亿元，净利润 300 亿元，毛利率 75%，ROE 24%。",
     "metadata": {"ticker": "000858.SZ", "date": "2023-12-31", "kind": "研报", "source": "mock", "section": "财务"}},
    {"id": "macro", "page_content": "宏观经济政策 利率 信贷 货币 增速 数据 统计。",
     "metadata": {"ticker": "", "date": "2024-01-15", "kind": "宏观数据", "source": "mock", "section": "宏观"}},
]

# 查询 → 相关文档分级（3 最相关，0 无关）
QUERIES = [
    {"query": "茅台盈利 净利润 营收", "relevance": {"m1": 3, "m2": 1, "wl": 1, "macro": 0}},
    {"query": "五粮液 营收 增长", "relevance": {"wl": 3, "m1": 1, "m2": 0, "macro": 0}},
    {"query": "宏观 利率 政策", "relevance": {"macro": 3, "m1": 0, "m2": 0, "wl": 0}},
]

TOP_K = 4
MIN_NDCG = 0.5
REPORT_PATH = ROOT / "data" / "eval" / "l2_report.json"
HISTORY_PATH = ROOT / "data" / "eval" / "history.jsonl"
LABELS_PATH = ROOT / "data" / "eval" / "l2_labels.json"


class _FakeStore:
    """无嵌入后端（dense 恒空），驱动 retriever 词法降级路径。"""

    def __init__(self, docs: list[dict]):
        self._docs = docs

    def available(self) -> bool:
        return False

    def count(self) -> int:
        return len(self._docs)

    def all_docs(self, limit: int | None = 10000,
                 where: dict | None = None) -> list[dict]:
        if not where:
            return self._docs
        return [d for d in self._docs
                if all(d["metadata"].get(k) == v for k, v in where.items())]

    def query(self, text: str, where: dict | None = None, top_k: int = 5) -> list[dict]:
        return []

    @property
    def backend_label(self) -> str:
        return "fake-lexical"


def _dcg(rels: list[int]) -> float:
    return sum(r / math.log2(i + 2) for i, r in enumerate(rels))


def _ndcg(rels: list[int], k: int | None = None) -> float:
    if k:
        rels = rels[:k]
    ideal = sorted(rels, reverse=True)
    denom = _dcg(ideal)
    return _dcg(rels) / denom if denom else 0.0


def _recall(got: list[int], relevance: dict[str, int], k: int) -> float:
    """相关文档（分级 > 0）有多少进了前 k。标注里一条相关都没有时返回 1.0。"""
    relevant = {d for d, r in relevance.items() if r > 0}
    if not relevant:
        return 1.0
    hit = sum(1 for d in relevant if d in got[:k])
    return hit / len(relevant)


def _reciprocal_rank(got: list[int], relevance: dict[str, int]) -> float:
    """第一个相关结果的位置倒数；一个都没召回到就是 0。"""
    for i, d in enumerate(got, 1):
        if relevance.get(d, 0) > 0:
            return 1.0 / i
    return 0.0


def load_labels(path: Path = LABELS_PATH) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_labeled(retriever, items: list[dict], top_k: int = TOP_K) -> dict:
    """人工标注版：对每条真实查询跑真实检索，按标注算 nDCG/recall/MRR。

    **召回率是池化的下界估计。**标注池 = 系统 top-k + 盲抽 2 篇，语料里未被
    标到的文档一律当 0 分。这是 TREC pooling 的标准做法，但它意味着 recall
    只会低估、不会高估——所以不能对外说「召回率 0.8 就是真实召回率 0.8」。
    """
    ndcg: dict[str, float] = {}
    recall: dict[str, float] = {}
    mrr: dict[str, float] = {}
    retrieved: dict[str, list[str]] = {}
    missed: list[str] = []
    skipped: list[str] = []

    for it in items:
        query = it["query"]
        if it.get("no_relevant_docs"):
            skipped.append(query)
            continue
        docs = retriever.invoke(query, top_k=top_k)
        ids = [d.id or "" for d in docs]
        relevance = it["relevance"]
        rels = [int(relevance.get(d, 0)) for d in ids]
        retrieved[query] = ids
        ndcg[query] = round(_ndcg(rels, top_k), 4)
        recall[query] = round(_recall(ids, relevance, top_k), 4)
        mrr[query] = round(_reciprocal_rank(ids, relevance), 4)
        missed += [d for d, r in relevance.items() if r >= 2 and d not in ids]

    n = len(ndcg)
    checks = [{
        "name": f"nDCG@{top_k} ≥ {MIN_NDCG}（人工标注集）",
        "passed": bool(n) and all(v >= MIN_NDCG for v in ndcg.values()),
        "detail": f"{n} 条查询，最低 {min(ndcg.values()) if ndcg else 0}",
    }]
    return {
        "tier": "l2",
        "title": "检索质量",
        "description": "nDCG / 召回率 / MRR：相关文档有没有被排到前面（人工标注）",
        "status": "pass" if all(c["passed"] for c in checks) else "fail",
        "labels": "human",
        "note": f"人工标注 {n} 条查询；召回率为池化下界估计（标注池 = top-{top_k} + 盲抽）",
        "queries": n,
        "top_k": top_k,
        "skipped_no_relevant": len(skipped),
        "missed_relevant": len(missed),
        "mean_ndcg": round(sum(ndcg.values()) / n, 4) if n else 0.0,
        "mean_recall": round(sum(recall.values()) / n, 4) if n else 0.0,
        "mrr": round(sum(mrr.values()) / n, 4) if n else 0.0,
        "per_query": ndcg,
        "per_query_recall": recall,
        "per_query_mrr": mrr,
        "retrieved": retrieved,
        "checks": checks,
        "checks_passed": sum(1 for c in checks if c["passed"]),
        "checks_total": len(checks),
        "recall_saturated": False,
        "metrics": {
            "mean_ndcg": round(sum(ndcg.values()) / n, 4) if n else 0.0,
            "mean_recall": round(sum(recall.values()) / n, 4) if n else 0.0,
            "mrr": round(sum(mrr.values()) / n, 4) if n else 0.0,
            "queries": n,
            "top_k": top_k,
            "missed_relevant": len(missed),
        },
    }


def run(labeled: bool = False, db: str | Path | None = None) -> dict:
    """db 指定 Chroma 持久化目录（如冻结快照 knowledge_db_v1）。

    不指定时仍走全局单例，兼容既有调用。
    """
    from app.rag.retriever import KnowledgeRetriever

    if labeled or db or LABELS_PATH.exists():
        labels = load_labels()
        if labels:
            from app.rag.store import KnowledgeStore, get_knowledge_store

            if db:
                store = KnowledgeStore(persist_dir=db)
                try:
                    db_label = str(Path(db).resolve().relative_to(ROOT))
                except ValueError:
                    db_label = str(Path(db).resolve())
            else:
                store = get_knowledge_store()
                db_label = "data/knowledge_db"
            retriever = KnowledgeRetriever(store=store, top_k=TOP_K)
            report = evaluate_labeled(retriever, labels["items"], labels.get("top_k", TOP_K))
            report["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            report["db"] = db_label
            report["corpus_total"] = store.count()
            return report
        if labeled:
            raise SystemExit(f"没有标注文件 {LABELS_PATH}：先跑 scripts/make_label_sheet.py")


    retriever = KnowledgeRetriever(store=_FakeStore(DOCS), top_k=TOP_K)
    ndcg: dict[str, float] = {}
    recall: dict[str, float] = {}
    mrr: dict[str, float] = {}
    retrieved: dict[str, list[str]] = {}

    for q in QUERIES:
        docs = retriever.invoke(q["query"], top_k=TOP_K)
        ids = [d.id or "" for d in docs]
        rels = [int(q["relevance"].get(d, 0)) for d in ids]
        retrieved[q["query"]] = ids
        ndcg[q["query"]] = round(_ndcg(rels, TOP_K), 4)
        recall[q["query"]] = round(_recall(ids, q["relevance"], TOP_K), 4)
        mrr[q["query"]] = round(_reciprocal_rank(ids, q["relevance"]), 4)

    n = len(QUERIES)
    checks = [{
        "name": f"nDCG@{TOP_K} ≥ {MIN_NDCG}",
        "passed": all(v >= MIN_NDCG for v in ndcg.values()),
        "detail": f"逐查询 {ndcg}",
    }]

    return {
        "tier": "l2",
        "title": "检索质量",
        "description": "nDCG / 召回率 / MRR：相关文档有没有被排到前面",
        "status": "pass" if all(c["passed"] for c in checks) else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "labels": "synthetic",
        "note": "3 条自造查询 / 4 篇文档，只作烟囱测试，不构成质量结论",
        "queries": n,
        "top_k": TOP_K,
        "mean_ndcg": round(sum(ndcg.values()) / n, 4),
        "mean_recall": round(sum(recall.values()) / n, 4),
        "mrr": round(sum(mrr.values()) / n, 4),
        "per_query": ndcg,
        "per_query_recall": recall,
        "per_query_mrr": mrr,
        "retrieved": retrieved,
        "checks": checks,
        "checks_passed": sum(1 for c in checks if c["passed"]),
        "checks_total": len(checks),
        # top_k 已经覆盖整个语料时召回率恒为 1.0，此时它不度量任何东西
        "recall_saturated": TOP_K >= len(DOCS),
        "metrics": {
            "mean_ndcg": round(sum(ndcg.values()) / n, 4),
            "mean_recall": round(sum(recall.values()) / n, 4),
            "mrr": round(sum(mrr.values()) / n, 4),
            "queries": n,
            "top_k": TOP_K,
        },
    }


def save(report: dict, out: str | Path | None = None,
         history: bool = True) -> Path:
    path = Path(out) if out else REPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not history:
        return path
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "tier": report["tier"],
            "generated_at": report["generated_at"],
            "status": report["status"],
            "checks_passed": report["checks_passed"],
            "checks_total": report["checks_total"],
            # 记下标注口径：趋势图上要标出「合成→人工」切换点，
            # 否则曲线看起来像质量突然下跌
            "labels": report.get("labels"),
            "metrics": report["metrics"],
        }, ensure_ascii=False) + "\n")
    return path


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="L2 检索质量评测")
    ap.add_argument("--labeled", action="store_true",
                    help="用人工标注集 l2_labels.json（存在时会自动使用）")
    ap.add_argument("--db", default=None,
                    help="Chroma 持久化目录（如 data/knowledge_db_v1 冻结快照）")
    ap.add_argument("--out", default=None, help="报告输出路径")
    ap.add_argument("--no-history", action="store_true",
                    help="不写 history.jsonl（快照复现时用）")
    args = ap.parse_args()

    r = run(labeled=args.labeled, db=args.db)
    path = save(r, out=args.out, history=not args.no_history)
    print(f"[eval_retrieval] queries={r['queries']} top_k={r['top_k']} "
          f"labels={r['labels']} db={r.get('db')} corpus={r.get('corpus_total')}")
    print(f"  mean_ndcg={r['mean_ndcg']}  mean_recall={r['mean_recall']}  "
          f"mrr={r['mrr']}"
          + ("  (recall 已饱和：k 覆盖整个语料，该值不度量任何东西)"
             if r["recall_saturated"] else ""))
    for q in r["per_query"]:
        print(f"  {q}  nDCG={r['per_query'][q]}  recall={r['per_query_recall'][q]}  "
              f"rr={r['per_query_mrr'][q]}")
    if r.get("missed_relevant"):
        print(f"  漏召回（标注为相关但未进 top-{r['top_k']}）: {r['missed_relevant']} 篇")
    print(f"  {r['note']}")
    try:
        shown = path.resolve().relative_to(ROOT)
    except ValueError:
        shown = path.resolve()
    print(f"报告已写: {shown}")
    sys.exit(0 if r["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
