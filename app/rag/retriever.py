"""知识库检索器 —— 深度研究管线接入层。

KnowledgeRetriever 包装 KnowledgeStore，向管线提供统一检索接口：
- invoke(query, where, top_k) → list[SimpleNamespace]，
  元素含 page_content(str) 与 metadata(dict{score,section,source,ticker,date,kind})，
  对齐 SwotAnalyzer.build_rag_context 的期望（doc.page_content / doc.metadata.score|section|source）。
- 混合检索：dense（向量）与 lexical（TfidfVectorizer 词法粗召回）线性融合。
  无嵌入后端时词法路径独立可用 → 静默降级不崩溃。
- 时间衰减：近 RECENCY_DAYS 天文档加分，避免历史研报以旧当新。
- query_for_ticker(ticker, industry)：多路检索（个股研报/新闻/公告 + 宏观数据 + 宏观新闻
  + 披露文件 + 政策 + 论文）后合并排序。
"""
from __future__ import annotations

import datetime
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Optional

from app.rag.store import get_knowledge_store

RECENCY_DAYS = 180  # 近 180 天视为"近期文档"
RECENCY_BONUS_MAX = 0.05  # 时间衰减最高加分（保持余弦相似度为主导）
W_LEX = 0.5  # 词法分权重（dense 同权）
# dense 多取倍数：只向 chroma 要 top_k 篇时，同分文档会在 k 边界被任意截断
# （同一 query 跨进程实测返回不同子集），融合分随之不可复现。多要一些把同分
# 组覆盖完整，再按 (分数, id) 确定性截回 top_k——排序语义不变，只是不再随机。
DENSE_MULT = 3
# 重排候选池倍数：池子必须比 top_k 大，否则只是把同一批人重排一次，换不了位
RERANK_CANDIDATE_MULT = 4

_MACRO_DATA_QUERY = "宏观经济 政策 利率 CPI 增速 货币 信贷"
_MACRO_NEWS_QUERY = "宏观 经济 政策 市场 资金 央行 货币"
_POLICY_QUERY = "政策 监管 产业"
_PAPER_QUERY = "研究 论文"
_DISCLOSURE_QUERY = "年报 披露 财务报表"
# 单篇外部文档最多占几个位置：一份年报切出几百块，不限的话个股路前 5 全是同一篇
# （实测 2 份年报就占掉前 10 的 6~7 个位置）。存量切片没有 doc_uid，不受此限。
EXTERNAL_DOC_CAP = 2
# 外部类各保 1 个位置：这些语料与个股研报的语义距离天然更远（10-K 的表格正文、
# 政策条文），按分数排序时会被存量研报整体挤掉——实测 AAPL 的披露路最高 0.5573，
# 而第 5 名研报 0.6002，披露块永远进不了前 5。所以候选池里有就给 1 个位置。
_RESERVED_KINDS = ("披露文件", "政策", "论文")

_EXCLUDED_META = {"score", "id"}  # 透传 metadata 时剔除合成字段


