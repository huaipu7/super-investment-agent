"""Step 1 验证：Chroma 建库 + bge-small-zh 嵌入 + 切块。

运行: PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\verify_step1.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag.chunking import _sliding_chunks, chunk_markdown_text, chunk_text
from app.rag.store import KnowledgeStore, doc_id

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


tmp = tempfile.mkdtemp(prefix="kb_step1_")
store = KnowledgeStore(persist_dir=tmp)
print(f"临时库: {tmp}")

docs = [
    {
        "page_content": "贵州茅台2025年营收同比增长15%，直销渠道占比提升，净利润增速放缓。",
        "metadata": {"kind": "研报", "ticker": "600519.SS", "date": "2026-08-11",
                     "source": "历史研报", "section": "财务", "url": ""},
    },
    {
        "page_content": "白酒行业竞争格局：CR5 集中度提升，头部企业定价权增强，高端酒量价齐升。",
        "metadata": {"kind": "新闻", "ticker": "600519.SS", "date": "2026-08-01",
                     "source": "东财新闻", "section": "行业", "url": "http://x"},
    },
    {
        "page_content": "央行宣布下调存款准备金率50个基点，释放流动性约一万亿，利好消费板块估值修复。",
        "metadata": {"kind": "宏观数据", "ticker": "", "date": "2026-08-10",
                     "source": "新闻联播", "section": "货币", "url": "http://y"},
    },
]

n = store.upsert(docs)
check("插入 3 条", n == 3, f"upsert={n}")
check("count = 3", store.count() == 3, f"count={store.count()}")

res = store.query("茅台 白酒 营收 增长", top_k=3)
check("文本相似度检索返回", len(res) >= 1, f"top={len(res)}")
if res:
    print("   相似度分数:", [r["score"] for r in res])
    check("top1 关联 600519", res[0]["metadata"]["ticker"] == "600519.SS",
          f"top1 ticker={res[0]['metadata']['ticker']} score={res[0]['score']}")
    check("检索结果含 page_content", bool(res[0]["page_content"]), "")

res = store.query("流动性 释放", where={"kind": "宏观数据"}, top_k=2)
check("where 过滤宏观数据", len(res) == 1 and res[0]["metadata"]["kind"] == "宏观数据",
      f"n={len(res)}")
res = store.query("白酒 行业 竞争", where={"kind": "研报"}, top_k=2)
check("where 过滤研报", len(res) >= 1 and all(r["metadata"]["kind"] == "研报" for r in res),
      f"n={len(res)}")

n2 = store.upsert(docs)
check("重复 upsert 去重", store.count() == 3, f"count={store.count()} upsert2={n2}")
check("doc_id 稳定", doc_id("a", "b") == doc_id("a", "b"), "")

chunks = chunk_text("这是第一段。\n\n这是第二段，内容稍微长一点。", {"kind": "新闻"})
check("chunk_text", len(chunks) >= 1 and all("kind" in c["metadata"] for c in chunks),
      f"n={len(chunks)}")

md = """# 贵州茅台深度研报
## 一、核心观点
公司基本面稳健，品牌力强。
## 二、财务分析
营收增长，毛利率提升，ROE 高企。
## 三、风险分析
政策风险，消费疲软。
"""
mc = chunk_markdown_text(md, {"kind": "研报", "ticker": "600519.SS"})
secs = sorted({c["metadata"]["section"] for c in mc})
check("chunk_markdown 分节", len(secs) >= 3, f"sections={secs}")

longp = "长" * 1200
lc = _sliding_chunks(longp, size=400, overlap=80)
check("滑窗切分长文本", len(lc) >= 3 and all(len(c) <= 400 for c in lc),
      f"n={len(lc)} 最大块={max(len(c) for c in lc)}")

check("空文本 query 防御", store.query("") == [], "")
check("嵌入模型可用", store.available(), f"dim={store._ef.dim}")

print("=" * 50)
print(f"Step 1 验证: {passed}/{passed + failed} 通过")
sys.exit(0 if failed == 0 else 1)
