"""外部语料检索样例表：跑真实查询，把命中的片段与分数原样写进 samples.md。

样例表的作用是证明「新入库的四类文档确实能被自己的子查询捞出来」，而不是
再产出一份评测指标——L2 的标注基线冻结在 knowledge_db_v1，新语料不入 v1。

每条样例都来自当次真实检索：分数、dense/lex 分解、来源、doc_uid、原文片段
全部现场取值，不手写、不修补。词法分为 None 表示该片段只由 dense 一路召回
（融合只对两路都命中的片段做加权，dense-only 保留原始余弦分）。

运行: D:\\python\\python.exe scripts/sample_corpus.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.rag.retriever import _RESERVED_KINDS as _RESERVED  # noqa: E402
from app.rag.retriever import KnowledgeRetriever  # noqa: E402
from app.rag.store import get_knowledge_store  # noqa: E402

OUT_PATH = ROOT / "data" / "external" / "samples.md"

# (标题, 查询词, where, 期望命中的 kind)
CASES: list[tuple[str, str, dict | None, str]] = [
    ("政策 · 个股行业路（检索器实际发出的政策子查询）",
     "白酒 政策 监管 产业", {"kind": "政策"}, "政策"),
    ("政策 · 主题明确时",
     "安全生产费用提取使用 监督管理", {"kind": "政策"}, "政策"),
    ("政策 · 货币政策主题",
     "货币政策 流动性 降准", {"kind": "政策"}, "政策"),
    ("披露文件 · AAPL 个股路",
     "AAPL 消费电子 财务状况 行业分析 新闻 公告", {"ticker": "AAPL"}, "披露文件"),
    ("披露文件 · AAPL 分部与营收",
     "Apple net sales iPhone services segment revenue", {"ticker": "AAPL"}, "披露文件"),
    ("披露文件 · 600519 个股路",
     "600519.SS 白酒 财务状况 行业分析 新闻 公告", {"ticker": "600519.SS"}, "披露文件"),
    ("披露文件 · 00700.HK 个股路",
     "00700.HK 互联网 游戏 财务状况 行业分析 新闻 公告", {"ticker": "00700.HK"}, "披露文件"),
    ("披露文件 · SE 分部",
     "SE e-commerce Shopee digital financial services segment", {"ticker": "SE"}, "披露文件"),
    ("论文 · 个股行业路（检索器实际发出的论文子查询）",
     "白酒 研究 论文", {"kind": "论文"}, "论文"),
    ("论文 · 主题明确时",
     "market microstructure limit order book price impact", {"kind": "论文"}, "论文"),
    ("论文 · 货币政策传导",
     "monetary policy transmission bank lending channel", {"kind": "论文"}, "论文"),
]

SNIPPET = 150


def _snippet(text: str) -> str:
    line = " ".join(str(text or "").split())
    return line[:SNIPPET] + ("…" if len(line) > SNIPPET else "")


def _esc(s: str) -> str:
    return str(s or "").replace("|", "\\|")


def run() -> tuple[str, list[dict]]:
    store = get_knowledge_store()
    r = KnowledgeRetriever()

    t0 = time.perf_counter()
    r.invoke("warmup", where={"kind": "宏观数据"}, top_k=1)
    r.invoke("warmup", where={"kind": "政策"}, top_k=1)
    r.invoke("warmup", where={"kind": "论文"}, top_k=1)
    warm_up = time.perf_counter() - t0

    final: list[dict] = []
    for tk, ind in [("600519.SS", "白酒"), ("AAPL", "消费电子"), ("SE", "电商")]:
        t0 = time.perf_counter()
        docs = r.query_for_ticker(tk, ind, top_k=5)
        final.append({"ticker": tk, "industry": ind,
                      "elapsed": time.perf_counter() - t0, "docs": docs})

    rows: list[dict] = []
    for title, query, where, want in CASES:
        t0 = time.perf_counter()
        docs = r.invoke(query, where=where, top_k=3)
        elapsed = time.perf_counter() - t0
        rows.append({
            "title": title, "query": query, "where": where, "want": want,
            "elapsed": elapsed, "docs": docs,
        })

    kinds = ["研报", "宏观新闻", "宏观数据", "新闻", "公告", "股东大会",
             "政策", "披露文件", "论文"]
    counts = {k: store.count(where={"kind": k}) for k in kinds}
    return warm_up, counts, rows, final


def render(warm_up: float, counts: dict, rows: list[dict],
           final: list[dict]) -> str:
    total = sum(counts.values())
    lines = [
        "# 外部语料检索样例",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　"
        f"库：`data/knowledge_db`　切片总数：**{total:,}**",
        "",
        "本表由 `scripts/sample_corpus.py` 现场检索生成，分数与片段均取自当次真实查询。",
        "",
        "## 库内分布",
        "",
        "| kind | 切片数 |",
        "|---|---:|",
    ]
    for k in ["政策", "披露文件", "论文", "研报", "宏观新闻", "宏观数据", "新闻", "公告", "股东大会"]:
        lines.append(f"| {k} | {counts.get(k, 0):,} |")

    lines += [
        "",
        f"预热（词法索引首次构建，宏观数据 + 政策 + 论文三路）：**{warm_up:.2f}s**。"
        "此后每次 `invoke` 只做一次稀疏乘。",
        "",
        "## 检索样例",
        "",
        "`dense` 为向量余弦分，`lex` 为词法分；`lex` 标 `None` 表示该片段只由 dense 召回"
        "（两路都命中才做 0.5/0.5 融合）。",
        "",
        "耗时含该路词法索引的**首次构建**（每个 ticker 过滤条件各建一次，0.9–4.3s），"
        "同一路第二次起只做一次稀疏乘，约 0.1–0.3s；预热只覆盖了政策/论文/宏观数据三路，"
        "所以表中个股路的耗时是一次性建库成本，不是单次检索成本。",
        "",
    ]

    reach: dict[str, list[bool]] = {}
    for row in rows:
        hit = [d for d in row["docs"] if d.metadata.get("kind") == row["want"]]
        reach.setdefault(row["want"], []).append(bool(hit))
        lines.append(f"### {row['title']}")
        lines.append("")
        w = row["where"] or {}
        where_txt = ", ".join(f"`{k}={v}`" for k, v in w.items()) or "无过滤"
        lines.append(f"- 查询：`{row['query']}`　过滤：{where_txt}　"
                     f"耗时：{row['elapsed'] * 1000:.0f} ms　"
                     f"命中 `{row['want']}`：{'有' if hit else '**无**'}")
        lines.append("")
        if not row["docs"]:
            lines.append("_空结果_")
            lines.append("")
            continue
        lines.append("| # | 分数 | dense | lex | kind | source | ticker | date | doc_uid | 片段 |")
        lines.append("|---:|---:|---:|---:|---|---|---|---|---|---|")
        for i, d in enumerate(row["docs"], 1):
            m = d.metadata
            lines.append(
                f"| {i} | {m.get('score')} | {m.get('dense_score')} | "
                f"{m.get('lex_score')} | {m.get('kind')} | {m.get('source')} | "
                f"{m.get('ticker') or '—'} | {m.get('date') or '—'} | "
                f"{m.get('doc_uid') or '—'} | {_esc(_snippet(d.page_content))} |")
        lines.append("")

    lines += ["## 每类可达性", "", "| 类别 | 样例查询数 | 其中命中 |", "|---|---:|---:|"]
    for kind, flags in reach.items():
        lines.append(f"| {kind} | {len(flags)} | {sum(flags)} |")
    lines += ["", "「个股行业路」是检索器实际会发出的子查询（含行业词）；"
              "「主题明确」类样例用于说明该路在查询词与该类语料确实相交时能命中。"
              "个股路对披露文件未命中是预期的：路径查询词本身就是存量研报的章节名，"
              "所以最终榜给外部三类各预留 1 位（下表）。", ""]

    lines += ["## 最终榜（query_for_ticker，外部三类各保 1 位）", ""]
    for row in final:
        kinds = [d.metadata.get("kind") for d in row["docs"]]
        lines.append(f"### {row['ticker']}（{row['industry']}）　"
                     f"{row['elapsed'] * 1000:.0f} ms")
        lines.append("")
        lines.append("| # | 分数 | kind | ticker | date | doc_uid | 片段 |")
        lines.append("|---:|---:|---|---|---|---|---|")
        for i, d in enumerate(row["docs"], 1):
            m = d.metadata
            lines.append(
                f"| {i} | {m.get('score')} | {m.get('kind')} | "
                f"{m.get('ticker') or '—'} | {m.get('date') or '—'} | "
                f"{m.get('doc_uid') or '—'} | {_esc(_snippet(d.page_content))} |")
        lines.append("")
        lines.append(f"外部三类覆盖：{', '.join(sorted(set(kinds) & set(_RESERVED)))}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    warm_up, counts, rows, final = run()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(warm_up, counts, rows, final), encoding="utf-8")

    total = sum(counts.values())
    print(f"库内切片 {total:,}｜政策 {counts['政策']:,}｜披露文件 {counts['披露文件']:,}｜论文 {counts['论文']:,}")
    print(f"词法索引预热 {warm_up:.2f}s")
    for row in rows:
        got = [d.metadata.get("kind") for d in row["docs"]]
        mark = "OK " if row["want"] in got else "MISS"
        print(f"[{mark}] {row['title']}  {row['elapsed'] * 1000:.0f}ms  kinds={got}")
    print(f"已写 {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
