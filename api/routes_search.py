"""知识库检索 API —— /api/v1/search

解决「22,943 个切片进了库，用户摸不到」：给一个能直接查、能看见分数拆解、
看得见来源与日期的入口。

三个诚实点：
  * 重排是否发生写进响应的 `reranked` / `rerank_note`，不让「排序变了」变成暗箱；
  * `rerank=on` 也压不过 v1 快照的强制旁路——那是 L2 基线标的，硬规则；
  * 检索不到就说检索不到，空列表 + 实际生效的过滤条件，不补默认内容。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.rag.rerank import Reranker
from app.rag.retriever import KnowledgeRetriever
from app.rag.store import get_knowledge_store

router = APIRouter(prefix="/api/v1/search", tags=["search"])

KINDS = ("研报", "披露文件", "论文", "政策", "宏观新闻", "宏观数据", "新闻", "公告", "股东大会")
TOP_K_MAX = 50
DISABLED_BY_REQUEST = "disabled_by_request"
UNKNOWN_NOTE = "unknown"
_RERANK_MODES = {"auto": None, "on": True, "off": False}


# 每个重排模式各留一个长生存期的检索器：KnowledgeRetriever 内部按 (语料规模, where)
# 缓存 TF-IDF 索引，每次请求新建实例等于每查一次就把 2.2 万条重新向量化一遍
# （实测单次 18s，其中绝大部分耗在这）。按 store 身份失效，测试换库不会被旧缓存骗。
_RETRIEVERS: dict[str, tuple[object, KnowledgeRetriever]] = {}


def _retriever(store, mode: str) -> KnowledgeRetriever:
    hit = _RETRIEVERS.get(mode)
    if hit is not None and hit[0] is store:
        return hit[1]
    r = KnowledgeRetriever(
        store=store,
        reranker=Reranker(store_dir=getattr(store, "persist_dir", ""),
                          enabled=_RERANK_MODES[mode]))
    _RETRIEVERS[mode] = (store, r)
    return r


@router.get("/stats")
def stats():
    """库规模与各类切片数。分母是真的，查不到就是 0，不估。"""
    store = get_knowledge_store()
    out = {"available": bool(store.available), "backend": store.backend_label,
           "count": None, "kinds": {}}
    try:
        out["count"] = store.count()
    except Exception as e:                       # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    for k in KINDS:
        try:
            n = store.count(where={"kind": k})
        except Exception:                        # noqa: BLE001 单类查不了不影响总数
            n = None
        out["kinds"][k] = n
    return out


@router.get("")
def search(q: str = Query(..., min_length=1, description="查询文本"),
           kind: str = "", ticker: str = "", top_k: int = 10,
           rerank: str = "auto"):
    mode = (rerank or "auto").strip().lower()
    if mode not in _RERANK_MODES:
        raise HTTPException(status_code=400,
                            detail=f"rerank 只能是 auto / on / off，收到 {rerank!r}")
    top_k = max(1, min(int(top_k or 10), TOP_K_MAX))

    where: dict = {}
    if kind.strip():
        where["kind"] = kind.strip()
    if ticker.strip():
        where["ticker"] = ticker.strip().upper()

    store = get_knowledge_store()
    retriever = _retriever(store, mode)
    docs = retriever.invoke(q.strip(), where=where or None, top_k=top_k,
                            rerank=(mode != "off"))

    items = [_item(d) for d in docs]
    note = items[0]["rerank_note"] if items else retriever.reranker.decision()[1]
    if note is None:
        # rerank=off 时整条重排被跳过，没有逐条标注；这次是「请求关的」，
        # 不能说成「配置关的」，也不能留个 None 让 UI 猜。
        note = DISABLED_BY_REQUEST if mode == "off" else UNKNOWN_NOTE
    return {
        "query": q.strip(),
        "where": where,
        "top_k": top_k,
        "requested_rerank": mode,
        "reranked": any(i["reranked"] for i in items),
        "rerank_note": note,
        "count": len(items),
        "items": items,
    }


def _item(doc) -> dict:
    meta = dict(getattr(doc, "metadata", {}) or {})
    return {
        "id": getattr(doc, "id", "") or "",
        "content": getattr(doc, "page_content", "") or "",
        "score": meta.get("score"),
        "dense_score": meta.get("dense_score"),
        "lex_score": meta.get("lex_score"),
        "rerank_score": meta.get("rerank_score"),
        "reranked": bool(meta.get("reranked")),
        "rerank_note": meta.get("rerank_note"),
        "metadata": meta,
    }
