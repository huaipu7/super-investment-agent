"""Step 2 验证：三类源入库 + 检索质量 + 幂等性。

运行: D:\\python\\python.exe scripts\\verify_step2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag.ingest.reports import index_all_reports
from app.rag.store import get_knowledge_store

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


store = get_knowledge_store()
total = store.count()
check("知识库非空", total > 0, f"count={total}")

# 1. 各 kind 计数
kinds = {"研报": "研报", "宏观新闻": "宏观新闻", "宏观数据": "宏观数据",
         "新闻": "新闻", "公告": "公告", "股东大会": "股东大会"}
counts = {k: store.count(where={"kind": v}) for k, v in kinds.items()}
check("研报已入库", counts["研报"] > 0, f"n={counts['研报']}")
check("宏观新闻已入库", counts["宏观新闻"] > 0, f"n={counts['宏观新闻']}")
check("宏观数据已入库", counts["宏观数据"] > 0, f"n={counts['宏观数据']}")
check("个股新闻已入库", counts["新闻"] + counts["公告"] > 0,
      f"新闻={counts['新闻']} 公告={counts['公告']}")

# 2. 检索质量：个股问题命中研报
res = store.query("贵州茅台 白酒 营收 净利润 增长", top_k=3)
if res:
    print("  [研报检索] top:", [(r["metadata"].get("ticker"), round(r["score"], 3)) for r in res])
    check("个股检索命中研报", any(r["metadata"].get("kind") == "研报" for r in res),
          f"top={len(res)}")
    check("研报 chunk 含 ticker/date", bool(res[0]["metadata"].get("ticker"))
          and bool(res[0]["metadata"].get("date")),
          f"ticker={res[0]['metadata'].get('ticker')} date={res[0]['metadata'].get('date')}")
    check("研报 chunk 含 section", bool(res[0]["metadata"].get("section")),
          f"section={res[0]['metadata'].get('section')}")
else:
    check("个股检索命中研报", False, "空结果")

# 3. 宏观数据检索
res = store.query("居民消费价格 CPI 通胀 同比", where={"kind": "宏观数据"}, top_k=3)
if res:
    print("  [宏观数据检索]", res[0]["page_content"][:60])
    check("宏观数据快照可检索", "CPI" in res[0]["page_content"] or "居民消费" in res[0]["page_content"],
          f"score={res[0]['score']}")
else:
    check("宏观数据快照可检索", False, "空结果")

# 4. 宏观新闻检索
res = store.query("央行 货币政策 降准 流动性", where={"kind": "宏观新闻"}, top_k=2)
check("宏观新闻可检索", len(res) >= 1, f"n={len(res)}")

# 5. 泛查命中宏观（不加 where，验证跨类目混合检索）
res = store.query("货币政策 流动性 央行", top_k=5)
print("  [混合检索] kinds:", [r["metadata"].get("kind") for r in res])
check("混合检索含宏观", any(r["metadata"].get("kind") in ("宏观新闻", "宏观数据") for r in res),
      f"top={len(res)}")

# 6. 幂等性：研报重新索引 count 不变（id 稳定去重）
before = counts["研报"]
r = index_all_reports()
after = store.count(where={"kind": "研报"})
check("研报重扫幂等", after == before, f"before={before} after={after}")

print("=" * 50)
print(f"Step 2 验证: {passed}/{passed + failed} 通过")
sys.exit(0 if failed == 0 else 1)
