"""二段重排 —— 用 cross-encoder 对候选切片重新打分。

一段（dense 向量 + TF-IDF 词法）擅长从 2 万条里捞回候选，但它是「查询与文档
各自编码再算相似度」，看不到两者的交互；cross-encoder 把 (query, passage)
成对喂进模型，精度更高，代价是不能预先建索引，只能对已召回的小池子算。

三道纪律：

1. **默认关闭**（`RERANK_ENABLED=false`）。不装模型、不改排序，行为与从前一致。
2. **v1 只读快照强制旁路**。`data/knowledge_db_v1` 是 L2 基线标的，任何排序变化
   都会让 0.9461 失效。这条不靠调用方自觉——构造函数按目录名硬拦。
3. **模型缺失 / 加载失败 / 打分异常一律退回原顺序**，不报错、不半途换序，
   并在 `metadata.reranked` / `rerank_note` 上把实际发生的事写出来。

排序之外**不改 `score`**：融合分仍是融合分，重排分单独放在 `rerank_score`。
下游（接地检查把 `metadata.score` 当事实）因此不会被一个开关悄悄改掉数值。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from app.utils.logger import logger

# 模型已经在本地缓存（decision() 会先查磁盘），加载时不需要也不允许再联网。
# 不加这两行，CrossEncoder 会先 HEAD 一次 hub 查更新：huggingface.co 在本机不可达，
# 5 次重试每次约 30s 连接超时，一次加载能卡掉两分半。embed.py 早就这么做了。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

MODEL_NAME = "BAAI/bge-reranker-base"
# 切片正文通常几百字；截断到 512 字覆盖绝大多数，避免长 10-K 块拖慢批次
MAX_PASSAGE_CHARS = 512
# v1 快照目录名里的标记：命中即强制旁路
V1_SNAPSHOT_MARKER = "knowledge_db_v1"
CANDIDATE_MULT = 4

ENABLED = "enabled"
DISABLED_BY_CONFIG = "disabled_by_config"
V1_SNAPSHOT = "v1_snapshot"
MODEL_MISSING = "model_missing"
SCORE_ERROR = "score_error"


def _model_cache_dir(model_name: str = MODEL_NAME) -> Path:
    from huggingface_hub.constants import HF_HUB_CACHE

    return Path(HF_HUB_CACHE) / ("models--" + model_name.replace("/", "--"))


def model_present(model_name: str = MODEL_NAME) -> bool:
    """只看磁盘，不加载模型——要在每次检索前问这个问题，必须便宜。"""
    root = _model_cache_dir(model_name)
    if not root.is_dir():
        return False
    for snap in (root / "snapshots").glob("*"):
        if any(snap.rglob("*.safetensors")) or any(snap.rglob("*.bin")):
            return True
    return False


class Reranker:
    """可插拔重排器。单个实例持有模型，进程内复用。"""

    def __init__(self, model_name: str = MODEL_NAME, *, enabled: bool | None = None,
                 store_dir: str | Path | None = None, settings=None):
        self._model_name = model_name
        self._store_dir = str(store_dir or "")
        self._enabled = _enabled_from_settings(settings) if enabled is None else bool(enabled)
        self._model = None
        self._load_failed = False
        self._lock = threading.RLock()

    # ── 决策 ──────────────────────────────────────────

    def decision(self) -> tuple[bool, str]:
        """返回 (是否启用, 原因)。原因直接写进每条结果的 metadata。"""
        if V1_SNAPSHOT_MARKER in self._store_dir:
            return False, V1_SNAPSHOT
        if not self._enabled:
            return False, DISABLED_BY_CONFIG
        if self._load_failed or not model_present(self._model_name):
            return False, MODEL_MISSING
        return True, ENABLED

    @property
    def available(self) -> bool:
        return self.decision()[0]

    def _ensure(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import CrossEncoder

                    logger.info("加载重排模型 %s ...", self._model_name)
                    self._model = CrossEncoder(self._model_name)
                    logger.info("重排模型就绪")
        return self._model

    # ── 排序 ──────────────────────────────────────────

    def rerank(self, query: str, docs: list, top_k: int | None = None) -> list:
        """对已召回的候选重新排序。任何失败都原样返回，不做半程改动。"""
        if not docs:
            return docs
        on, reason = self.decision()
        if not on:
            return self._annotate(docs, False, reason)

        try:
            model = self._ensure()
            pairs = [(query, _passage(d)) for d in docs]
            scores = [float(s) for s in model.predict(pairs)]
        except Exception as e:  # noqa: BLE001 重排是增强项，失败不能毁掉检索
            logger.warning("重排失败，保留原顺序: %s", e)
            self._load_failed = True
            return self._annotate(docs, False, SCORE_ERROR)

        order = sorted(range(len(docs)),
                       key=lambda i: (-scores[i], str(_key(docs[i]))))
        out = []
        for i in order:
            doc = docs[i]
            doc.metadata["rerank_score"] = round(scores[i], 4)
            doc.metadata["reranked"] = True
            doc.metadata["rerank_note"] = ENABLED
            out.append(doc)
        return out[:top_k] if top_k else out

    @staticmethod
    def _annotate(docs: list, reranked: bool, note: str) -> list:
        for d in docs:
            d.metadata["reranked"] = reranked
            d.metadata["rerank_note"] = note
        return docs


def _passage(doc) -> str:
    text = (getattr(doc, "page_content", "") or "").strip()
    return text[:MAX_PASSAGE_CHARS] or " "


def _key(doc):
    return getattr(doc, "id", "") or getattr(doc, "page_content", "")


def _enabled_from_settings(settings=None) -> bool:
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    return bool(getattr(settings, "RERANK_ENABLED", False))


# ── 全局单例（对齐 get_knowledge_store 模式）────────────────────

_reranker: Reranker | None = None


def get_reranker(store_dir: str | Path | None = None, *, settings=None) -> Reranker:
    """按库目录取实例。v1 与正式库的决策不同，所以目录变了就换一个实例。"""
    global _reranker
    key = str(store_dir or "")
    if _reranker is None or _reranker._store_dir != key:
        _reranker = Reranker(store_dir=store_dir, settings=settings)
    return _reranker


def reset_reranker() -> None:
    """测试用：丢掉单例，让下一个人重新读配置。"""
    global _reranker
    _reranker = None


__all__ = [
    "CANDIDATE_MULT", "DISABLED_BY_CONFIG", "ENABLED", "MODEL_MISSING",
    "MODEL_NAME", "Reranker", "SCORE_ERROR", "V1_SNAPSHOT", "get_reranker",
    "model_present", "reset_reranker",
]
