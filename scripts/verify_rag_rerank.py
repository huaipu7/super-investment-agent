"""C1/C2 验证：可插拔重排器 + 知识库检索 API。

Part A —— 决策矩阵：默认关 / 缺模型 / v1 快照强制旁路 / 开启。
Part B —— 排序行为：重排改变顺序、失败退回原顺序、不改融合分。
Part C —— 检索器接线：单路与多路各重排一次，v1 库整体旁路。
Part D —— 检索 API：参数校验、过滤透传、分数拆解、空结果不补内容。
Part E —— 真模型（模型已下载时）：相关句显著高于不相关句；未下载则如实跳过。
Part F —— L2 基线：v1 快照下重排必须关闭（这条是硬规则）。

运行: D:\\python\\python.exe scripts/verify_rag_rerank.py
（A–D 全离线：用桩模型与桩库，不联网、不加载真模型）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name: str, ok, detail: str = ""):
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


class FakeModel:
    """按关键词给分的假 cross-encoder：命中 'target' 的段落排第一。"""

    def __init__(self, scores: dict[str, float] | None = None):
        self.calls: list[list[tuple[str, str]]] = []
        self.raises = False
        self.scores = scores or {}

    def predict(self, pairs):
        self.calls.append(list(pairs))
        if self.raises:
            raise RuntimeError("打分炸了")
        out = []
        for q, passage in pairs:
            s = self.scores.get(passage[:10])
            if s is None:
                s = 0.9 if "target" in passage else 0.1
            out.append(s)
        return out


def _docs(*pairs):
    return [SimpleNamespace(id=i, page_content=t, metadata={"score": s, "kind": k})
            for i, t, s, k in pairs]


class FakeStore:
    """最小 chroma 替身：够 retriever 跑完 dense + 词法两路。"""

    def __init__(self, persist_dir: str, rows: list[dict] | None = None):
        self.persist_dir = persist_dir
        self.available = True
        self.backend_label = "stub"
        self._rows = rows if rows is not None else ROWS

    def count(self, where=None):
        if not where:
            return len(self._rows)
        return len([r for r in self._rows
                    if all(r["metadata"].get(k) == v for k, v in where.items())])

    def query(self, text, where=None, top_k=5):
        rows = [r for r in self._rows
                if not where or all(r["metadata"].get(k) == v for k, v in where.items())]
        return rows[:top_k]

    def all_docs(self, limit=None, where=None):
        return [r for r in self._rows
                if not where or all(r["metadata"].get(k) == v for k, v in where.items())]


def _row(i: int, text: str, score: float, kind: str = "研报", ticker: str = "600519.SS"):
    return {"id": f"d{i}", "page_content": text,
            "metadata": {"kind": kind, "ticker": ticker, "date": "2026-01-01",
                         "source": "stub", "section": "章", "url": ""},
            "_s": score}


def _mk_rows():
    rows = [_row(1, "白酒行业整体增速放缓 target", 0.70),
            _row(2, "茅台提价传闻待证实", 0.90),
            _row(3, "渠道库存与动销数据", 0.80)]
    for r in rows:
        r.pop("_s")
    return rows


ROWS = _mk_rows()


def main():
    from app.rag import rerank as RR

    # ── Part A：决策矩阵 ─────────────────────────────────
    print("\n=== Part A: 什么时候才重排 ===")
    with mock.patch.object(RR, "model_present", return_value=True):
        on, why = RR.Reranker(enabled=True, store_dir="/x/data/knowledge_db").decision()
        check("开启 + 有模型 → 重排", on and why == RR.ENABLED, why)

        on, why = RR.Reranker(enabled=False, store_dir="/x/data/knowledge_db").decision()
        check("默认关 → 不重排", not on and why == RR.DISABLED_BY_CONFIG, why)

        on, why = RR.Reranker(enabled=True, store_dir="/x/data/knowledge_db_v1").decision()
        check("v1 只读快照 → 强制旁路", not on and why == RR.V1_SNAPSHOT, why)

        on, why = RR.Reranker(enabled=False, store_dir="/x/data/knowledge_db_v1").decision()
        check("v1 快照即使关着也报 v1 原因（不是配置）",
              not on and why == RR.V1_SNAPSHOT, why)

    on, why = RR.Reranker(enabled=True, model_name="no/such-model",
                          store_dir="/x/knowledge_db").decision()
    check("模型缺失 → 不重排且不报错", not on and why == RR.MODEL_MISSING, why)

    check("available 属性与 decision 一致",
          RR.Reranker(enabled=False, store_dir="/x/knowledge_db").available is False)
    import os

    check("导入后强制离线（否则每次加载都先联网查更新，卡 5 次重试）",
          os.environ.get("HF_HUB_OFFLINE") == "1"
          and os.environ.get("TRANSFORMERS_OFFLINE") == "1")

    # ── Part B：排序 ────────────────────────────────────
    print("\n=== Part B: 重排改变顺序，但不改融合分 ===")
    docs = _docs(("a", "普通内容", 0.90, "研报"),
                 ("b", "含 target 的段落", 0.50, "研报"),
                 ("c", "另一段", 0.70, "研报"))
    with mock.patch.object(RR, "model_present", return_value=True):
        r = RR.Reranker(enabled=True, store_dir="/x/knowledge_db")
        r._model = FakeModel()
        out = r.rerank("茅台", docs)
    check("命中段落排到第一", out[0].page_content == "含 target 的段落", str([d.id for d in out]))
    check("融合分原样保留（不把重排分写进 score）",
          out[0].metadata["score"] == 0.50, str(out[0].metadata))
    check("重排分单独一列", out[0].metadata["rerank_score"] == 0.9,
          str(out[0].metadata.get("rerank_score")))
    check("每条都标 reranked=True", all(d.metadata["reranked"] for d in out))
    check("注明原因 enabled", all(d.metadata["rerank_note"] == RR.ENABLED for d in out))

    docs = _docs(("a", "普通内容", 0.90, "研报"), ("b", "含 target 的段落", 0.50, "研报"))
    with mock.patch.object(RR, "model_present", return_value=True):
        r = RR.Reranker(enabled=False, store_dir="/x/knowledge_db")
        r._model = FakeModel()
        out = r.rerank("茅台", docs)
    check("关闭时顺序原样", [d.id for d in out] == ["a", "b"], str([d.id for d in out]))
    check("关闭时也标注 reranked=False",
          all(d.metadata["reranked"] is False for d in out)
          and out[0].metadata["rerank_note"] == RR.DISABLED_BY_CONFIG)

    docs = _docs(("a", "普通内容", 0.90, "研报"), ("b", "含 target 的段落", 0.50, "研报"))
    with mock.patch.object(RR, "model_present", return_value=True):
        r = RR.Reranker(enabled=True, store_dir="/x/knowledge_db")
        bad = FakeModel()
        bad.raises = True
        r._model = bad
        out = r.rerank("茅台", docs)
    check("打分出错 → 保留原顺序", [d.id for d in out] == ["a", "b"])
    check("打分出错 → 如实标注", out[0].metadata["rerank_note"] == RR.SCORE_ERROR)
    check("出错后不再尝试加载（避免每次检索都炸一遍）", r._load_failed is True)

    check("空候选原样返回", RR.Reranker(enabled=True, store_dir="/x/knowledge_db").rerank("q", []) == [])

    # ── Part C：检索器接线 ───────────────────────────────
    print("\n=== Part C: 检索器只在合适的地方重排 ===")
    from app.rag.retriever import KnowledgeRetriever

    store = FakeStore(str(Path(tempfile.mkdtemp(prefix="rr_")) / "knowledge_db"))
    with mock.patch.object(RR, "model_present", return_value=True):
        rk = RR.Reranker(enabled=True, store_dir=store.persist_dir)
        rk._model = FakeModel()
        ret = KnowledgeRetriever(store=store, reranker=rk)
        out = ret.invoke("茅台 提价", top_k=3)
    check("单路检索走重排", out[0].page_content.startswith("白酒行业整体增速放缓"),
          out[0].page_content[:16])
    check("候选池比分条数多时才可能换位", len(rk._model.calls[0]) >= 3,
          f"候选 {len(rk._model.calls[0])} 条")

    v1store = FakeStore(str(Path(tempfile.mkdtemp(prefix="rr_")) / "knowledge_db_v1"))
    with mock.patch.object(RR, "model_present", return_value=True):
        rk2 = RR.Reranker(enabled=True, store_dir=v1store.persist_dir)
        rk2._model = FakeModel()
        ret2 = KnowledgeRetriever(store=v1store, reranker=rk2)
        out2 = ret2.invoke("茅台 提价", top_k=3)
        tq = ret2.query_for_ticker("600519.SS", "白酒", top_k=3)
    check("v1 库：单路不重排", all(d.metadata["reranked"] is False for d in out2))
    check("v1 库：原因写明 v1_snapshot",
          out2[0].metadata["rerank_note"] == RR.V1_SNAPSHOT, out2[0].metadata["rerank_note"])
    check("v1 库：多路也不重排（不碰 L2 基线）",
          all(d.metadata.get("reranked") is False for d in tq), str(len(tq)))
    check("v1 库：桩模型一次都没被调用", rk2._model.calls == [], str(len(rk2._model.calls)))

    store3 = FakeStore(str(Path(tempfile.mkdtemp(prefix="rr_")) / "knowledge_db"))
    with mock.patch.object(RR, "model_present", return_value=True):
        rk3 = RR.Reranker(enabled=True, store_dir=store3.persist_dir)
        rk3._model = FakeModel()
        ret3 = KnowledgeRetriever(store=store3, reranker=rk3)
        ret3.query_for_ticker("600519.SS", "白酒", top_k=3)
    check("多路合并后只重排一次（不是每路一次）", len(rk3._model.calls) == 1,
          f"{len(rk3._model.calls)} 次")

    # ── Part D：检索 API ─────────────────────────────────
    print("\n=== Part D: /api/v1/search ===")
    from fastapi.testclient import TestClient

    import api.routes_search as RS

    store4 = FakeStore(str(Path(tempfile.mkdtemp(prefix="rr_")) / "knowledge_db"))
    with mock.patch.object(RS, "get_knowledge_store", lambda: store4), \
            mock.patch.object(RR, "model_present", return_value=False):
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(RS.router)
        c = TestClient(app)

        r = c.get("/api/v1/search", params={"q": "茅台"})
        body = r.json()
        check("检索 200", r.status_code == 200, str(r.status_code))
        check("返回条数与分数拆解",
              body["count"] == 3 and all("dense_score" in i for i in body["items"]))
        check("默认不重排：配置关是根因（不是模型缺失）",
              body["reranked"] is False
              and body["rerank_note"] == RR.DISABLED_BY_CONFIG, str(body["rerank_note"]))
        check("空 where 不虚构过滤", body["where"] == {})

        body_on = c.get("/api/v1/search", params={"q": "茅台", "rerank": "on"}).json()
        check("强制开 + 没有模型 → 如实说缺模型，且不静默降级成别的理由",
              body_on["reranked"] is False
              and body_on["rerank_note"] == RR.MODEL_MISSING, str(body_on["rerank_note"]))

        body_off = c.get("/api/v1/search", params={"q": "茅台", "rerank": "off"}).json()
        check("请求关掉 → 原因写「请求关的」，不冒充配置原因",
              body_off["rerank_note"] == RS.DISABLED_BY_REQUEST, str(body_off["rerank_note"]))

        body = c.get("/api/v1/search", params={"q": "茅台", "kind": "研报",
                                               "ticker": "600519.ss"}).json()
        check("过滤条件透传且标的大写归一",
              body["where"] == {"kind": "研报", "ticker": "600519.SS"}, str(body["where"]))

        check("非法 rerank → 400",
              c.get("/api/v1/search", params={"q": "x", "rerank": "maybe"}).status_code == 400)
        check("空查询 → 422", c.get("/api/v1/search", params={"q": ""}).status_code == 422)
        check("top_k 上限收敛",
              c.get("/api/v1/search", params={"q": "茅台", "top_k": 999}).json()["top_k"] == 50)

        empty = FakeStore(str(Path(tempfile.mkdtemp(prefix="rr_")) / "knowledge_db"), rows=[])
        with mock.patch.object(RS, "get_knowledge_store", lambda: empty):
            body = c.get("/api/v1/search", params={"q": "不存在"}).json()
        check("查不到就空列表 + 0 条，不补内容",
              body["items"] == [] and body["count"] == 0)

        st = c.get("/api/v1/search/stats").json()
        check("stats 给出总数与分类",
              st["count"] == 3 and st["kinds"].get("研报") == 3, str(st))
        check("stats 标出后端与可用性", st["available"] is True and st["backend"] == "stub")

    # ── Part E：真模型 ──────────────────────────────────
    print("\n=== Part E: 真模型（未下载则如实跳过）===")
    if not RR.model_present():
        print("[SKIP] 本地没有 BAAI/bge-reranker-base，跳过真模型打分")
    else:
        r = RR.Reranker(enabled=True, store_dir="/x/knowledge_db")
        real = _docs(("a", "贵州茅台宣布上调出厂价，毛利率有望提升", 0.5, "研报"),
                     ("b", "苹果公司发布新款手机，搭载自研芯片", 0.5, "研报"))
        out = r.rerank("茅台提价对毛利率的影响", real)
        hi = out[0].metadata["rerank_score"]
        lo = out[1].metadata["rerank_score"]
        check("相关段落得分更高", hi > lo, f"{hi} vs {lo}")
        check("相关段落排第一", "茅台" in out[0].page_content, out[0].page_content[:12])

    # ── Part F：L2 硬规则 ───────────────────────────────
    print("\n=== Part F: L2 基线不许动 ===")
    with mock.patch.object(RR, "model_present", return_value=True):
        check("v1 快照 + 显式开启 → 仍然不重排",
              RR.Reranker(enabled=True, store_dir=str(
                  ROOT / "data" / "knowledge_db_v1")).decision()[1] == RR.V1_SNAPSHOT)

    # ── Part G：检索页 ──────────────────────────────────
    print("\n=== Part G: 🔍 知识库检索页 ===")
    _ui_checks()

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


FIXTURE = {
    "query": "茅台 提价", "where": {}, "top_k": 10, "requested_rerank": "auto",
    "reranked": False, "rerank_note": "v1_snapshot", "count": 2,
    "items": [
        {"id": "d1", "content": "茅台提价传闻待证实", "score": 0.9,
         "dense_score": 0.9, "lex_score": 0.5, "rerank_score": None,
         "reranked": False, "rerank_note": "v1_snapshot",
         "metadata": {"kind": "研报", "ticker": "600519.SS", "date": "2026-01-01",
                      "source": "stub", "section": "章", "url": ""}},
        {"id": "d2", "content": "渠道库存与动销数据", "score": 0.8,
         "dense_score": 0.8, "lex_score": None, "rerank_score": None,
         "reranked": False, "rerank_note": "v1_snapshot",
         "metadata": {"kind": "政策", "ticker": "", "date": "", "source": "",
                      "section": "", "url": ""}},
    ],
}


def _ui_checks() -> None:
    from streamlit.testing.v1 import AppTest

    from ui import api_client as api
    from ui.pages.search import _stats

    _stats.clear()
    api.kb_stats = lambda: {"available": True, "backend": "本地 bge-small-zh-v1.5",
                            "count": 22943,
                            "kinds": {"研报": 2090, "披露文件": 13504, "政策": 1029}}
    api.kb_search = lambda *a, **k: FIXTURE

    at = AppTest.from_file(str(ROOT / "ui" / "dashboard.py"), default_timeout=240)
    at.run(timeout=240)
    labels = [str(o) for o in (at.radio[0].options or [])] if at.radio else []
    check("检索模块出现在导航里", "🔍 知识库检索" in labels, f"共 {len(labels)} 个模块")
    if "🔍 知识库检索" not in labels:
        return
    at.radio[0].set_value("🔍 知识库检索")
    at.run(timeout=240)
    if at.exception:
        check("检索页渲染无异常", False, str(at.exception))
        return
    check("检索页渲染无异常", True)

    blob = "\n".join(
        "" if getattr(el, "value", None) is None else str(el.value)
        for attr in ("markdown", "caption", "info", "warning", "error", "success")
        for el in (getattr(at, attr, []) or []))
    check("库规模显示为真实总数", "22943" in blob, blob[:80])
    check("分类构成可见", "披露文件 13504" in blob)
    check("表单齐全", len(at.text_input or []) >= 2 and len(at.slider or []) >= 1,
          f"input={len(at.text_input or [])} slider={len(at.slider or [])}")

    at.session_state["kb_res"] = FIXTURE
    at.run(timeout=240)
    if at.exception:
        check("结果区渲染无异常", False, str(at.exception))
        return
    res = "\n".join(
        "" if getattr(el, "value", None) is None else str(el.value)
        for attr in ("markdown", "caption", "info", "warning", "error", "success",
                     "dataframe", "text")
        for el in (getattr(at, attr, []) or []))
    check("结果区渲染无异常", True)
    check("命中条数与切片正文可见",
          "命中 2 条" in res and "茅台提价传闻待证实" in res)
    check("重排状态写明为什么没重排", "v1 只读快照强制旁路" in res, res[:120])
    check("分数拆成三列", "Dense" in res and "词法" in res and "重排分" in res)


if __name__ == "__main__":
    main()
