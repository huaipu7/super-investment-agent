"""嵌入后端 —— 远程（OpenAI 兼容）优先，本地 bge-small-zh-v1.5 兜底。

后端选择（get_embedding_function）：
  1. 配置了 EMBEDDING_API_KEY → RemoteEmbeddingFunction（httpx，OpenAI 兼容 /embeddings）
  2. 否则 → BGEEmbeddingFunction（torch 懒加载，模型已缓存于 ~/.cache/huggingface）
  3. 皆不可用 → available()=False，KnowledgeStore 静默回退"无 RAG"分支

本地 bge 加载失败时静默降级，不影响管线主流程。
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from app.config import get_settings
from app.utils.logger import logger

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
QUERY_PROMPT = "为这个句子生成表示以用于检索相关文章："

# 模型已本地缓存；强制离线，避免初始化时联网请求 HuggingFace 而长时间挂起
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


class BGEEmbeddingFunction:
    """chromadb 兼容的 EmbeddingFunction，内部延迟加载 bge-small-zh。

    - `__call__(input: list[str]) -> list[list[float]]`：chromadb 调用入口
    - `embed_texts(texts) -> list[list[float]]`：显式批量编码（归一化余弦向量）
    - `dim`：模型输出维度
    """

    def __init__(self, model_name: str = MODEL_NAME):
        self._model_name = model_name
        self._model = None
        self._dim: Optional[int] = None
        self._lock = threading.RLock()

    @staticmethod
    def name() -> str:
        """chromadb 持久化配置比对用（collection 必须与首次创建时的 EF 名称一致）。"""
        return f"bge-{MODEL_NAME.split('/')[-1]}"

    def _ensure(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    logger.info("加载嵌入模型 %s ...", self._model_name)
                    self._model = SentenceTransformer(self._model_name)
                    # 新版 API 为 get_embedding_dimension，旧版为 get_sentence_embedding_dimension
                    getter = getattr(self._model, "get_embedding_dimension", None) or getattr(
                        self._model, "get_sentence_embedding_dimension"
                    )
                    self._dim = getter()
                    logger.info("嵌入模型就绪, dim=%d", self._dim)
        return self._model

    def embed_texts(
        self, texts: list[str], *, for_query: bool = False
    ) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure()
        kwargs = {"batch_size": 32, "show_progress_bar": False}
        if for_query:
            # bge 官方推荐：query 加检索指令前缀，document 不加，可提升检索质量
            kwargs["prompt"] = QUERY_PROMPT
        vecs = model.encode(list(texts), normalize_embeddings=True, **kwargs)
        return [v.tolist() for v in vecs]

    def embed_query(self, input):
        """chromadb 查询用：对 query 加 bge 检索指令。"""
        if isinstance(input, str):
            input = [input]
        return self.embed_texts(input, for_query=True)

    def embed_documents(self, input):
        """chromadb 入库用：document 不加指令。"""
        return self.embed_texts(input)

    def __call__(self, input):
        return self.embed_documents(input)

    @property
    def dim(self) -> Optional[int]:
        self._ensure()
        return self._dim


class RemoteEmbeddingFunction:
    """OpenAI 兼容 /embeddings 远程嵌入（httpx）。

    配置了 EMBEDDING_API_KEY 时优先于本地 bge 使用。向量做 L2 归一化后
    返回（对齐 chroma cosine 距离假设）。dim 首次返回后缓存。
    """

    def __init__(self, api_base: str = "", api_key: str = "", model: str = "",
                 timeout: float = 30.0):
        self.api_base = (api_base or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key
        self.model = model or "text-embedding-3-small"
        self.timeout = timeout
        self._dim: Optional[int] = None

    @property
    def configured(self) -> bool:
        return bool((self.api_key or "").strip())

    def name(self) -> str:
        return "remote-openai-embeddings"

    @staticmethod
    def _normalize(vec: list[float]) -> list[float]:
        norm = sum(x * x for x in vec) ** 0.5
        return [x / norm for x in vec] if norm else vec

    def _request(self, texts: list[str]) -> list[list[float]]:
        import httpx

        resp = httpx.post(
            f"{self.api_base}/embeddings",
            json={"model": self.model, "input": texts},
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        items = sorted(data.get("data") or [], key=lambda x: x.get("index", 0))
        vecs = [self._normalize(item["embedding"]) for item in items]
        if vecs and self._dim is None:
            self._dim = len(vecs[0])
        return vecs

    def embed_texts(self, texts: list[str], *, for_query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        return self._request(list(texts))

    def embed_query(self, input):
        if isinstance(input, str):
            input = [input]
        return self.embed_texts(input)

    def embed_documents(self, input):
        return self.embed_texts(input)

    def __call__(self, input):
        return self.embed_documents(input)

    @property
    def dim(self) -> Optional[int]:
        if self._dim is None:
            raise RuntimeError("远程嵌入尚未获取过向量，dim 未知")
        return self._dim


# ── 全局单例（对齐 analysis_llm 模式）────────────────────────────

_ef: Optional[object] = None


def get_embedding_function():
    """选择可用嵌入后端：远程（配了 EMBEDDING_API_KEY）> 本地 bge。

    均不可用时返回 bge 实例（懒加载）；KnowledgeStore.available() 会据此
    返回 False，调用方静默回退无 RAG 分支，不崩溃。
    """
    global _ef
    if _ef is None:
        s = get_settings()
        if (s.EMBEDDING_API_KEY or "").strip():
            _ef = RemoteEmbeddingFunction(
                s.EMBEDDING_API_BASE, s.EMBEDDING_API_KEY, s.EMBEDDING_MODEL)
        else:
            _ef = BGEEmbeddingFunction()
    return _ef