class KnowledgeRetriever:
    def __init__(self, store=None, top_k: int = 5, reranker=None):
        self._store = store or get_knowledge_store()
        self._top_k = top_k
        self._reranker = reranker
        self._lex_cache: dict[tuple, tuple[list[dict], object, object]] = {}
        self._lex_count: int = -1

    @property
    def reranker(self):
        """按库目录取重排器。v1 快照会被它自己拦下（目录名硬判）。"""
        if self._reranker is None:
            from app.rag.rerank import get_reranker

            self._reranker = get_reranker(
                str(getattr(self._store, "persist_dir", "") or ""))
        return self._reranker

    def _rerank(self, query: str, docs: list, top_k: int | None = None) -> list:
        if not docs:
            return docs
        return self.reranker.rerank(query, docs, top_k=top_k)

    @property
    def available(self) -> bool:
        """嵌入模型/向量库可用性（不可用时调用方应回退无 RAG 分支）。"""
        try:
            return self._store.available()
        except Exception:
            return False

    def invoke(
        self, query: str, where: dict | None = None, top_k: int | None = None,
        rerank: bool = True,
    ) -> list[SimpleNamespace]:
        """混合检索（dense + Tfidf 词法）融合，返回 SimpleNamespace 列表。

        rerank=False 供 query_for_ticker 的各子路使用：子路结果最后会合并再统一
        重排一次，逐路重排既浪费算力、又会让「本路第一名」压过合并后的全局第一名。
        """
        top_k = top_k or self._top_k

        # 1. dense 路径（无嵌入后端时 store.query 返回 []）
        dense: dict[str, SimpleNamespace] = {}
        rows = self._store.query(query, where=where, top_k=max(top_k * DENSE_MULT, 10))
        rows = sorted(rows, key=lambda r: (-float(r.get("score") or 0.0),
                                           str(r.get("id") or "")))[:top_k]
        for r in rows:
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

        docs = sorted(merged.values(), key=_rank_key)
        if not rerank:
            return docs[:top_k]
        # 重排吃的是「候选池」而不是 top_k：给大了才有换位的空间，给到 top_k
        # 只是把同一批人重新排一次，第一名永远是第一名，等于没重排。
        pool = docs[:max(top_k * RERANK_CANDIDATE_MULT, 20)]
        return self._rerank(query, pool, top_k=top_k)

    def _lexical_retrieve(self, query: str, where: dict | None = None,
                          top_k: int = 10) -> list[dict]:
        """TfidfVectorizer 词法粗召回，返回 [{id, page_content, metadata, lex_score}]。"""
        docs, vec, X = self._lex_index(where)
        if not docs:
            return []
        if vec is None:
            ql = query.lower()
            sims = [1.0 if ql in (d["page_content"] or "").lower() else 0.0
                    for d in docs]
        else:
            # 稀疏乘走多线程 BLAS 时累加顺序不定，同一查询的相似度会在第 7 位
            # 上下抖动，4 位小数取整后翻转并列名次 → 检索指标自己抖（实测 nDCG
            # 0.9436/0.9461 交替）。单线程毫秒级，直接锁死。
            with _single_thread():
                sims = (X @ vec.transform([query]).T).toarray().ravel()

        scored = sorted(zip(docs, sims),
                        key=lambda x: (-float(x[1]), str(x[0].get("id") or "")))
        out = []
        for d, s in scored[:top_k]:
            if s <= 0:
                break
            out.append({"id": d["id"], "page_content": d["page_content"],
                        "metadata": d["metadata"], "lex_score": float(s)})
        return out

    def _lex_index(self, where: dict | None):
        """按 where 分路建词法索引，返回 (docs, vec, X)；无 sklearn 时 vec 为 None。

        缓存粒度是 (库内文档数, where)：query_for_ticker 一次走五路，若每路都在
        调用时重算全语料 Tfidf，语料涨到 2.2 万块后单次检索从 0.7s 涨到 5s 以上
        （实测）。改为每路只建一次索引、后续查询只做一次稀疏乘。

        where 交给 chroma 过滤而不是本地全量拉取后筛：默认 all_docs 上限 1 万条，
        实测「披露文件」整路 13,504 条会被静默截掉，该路词法分全为 None——词法分
        缺失不会报错，只会让融合分退化成纯 dense，属于会静默劣化的失效模式。
        """
        try:
            n = self._store.count()
        except Exception:
            return [], None, None
        if n != self._lex_count:
            self._lex_cache.clear()
            self._lex_count = n
        key = tuple(sorted((str(k), str(v)) for k, v in (where or {}).items()))
        hit = self._lex_cache.get(key)
        if hit is not None:
            return hit

        docs = self._store.all_docs(limit=None, where=where)
        vec = X = None
        if docs:
            try:
                from sklearn.feature_extraction.text import TfidfVectorizer

                vec = TfidfVectorizer(
                    analyzer="char_wb", ngram_range=(1, 2),
                    min_df=1, max_features=5000, sublinear_tf=True,
                )
                X = vec.fit_transform([d["page_content"] or "" for d in docs])
            except Exception:  # noqa: BLE001 sklearn 缺失/异常 → 子串命中兜底
                vec = X = None
        self._lex_cache[key] = (docs, vec, X)
        return docs, vec, X

    @staticmethod
    def _fuse(dense_score: float | None, lex_score: float | None) -> float:
        return (W_LEX * float(dense_score or 0.0)
                + (1.0 - W_LEX) * float(lex_score or 0.0))

    def query_for_ticker(
        self, ticker: str, industry: str = "", top_k: int | None = None
    ) -> list[SimpleNamespace]:
        """按股票组装多路检索并合并：个股相关 + 宏观数据 + 宏观新闻 + 披露文件 + 政策 + 论文。

        个股路由用 where={"ticker": ticker} 精确过滤（研报/新闻/公告/股东大会）；
        宏观数据/新闻 ticker 为空串，需单独路由；披露文件按 ticker+kind、政策/论文按
        kind 单独路由，各只出 1 条。合并后按 score 降序，返回前 top_k。
        """
        top_k = top_k or self._top_k
        merged: dict[str, SimpleNamespace] = {}

        if ticker:
            for d in self.invoke(
                f"{ticker} {industry} 财务状况 行业分析 新闻 公告",
                where={"ticker": ticker},
                top_k=max(3, top_k),
                rerank=False,
            ):
                merged[d.id or d.page_content] = d

        for d in self.invoke(
            f"{industry} {_MACRO_DATA_QUERY}",
            where={"kind": "宏观数据"},
            top_k=2,
            rerank=False,
        ):
            merged[d.id or d.page_content] = d

        for d in self.invoke(_MACRO_NEWS_QUERY, where={"kind": "宏观新闻"}, top_k=2,
            rerank=False):
            merged[d.id or d.page_content] = d

        # 披露文件单开一路：个股路的查询词（财务状况/行业分析/新闻/公告）本身就是
        # 存量研报的章节名，10-K/20-F 的表格正文打不过它们，实测 600519.SS 与 AAPL
        # 的个股路 top-3 全是研报、零披露块。这里按 ticker+kind 精确捞 1 条兜住。
        if ticker:
            for d in self.invoke(
                f"{ticker} {industry} {_DISCLOSURE_QUERY}",
                where={"ticker": ticker, "kind": "披露文件"},
                top_k=1,
                rerank=False,
            ):
                merged[d.id or d.page_content] = d

        # 外部语料两路：政策与论文都不带 ticker，靠 kind 等值路由单独捞；
        # 各只取 1 条，不挤占个股与宏观的位置
        for d in self.invoke(f"{industry} {_POLICY_QUERY}", where={"kind": "政策"},
                             top_k=1, rerank=False):
            merged[d.id or d.page_content] = d

        for d in self.invoke(f"{industry} {_PAPER_QUERY}", where={"kind": "论文"},
                             top_k=1, rerank=False):
            merged[d.id or d.page_content] = d

        # 多路合并后才重排一次：让七路来的候选放在同一个池子里比较
        ranked = self._rerank(f"{ticker} {industry} 财务 行业 新闻 公告",
                              sorted(merged.values(), key=_rank_key))
        return _reserve_external(_cap_external_docs(ranked), ranked, top_k)

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


