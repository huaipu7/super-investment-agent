"""Step 4 验证：报告层「九、知识库参考」章节。

运行: D:\\python\\python.exe scripts\\verify_step4.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.report.markdown import result_to_markdown  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _base_result() -> dict:
    return {
        "ticker": "600519.SS",
        "company_info": {"name": "贵州茅台", "industry": "白酒",
                         "country": "中国", "currency": "CNY"},
        "financial_analysis": {},
        "valuation_analysis": {},
        "sotp_analysis": {},
        "market_analysis": {},
        "competitive_analysis": {},
        "news_analysis": {"available": False},
        "meeting_analysis": {"available": False},
        "swot_analysis": {},
        "geopolitical_analysis": {},
        "supply_chain_analysis": {},
        "fact_checks": {},
    }


_SNIPPETS = [
    {
        "content": "央行：把促进经济稳定增长、物价合理回升作为货币政策的重要考量，"
                  "保持流动性合理充裕，进一步降低实体经济融资成本。",
        "metadata": {"kind": "宏观新闻", "ticker": "", "date": "2026-08-11",
                     "source": "东财全球财经快讯", "section": "宏观新闻",
                     "score": 0.6123},
    },
    {
        "content": "白酒行业营收增速放缓，头部酒企集中度进一步提升，渠道库存趋于合理。",
        "metadata": {"kind": "研报", "ticker": "600519.SS", "date": "2026-07-20",
                     "source": "data/reports/600519.SS_20260720_093000.md",
                     "section": "行业分析", "score": 0.5834},
    },
]


def main() -> None:
    # 1. available=True 渲染片段
    r = _base_result()
    r["knowledge"] = {
        "available": True,
        "source": "RAG 知识库 (Chroma + bge-small-zh-v1.5)",
        "count": len(_SNIPPETS),
        "snippets": _SNIPPETS,
    }
    md = result_to_markdown(r)

    check("available=True 含 ## 九、知识库参考", "## 九、知识库参考" in md)
    try:
        idx_8 = md.index("## 八、风险分析")
        idx_9 = md.index("## 九、知识库参考")
        check("九章位于八章之后", idx_9 > idx_8)
    except ValueError:
        check("九章位于八章之后", False, "标题缺失")
    check("片段标注来源/日期", "东财全球财经快讯" in md and "2026-08-11" in md)
    check("片段标注相关度", "相关度 0.612" in md)
    check("研报 source 只显示文件名", "600519.SS_20260720_093000.md" in md
          and "data/reports" not in md)
    check("片段内容摘录", "央行：把促进经济稳定增长" in md)

    # 2. available=False 渲染暂无
    r2 = _base_result()
    r2["knowledge"] = {"available": False, "reason": "知识库未启用", "snippets": []}
    md2 = result_to_markdown(r2)
    check("available=False 渲染暂无", "暂无知识库参考内容" in md2 and "知识库未启用" in md2)
    check("available=False 无片段详情", "相关度" not in md2)

    # 3. knowledge 字段缺失（老报告兼容）
    md3 = result_to_markdown(_base_result())
    check("knowledge 缺失时容错为暂无", "暂无知识库参考内容" in md3)

    # 4. 一~八章编号未重排（smoke_phase9 兼容）
    for ch in ("一", "二", "三", "四", "五", "六", "七", "八", "九"):
        check(f"含 ## {ch}、", f"## {ch}、" in md)

    # 5. 输出样例（人工核验）
    print("\n-- 知识库参考章节样例 --")
    seg = md[md.index("## 九、知识库参考"):]
    seg = seg.split("## 附录")[0]
    print(seg.strip())

    print("=" * 50)
    print(f"Step 4 验证: {passed}/{passed + failed} 通过")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
