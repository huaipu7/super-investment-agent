"""Phase C2 验证：RAG 生产化（远程嵌入 / 默认可开 / 混合检索 / 降级不崩）。

覆盖：
  (a) 配置默认：RAG_ENABLED=True、EMBEDDING_* 字段存在
  (b) RemoteEmbeddingFunction：mock httpx → 归一化向量、dim 缓存、configured
  (c) 知识库入库 + 检索（mock 嵌入）：结果带 source/date 元数据、score 为融合分
  (d) query_for_ticker 按 ticker 过滤
  (e) 无嵌入后端时降级：available=False、query 返回 []、invoke 仍返回列表不崩
  (f) ingest 脚本切块入库路径（.md/.txt → upsert → count 增加）

运行: D:\\python\\python.exe scripts\\verify_rag.py
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


class _MockEF:
    """确定性 mock 嵌入（文本哈希 → 归一化向量），离线可复现。"""

    def __init__(self, dim: int = 8):
        self._dim = dim
        self._cache: dict[str, list[float]] = {}

    def name(self):
        return "mock-ef"

    @property
    def dim(self):
        return self._dim

    def _vec(self, text: str) -> list[float]:
        if text not in self._cache:
            h = hashlib.sha256(text.encode("utf-8")).digest()
            v = list(h[: self._dim])
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            self._cache[text] = [x / norm for x in v]
        return self._cache[text]

    def embed_documents(self, input):
        return [self._vec(t) for t in input]

    def embed_query(self, input):
        if isinstance(input, str):
            input = [input]
        return [self._vec(t) for t in input]

    def __call__(self, input):
        return self.embed_documents(input)


class _BrokenEF:
    def name(self):
        return "broken-ef"

    @property
    def dim(self):
        raise RuntimeError("无嵌入后端")

    def embed_documents(self, input):
        raise RuntimeError("无嵌入后端")

    def embed_query(self, input):
        raise RuntimeError("无嵌入后端")

    def __call__(self, input):
        raise RuntimeError("无嵌入后端")


def main():
    import app.config as cfg

    # ── (a) 配置默认（先清掉 .env 注入的 RAG_ENABLED，测纯默认值）──
    saved = os.environ.pop("RAG_ENABLED", None)
    try:
        s = cfg.Settings()
    finally:
        if saved is not None:
            os.environ["RAG_ENABLED"] = saved
    check("RAG_ENABLED 默认 True", s.RAG_ENABLED is True, f"={s.RAG_ENABLED}")
    check("EMBEDDING_* 字段存在",
          hasattr(s, "EMBEDDING_API_BASE") and hasattr(s, "EMBEDDING_API_KEY")
          and hasattr(s, "EMBEDDING_MODEL"))

    # ── (b) RemoteEmbeddingFunction（mock httpx）──
    from app.rag.embed import RemoteEmbeddingFunction

    ef = RemoteEmbeddingFunction(api_base="http://fake/v1", api_key="k", model="m")
    check("remote configured", ef.configured is True)

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [
                {"index": 1, "embedding": [3.0, 4.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]}

    with mock.patch("httpx.post", return_value=_FakeResp()):
        vecs = ef.embed_texts(["b", "a"])
    check("remote 返回 2 个向量", len(vecs) == 2)
    check("remote 归一化", abs(sum(x * x for x in vecs[0]) - 1.0) < 1e-6)
    check("remote 按 index 排序", vecs[0][0] == 1.0, str(vecs[0]))
    check("remote dim 缓存", ef.dim == 2)

    # ── (c)(d) 知识库 + 混合检索（mock 嵌入）──
    from app.rag.retriever import KnowledgeRetriever
    from app.rag.store import KnowledgeStore, doc_id

    tmp = Path(tempfile.mkdtemp(prefix="verify_rag_"))
    # store.py 用 from-import 持有引用，须同时 patch 其命名空间才生效（避免真加载 bge）
    with mock.patch("app.rag.embed.get_embedding_function", lambda: _MockEF()), \
            mock.patch("app.rag.store.get_embedding_function", lambda: _MockEF()):
        store = KnowledgeStore(persist_dir=tmp / "kb")
        check("store available", store.available() is True)
        check("backend_label", store.backend_label == "本地 bge-small-zh-v1.5",
              store.backend_label)  # 非 remote 后端按 bge 标注

        docs = [
            {"id": doc_id("研报", "r1", "茅台 2024 营收增长 15%，净利率提升"),
             "page_content": "茅台 2024 营收增长 15%，净利率提升，现金流强劲",
             "metadata": {"kind": "研报", "ticker": "600519.SS", "date": "2026-08-01",
                          "source": "data/reports/600519_20260801.md", "section": "业绩"}},
            {"id": doc_id("研报", "r2", "五粮液 2024 营收增长 8%，毛利率改善"),
             "page_content": "五粮液 2024 营收增长 8%，毛利率改善，渠道库存去化",
             "metadata": {"kind": "研报", "ticker": "000858.SZ", "date": "2026-07-20",
                          "source": "data/reports/000858_20260720.md", "section": "业绩"}},
            {"id": doc_id("宏观新闻", "m1", "央行降准 0.5 个百分点，释放长期资金"),
             "page_content": "央行宣布降准 0.5 个百分点，释放长期资金约 1 万亿元",
             "metadata": {"kind": "宏观新闻", "ticker": "", "date": "2026-08-10",
                          "source": "news/20260810.md", "section": ""}},
        ]
        n = store.upsert(docs)
        check("upsert 3 条", n == 3, f"={n}")

        r = KnowledgeRetriever(store=store, top_k=5)
        res = r.invoke("茅台 2024 营收 净利润 增长")
        check("invoke 返回非空", len(res) > 0, f"{len(res)} 条")
        check("invoke 结果带 source", any((d.metadata.get("source") or "").startswith("data/reports") for d in res))
        check("invoke 结果带 score(融合)", all(d.metadata.get("score") is not None for d in res))
        check("invoke 结果含 lex_score", any(d.metadata.get("lex_score") is not None for d in res))

        mt = r.query_for_ticker("600519.SS", "白酒", top_k=3)
        check("query_for_ticker 按 ticker 过滤",
              len(mt) > 0 and all(d.metadata.get("ticker") in ("600519.SS", "") for d in mt),
              f"{len(mt)} 条")

        # 关键词查询 → 词法路径也应命中
        lex = r.invoke("央行 降准 长期资金")
        check("词法命中宏观新闻", any("降准" in (d.page_content or "") for d in lex))

        # ── (e) 无嵌入后端降级 ──
        store._ef = _BrokenEF()
        check("degrade: available=False", store.available() is False)
        check("degrade: query 返回 []", store.query("茅台") == [])
        res2 = r.invoke("茅台 营收")
        check("degrade: invoke 仍返回列表不崩", isinstance(res2, list), f"len={len(res2)}")

        # ── (f) ingest 脚本切块入库路径 ──
        from scripts.ingest_docs import _chunk_file
        from app.rag.chunking import chunk_text

        indir = tmp / "in"
        indir.mkdir()
        (indir / "note.md").write_text(
            "# 白酒行业\n\n## 行业景气\n\n白酒行业景气度回升，渠道库存去化。\n",
            encoding="utf-8")
        (indir / "note.txt").write_text(
            "贵州茅台 2024 年营收破 1700 亿，净利率保持高位。", encoding="utf-8")

        store._ef = _MockEF()  # 恢复可用后端
        total_added = 0
        for f in sorted(indir.glob("*")):
            m = {"kind": "研报", "ticker": "600519.SS", "date": "2026-08-14",
                 "source": f.name, "section": "", "url": ""}
            dchunks = _chunk_file(f, m)
            for d in dchunks:
                d["id"] = doc_id("研报", f.name, d["page_content"])
            total_added += store.upsert(dchunks)
        check("ingest 路径可入库", total_added > 0, f"新增 {total_added} 条")
        check("ingest 后 count 增加", store.count() > 3, f"={store.count()}")

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
