"""知识库检索器 —— 深度研究管线接入层。

KnowledgeRetriever 包装 KnowledgeStore，向管线提供统一检索接口：
- invoke(query, where, top_k) → list[SimpleNamespace]，
  元素含 page_content(str) 与 metadata(dict{score,section,source,ticker,date,kind})，
  对齐 SwotAnalyzer.build_rag_context 的期望（doc.page_content / doc.metadata.score|section|source）。
- 混合检索：dense（向量）与 lexical（TfidfVectorizer 词法粗召回）线性融合。
  无嵌入后端时词法路径独立可用 → 静默降级不崩溃。
- 时间衰减：近 RECENCY_DAYS 天文档加分，避免历史研报以旧当新。
- query_for_ticker(ticker, industry)：多路检索（个股研报/新闻/公告 + 宏观数据 + 宏观新闻）后合并排序。
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Optional

from app.rag.store import get_knowledge_store

RECENCY_DAYS = 180  # 近 180 天视为"近期文档"
RECENCY_BONUS_MAX = 0.05  # 时间衰减最高加分（保持余弦相似度为主导）
W_LEX = 0.5  # 词法分权重（dense 同权）

_MACRO_DATA_QUERY = "宏观经济 政策 利率 CPI 增速 货币 信贷"
_MACRO_NEWS_QUERY = "宏观 经济 政策 市场 资金 央行 货币"

_EXCLUDED_META = {"score", "id"}  # 透传 metadata 时剔除合成字段


class KnowledgeRetriever:
    def __init__(self, store=None, top_k: int = 5):
        self._store = store or get_knowledge_store()
        self._top_k = top_k
        self._corpus: list[dict] = []
        self._corpus_key: int = -1

    @property
    def available(self) -> bool:
        """嵌入模型/向量库可用性（不可用时调用方应回退无 RAG 分支）。"""
        try:
            return self._store.available()
        except Exception:
            return False

    def invoke(
        self, query: str, where: dict | None = None, top_k: int | None = None
    ) -> list[SimpleNamespace]:
        """混合检索（dense + Tfidf 词法）融合，返回 SimpleNamespace 列表。"""
        top_k = top_k or self._top_k

        # 1. dense 路径（无嵌入后端时 store.query 返回 []）
        dense: dict[str, SimpleNamespace] = {}
        for r in self._store.query(query, where=where, top_k=top_k):
            meta = {k: v for k, v in (r.get("metadata") or {}).items()}
            score = r.get("score")
            if score is not None:
                score = self._apply_time_decay(float(score), meta.get("date", ""))
            dscore = round(score, 4) if score is not None else None
            meta["score"] = dscore
            meta["dense_score"] = dscore
            key = r.get("id") or r.get("page_content", "")
            dense[key] = SimpleNamespace(
                id=r.get("id", ""),
                page_content=r.get("page_content", ""),
                metadata=meta,
            )

        # 2. lexical 粗召回（Tfidf），与 dense 融合
        merged: dict[str, SimpleNamespace] = dict(dense)
        for cand in self._lexical_retrieve(query, where=where, top_k=max(top_k * 3, 10)):
            key = cand["id"] or cand["page_content"]
            lex_s = round(float(cand.get("lex_score") or 0.0), 4)
            if key in merged:
                m = merged[key].metadata
                dscore = m.get("dense_score")
                m["lex_score"] = lex_s
                m["score"] = round(self._fuse(dscore, lex_s), 4)
            else:
                meta = {k: v for k, v in cand["metadata"].items()}
                meta["dense_score"] = None
                meta["lex_score"] = lex_s
                meta["score"] = round(self._fuse(None, lex_s), 4)
                merged[key] = SimpleNamespace(
                    id=cand["id"], page_content=cand["page_content"], metadata=meta)
            merged[key].metadata["lex_score"] = lex_s

        docs = sorted(
            merged.values(), key=lambda d: d.metadata.get("score") or 0.0, reverse=True
        )
        return docs[:top_k]

    def _lexical_retrieve(self, query: str, where: dict | None = None,
                          top_k: int = 10) -> list[dict]:
        """TfidfVectorizer 词法粗召回：对候选池算余弦，返回 [{id, page_content, metadata, lex_score}]。"""
        all_docs = self._corpus_docs()
        if not all_docs:
            return []
        if where:
            all_docs = [
                d for d in all_docs
                if all(_match_meta(d["metadata"], k, v) for k, v in where.items())
            ]
        if not all_docs:
            return []

        corpus = [d["page_content"] or "" for d in all_docs]
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer

            vec = TfidfVectorizer(
                analyzer="char_wb", ngram_range=(1, 2),
                min_df=1, max_features=5000, sublinear_tf=True,
            )
            X = vec.fit_transform(corpus)
            sims = (X @ vec.transform([query]).T).toarray().ravel()
        except Exception:  # noqa: BLE001 sklearn 缺失/异常 → 子串命中兜底
            ql = query.lower()
            sims = [1.0 if ql in (d["page_content"] or "").lower() else 0.0
                    for d in all_docs]

        scored = sorted(zip(all_docs, sims), key=lambda x: x[1], reverse=True)
        out = []
        for d, s in scored[:top_k]:
            if s <= 0:
                break
            out.append({"id": d["id"], "page_content": d["page_content"],
                        "metadata": d["metadata"], "lex_score": float(s)})
        return out

    def _corpus_docs(self) -> list[dict]:
        """候选池缓存：按 collection 文档数签名，避免多路检索重复拉全量。"""
        try:
            n = self._store.count()
        except Exception:
            return []
        if self._corpus_key != n:
            self._corpus = self._store.all_docs()
            self._corpus_key = n
        return self._corpus

    @staticmethod
    def _fuse(dense_score: float | None, lex_score: float | None) -> float:
        return (W_LEX * float(dense_score or 0.0)
                + (1.0 - W_LEX) * float(lex_score or 0.0))

    def query_for_ticker(
        self, ticker: str, industry: str = "", top_k: int | None = None
    ) -> list[SimpleNamespace]:
        """按股票组装多路检索并合并：个股相关 + 宏观数据 + 宏观新闻。

        个股路由用 where={"ticker": ticker} 精确过滤（研报/新闻/公告/股东大会）；
        宏观数据/新闻 ticker 为空串，需单独路由。合并后按 score 降序，返回前 top_k。
        """
        top_k = top_k or self._top_k
        merged: dict[str, SimpleNamespace] = {}

        if ticker:
            for d in self.invoke(
                f"{ticker} {industry} 财务状况 行业分析 新闻 公告",
                where={"ticker": ticker},
                top_k=max(3, top_k),
            ):
                merged[d.id or d.page_content] = d

        for d in self.invoke(
            f"{industry} {_MACRO_DATA_QUERY}",
            where={"kind": "宏观数据"},
            top_k=2,
        ):
            merged[d.id or d.page_content] = d

        for d in self.invoke(_MACRO_NEWS_QUERY, where={"kind": "宏观新闻"}, top_k=2):
            merged[d.id or d.page_content] = d

        docs = sorted(
            merged.values(), key=lambda d: d.metadata.get("score") or 0.0, reverse=True
        )
        return docs[:top_k]

    @staticmethod
    def _apply_time_decay(score: float, date_str: str) -> float:
        """近 180 天文档线性加分（距今越近分越高），日期缺失/异常不加分。"""
        if not date_str:
            return score
        try:
            d = datetime.date.fromisoformat(str(date_str)[:10])
        except ValueError:
            return score
        days = (datetime.date.today() - d).days
        if 0 <= days <= RECENCY_DAYS:
            return score + RECENCY_BONUS_MAX * (1 - days / RECENCY_DAYS)
        return score


def _match_meta(meta: dict, key: str, value) -> bool:
    """词法候选池 where 过滤：与 chroma 一致的严格标量相等。"""
    return str(meta.get(key)) == str(value)


# ── 全局单例（对齐 get_knowledge_store 模式）────────────────────

_retriever: Optional[KnowledgeRetriever] = None


def get_knowledge_retriever() -> KnowledgeRetriever:
    global _retriever
    if _retriever is None:
        _retriever = KnowledgeRetriever()
    return _retriever