@contextmanager
def _single_thread():
    """BLAS/OpenMP 线程锁到 1；缺 threadpoolctl 时退化为不限制（不影响正确性）。"""
    try:
        from threadpoolctl import threadpool_limits
    except ImportError:
        yield
        return
    with threadpool_limits(limits=1):
        yield


def _cap_external_docs(docs: list[SimpleNamespace],
                       cap: int = EXTERNAL_DOC_CAP) -> list[SimpleNamespace]:
    """同一篇外部文档最多留 cap 条，按传入顺序（已按分数降序）保留靠前的。

    只作用于 corpus="external" 且带 doc_uid 的切片；存量研报/新闻没有 doc_uid，
    一条不删，行为与从前完全一致。
    """
    kept: list[SimpleNamespace] = []
    seen: dict[str, int] = {}
    for d in docs:
        meta = d.metadata or {}
        uid = str(meta.get("doc_uid") or "") if meta.get("corpus") == "external" else ""
        if not uid:
            kept.append(d)
            continue
        n = seen.get(uid, 0)
        if n >= cap:
            continue
        seen[uid] = n + 1
        kept.append(d)
    return kept


def _reserve_external(chosen: list[SimpleNamespace], pool: list[SimpleNamespace],
                      top_k: int) -> list[SimpleNamespace]:
    """给披露/政策/论文各留 1 个位置，候选存在但被分数切掉时替换最低位的非外部条目。

    只替换非外部类条目，所以外部类之间仍按分数排序；没有任何可替换位（例如前 top_k
    全是外部类）时不动，宁缺不挤。返回前重新按分数排序。
    """
    head = list(chosen[:top_k])
    keys = {d.id or d.page_content for d in head}

    for kind in _RESERVED_KINDS:
        if any(d.metadata.get("kind") == kind for d in head):
            continue
        cand = next((d for d in pool if d.metadata.get("kind") == kind
                     and (d.id or d.page_content) not in keys), None)
        if cand is None:
            continue
        slot = next((i for i in range(len(head) - 1, -1, -1)
                     if head[i].metadata.get("kind") not in _RESERVED_KINDS), None)
        if slot is None:
            if len(head) < top_k:
                head.append(cand)
                keys.add(cand.id or cand.page_content)
            continue
        keys.discard(head[slot].id or head[slot].page_content)
        head[slot] = cand
        keys.add(cand.id or cand.page_content)

    return sorted(head, key=_rank_key)


def _rank_key(doc: SimpleNamespace) -> tuple[float, str]:
    """分数降序、同分按 id 升序。融合分保留 4 位小数，同分很常见（实测 5 篇
    文档相似度完全相同），只按分数排序会让输出取决于候选集插入顺序。

    重排生效时以 `rerank_score` 为准——`_reserve_external` 收尾会重新排序，
    用融合分排会把刚重排好的顺序打回去。重排关闭/失败时两者等价。
    """
    meta = doc.metadata
    score = meta.get("rerank_score") if meta.get("reranked") else None
    if score is None:
        score = meta.get("score") or 0.0
    return (-float(score), str(doc.id or doc.page_content))


# ── 全局单例（对齐 get_knowledge_store 模式）────────────────────

_retriever: Optional[KnowledgeRetriever] = None


def get_knowledge_retriever() -> KnowledgeRetriever:
    global _retriever
    if _retriever is None:
        _retriever = KnowledgeRetriever()
    return _retriever
