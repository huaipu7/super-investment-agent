"""D3 评测：知识库检索 nDCG（标注相关性）。

注入小标注语料（个股研报 + 宏观数据），按查询评估 KnowledgeRetriever 的
词法路径（FakeStore 无嵌入后端 → 静默降级词法），算 nDCG@k 均值。
离线确定性，不依赖 chroma 磁盘状态。

运行: D:\\python\\python.exe scripts\\eval_retrieval.py
"""
from __future__ import annotations

import math
import sys
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


class _FakeStore:
    """无嵌入后端（dense 恒空），驱动 retriever 词法降级路径。"""

    def __init__(self, docs: list[dict]):
        self._docs = docs

    def available(self) -> bool:
        return False

    def count(self) -> int:
        return len(self._docs)

    def all_docs(self, limit: int = 10000) -> list[dict]:
        return self._docs

    def query(self, text: str, where: dict | None = None, top_k: int = 5) -> list[dict]:
        return []

    @property
    def backend_label(self) -> str:
        return "fake-lexical"


def _dcg(rels: list[int]) -> float:
    return sum(r / math.log2(i + 2) for i, r in enumerate(rels))


def _ndcg(rels: list[int], k: int = None) -> float:
    if k:
        rels = rels[:k]
    ideal = sorted(rels, reverse=True)
    denom = _dcg(ideal)
    return _dcg(rels) / denom if denom else 0.0


def run() -> dict:
    from app.rag.retriever import KnowledgeRetriever

    retriever = KnowledgeRetriever(store=_FakeStore(DOCS), top_k=TOP_K)
    per_query: dict[str, float] = {}
    for q in QUERIES:
        docs = retriever.invoke(q["query"], top_k=TOP_K)
        rels = [int(q["relevance"].get(d.id or "", 0)) for d in docs]
        per_query[q["query"]] = round(_ndcg(rels, TOP_K), 4)

    mean = sum(per_query.values()) / len(per_query)
    return {"mean_ndcg": round(mean, 4), "per_query": per_query,
            "queries": len(per_query), "top_k": TOP_K}


def main() -> None:
    r = run()
    print(f"[eval_retrieval] queries={r['queries']} top_k={r['top_k']} "
          f"mean_ndcg={r['mean_ndcg']}")
    for q, s in r["per_query"].items():
        print(f"  nDCG@{r['top_k']}={s}  query={q}")
    sys.exit(0 if r["mean_ndcg"] >= 0.5 else 1)


if __name__ == "__main__":
    main()
