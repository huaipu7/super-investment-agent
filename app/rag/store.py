"""Chroma 向量库封装 —— 持久化到 data/knowledge_db/，collection invest。

统一 item schema（全包对外接口）：
    {
        "id": str,                      # 可选；缺省由 source+content 哈希生成（天然去重）
        "page_content": str,            # 切块后的正文
        "metadata": {
            "kind": str,                # 研报 | 宏观新闻 | 宏观数据 | 新闻 | 公告 | 股东大会
            "ticker": str,              # 关联股票代码（宏观/行业可空字符串）
            "date": str,                # YYYY-MM-DD
            "source": str,              # 来源说明
            "section": str,             # 章节名/小节名
            "url": str,                 # 原文链接（可为空）
        },
    }

注意：Chroma metadata 值只接受标量（str/int/float/bool），url/ticker 等一律 str。
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any, Optional

from app.config import get_settings
from app.rag.embed import RemoteEmbeddingFunction, get_embedding_function
from app.utils.logger import logger

COLLECTION_NAME = "invest"


def doc_id(*parts: str) -> str:
    """内容哈希 id，同一 source+正文 入库天然去重。"""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


class KnowledgeStore:
    def __init__(self, persist_dir: str | Path | None = None):
        s = get_settings()
        self._persist_dir = (
            Path(persist_dir)
            if persist_dir
            else Path(s.base_dir / "data" / "knowledge_db")
        )
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._ef = get_embedding_function()
        self._client: Any = None
        self._collection: Any = None
        self._lock = threading.RLock()

    # ── 连接（延迟初始化，首次调用才拉起 chromadb 持久化 client）────

    def _ensure(self):
        if self._collection is None:
            with self._lock:
                if self._collection is None:
                    import chromadb
                    from chromadb.config import Settings as ChromaSettings

                    self._client = chromadb.PersistentClient(
                        path=str(self._persist_dir),
                        settings=ChromaSettings(anonymized_telemetry=False),
                    )
                    self._collection = self._client.get_or_create_collection(
                        name=COLLECTION_NAME,
                        embedding_function=self._ef,
                        metadata={"hnsw:space": "cosine"},
                    )
        return self._collection

    @property
    def backend_label(self) -> str:
        """当前嵌入后端标签（供报告溯源）。"""
        if isinstance(self._ef, RemoteEmbeddingFunction):
            return "远程嵌入 API"
        return "本地 bge-small-zh-v1.5"

    def available(self) -> bool:
        """嵌入后端可用性（不可用时调用方回退无 RAG 分支，不崩溃）。

        远程：看是否配置了 key（网络可用性在 query 时兜底降级）；
        本地 bge：torch 加载失败 → False。
        """
        try:
            if isinstance(self._ef, RemoteEmbeddingFunction):
                return self._ef.configured
            self._ef.dim
            return True
        except Exception:
            return False

    def all_docs(self, limit: int = 10000) -> list[dict]:
        """取集合内全部文档 [{id, page_content, metadata}]，供词法检索粗召回。

        col.get 不触发嵌入，无后端时也能用（词法降级路径依赖它）。
        """
        col = self._ensure()
        try:
            res = col.get(include=["documents", "metadatas"], limit=limit)
        except Exception as e:
            logger.warning("知识库 all_docs 失败: %s", e)
            return []
        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        out = []
        for i in range(len(ids)):
            out.append({
                "id": ids[i],
                "page_content": docs[i] or "",
                "metadata": metas[i] or {},
            })
        return out

    # ── 写入 ─────────────────────────────────────────────────────────

    def upsert(self, docs: list[dict], batch: int = 128) -> int:
        """批量 upsert（按 id 去重覆盖）。返回成功条数。"""
        if not docs:
            return 0
        col = self._ensure()
        added = 0
        for i in range(0, len(docs), batch):
            chunk = docs[i : i + batch]
            ids = [
                d.get("id")
                or doc_id(d.get("metadata", {}).get("source", ""), d["page_content"])
                for d in chunk
            ]
            documents = [d["page_content"] for d in chunk]
            metadatas = [self._sanitize_meta(d.get("metadata", {})) for d in chunk]
            try:
                col.upsert(ids=ids, documents=documents, metadatas=metadatas)
                added += len(chunk)
            except Exception as e:
                logger.warning("知识库 upsert 失败（本批 %d 条）: %s", len(chunk), e)
        return added

    # ── 查询 ─────────────────────────────────────────────────────────

    def query(self, text: str, where: dict | None = None, top_k: int = 5) -> list[dict]:
        """按文本相似度检索，返回 [{id, score, metadata, page_content}]。

        score 为余弦相似度（越大越相关），由 1 - cosine_distance 换算。
        """
        if not text or not self.available():
            return []
        col = self._ensure()
        try:
            res = col.query(
                query_texts=[text],
                where=where,
                n_results=top_k,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            logger.warning("知识库 query 失败: %s", e)
            return []
        return self._pack(res)

    def query_embeddings(
        self, embeddings: list[float], where: dict | None = None, top_k: int = 5
    ) -> list[dict]:
        col = self._ensure()
        try:
            res = col.query(
                query_embeddings=[embeddings],
                where=where,
                n_results=top_k,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            logger.warning("知识库 query_embeddings 失败: %s", e)
            return []
        return self._pack(res)

    def count(self, where: dict | None = None) -> int:
        """计数。chromadb 1.5.8 的 count() 不接受 where，带过滤时改用 get 取 ids 数。"""
        if not self.available():
            return 0
        col = self._ensure()
        try:
            if where:
                return len(col.get(where=where, include=[])["ids"])
            return col.count()
        except Exception as e:
            logger.warning("知识库 count 失败: %s", e)
            return 0

    def delete(self, where: dict):
        col = self._ensure()
        try:
            col.delete(where=where)
        except Exception as e:
            logger.warning("知识库 delete 失败: %s", e)

    def delete_all(self):
        """清空整个集合（重置/测试用）。"""
        try:
            self._ensure()
            self._client.delete_collection(COLLECTION_NAME)
            self._collection = None
        except Exception as e:
            logger.warning("知识库 delete_all 失败: %s", e)

    # ── 内部 ─────────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_meta(meta: dict) -> dict:
        """把元数据规整为 Chroma 可接受的标量 dict。"""
        out: dict[str, Any] = {}
        for k, v in meta.items():
            if isinstance(v, (str, int, float, bool)):
                out[k] = v
            elif v is None:
                out[k] = ""
            else:
                out[k] = str(v)
        return out

    @staticmethod
    def _pack(res) -> list[dict]:
        """Chroma query 结果 → [{id, score, metadata, page_content}]。

        空结果时 Chroma 可能返回空 ids/distances，需防御。
        """
        items = []
        ids = res.get("ids") or [[]]
        if not ids or not ids[0]:
            return items
        n = len(ids[0])
        dists = (res.get("distances") or [[None] * n])[0]
        metas = (res.get("metadatas") or [[None] * n])[0]
        docs = (res.get("documents") or [[None] * n])[0]
        for k in range(n):
            distance = dists[k]
            items.append(
                {
                    "id": ids[0][k],
                    "score": round(1.0 - distance, 4) if distance is not None else None,
                    "metadata": metas[k] or {},
                    "page_content": docs[k],
                }
            )
        return items


# ── 全局单例 ─────────────────────────────────────────────────────────

_store: Optional[KnowledgeStore] = None


def get_knowledge_store() -> KnowledgeStore:
    global _store
    if _store is None:
        _store = KnowledgeStore()
    return _store
