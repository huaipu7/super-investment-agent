"""生成 L2 检索相关性标注表（人工标注用，脚本不代填）。

为什么需要它：`eval_retrieval.py` 现在那 3 条查询是自造夹具，top_k 覆盖
全部语料，召回率恒为 1.0 —— 不度量任何东西。要变成真指标，必须有人对
真实语料里的真实问题打相关性分。这一步只能人做，脚本只负责把「要判断的
东西」摆整齐，绝不预填任何分数（预填 = 诱导 + 伪造评测）。

设计要点：
  1 每条查询给 4 篇系统召回 + 2 篇**盲抽**（系统没召回、从全库随机取）。
    盲抽是关键：只标系统召回的文档，等于让系统自己出考题，永远发现不了
    漏召回。盲抽若被标为相关，就是一次真实的检索失败。
  2 候选取自真实检索路径（dense + 词法融合 + 时间衰减），不是离线 mock。
  3 随机种子固定，重跑不换题。

用法:
    D:\\python\\python.exe scripts/make_label_sheet.py            # 生成标注表
    D:\\python\\python.exe scripts/make_label_sheet.py --collect   # 收表并校验

生成: data/eval/l2_label_sheet.md   （你在编辑器里填「判：n」）
收录: data/eval/l2_labels.json      （填完后 --collect 生成）
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHEET_PATH = ROOT / "data" / "eval" / "l2_label_sheet.md"
LABELS_PATH = ROOT / "data" / "eval" / "l2_labels.json"

TOP_K = 4          # 与 eval_retrieval 的评测 k 一致
BLIND = 2          # 每条查询额外给 2 篇系统没召回的
PREVIEW = 110      # 预览字数（够判断主题，不够就不用翻原文）
SEED = 20261104

# 候选查询：都是从真实语料能回答的问题（语料 = AAPL/600519/SE 历史报告 + 宏观快讯）。
# 你不必全标——删掉不关心的行即可，但每条保留下来的查询至少要有一个分。
QUERIES = [
    "贵州茅台 2025年 营业收入 净利润",
    "茅台 毛利率 ROE 财务指标",
    "贵州茅台 分红 派息 股息率",
    "茅台 市盈率 PE 估值水平",
    "贵州茅台 回购 公告",
    "茅台 五粮液 竞争格局 对比",
    "白酒行业 市场份额 集中度 CR5",
    "贵州茅台 股东大会 议案",
    "茅台 产能 扩产 基酒",
    "茅台 出厂价 提价 渠道库存",
    "AAPL 苹果 营收 净利润",
    "苹果 服务业务 毛利率 收入占比",
    "苹果 iPhone 出货量 销量",
    "苹果 股票回购 share repurchase",
    "苹果 中国市场 销售 下滑",
    "AAPL valuation PE ratio target price",
    "苹果 供应链 关税 影响",
    "Sea Limited 收入 增长",
    "Shopee 电商 GMV 市场份额",
    "宏观 利率 政策 央行",
    "中国 CPI 通胀 数据",
    "美联储 降息 加息 预期",
    "人民币 汇率 波动",
    "DCF 估值 WACC 假设 敏感性",
    "分红 派息 政策 监管",
]


def _preview(text: str) -> str:
    t = " ".join((text or "").split())
    return t[:PREVIEW] + ("…" if len(t) > PREVIEW else "")


def _short_id(doc_id: str, meta: dict) -> str:
    """chroma 的 id 是 uuid，标注表里用短号更好读；短号写回 json 时再还原。"""
    return (doc_id or "")[:8]


def build_sheet() -> tuple[str, dict]:
    from app.rag.retriever import KnowledgeRetriever
    from app.rag.store import get_knowledge_store

    store = get_knowledge_store()
    if not store.available():
        raise SystemExit("知识库不可用：先确认 data/knowledge_db 存在且嵌入后端能加载")
    corpus = store.all_docs()
    if not corpus:
        raise SystemExit("知识库为空")

    retriever = KnowledgeRetriever(store=store, top_k=TOP_K)
    rng = random.Random(SEED)
    short_map: dict[str, str] = {}   # 短号 → 真实 id
    items: list[dict] = []

    for qi, query in enumerate(QUERIES, 1):
        hits = retriever.invoke(query, top_k=TOP_K)
        chosen = [(d.id or "", d.page_content, d.metadata or {}, "召回") for d in hits]
        got = {c[0] for c in chosen}

        pool = [d for d in corpus if (d.get("id") or "") not in got]
        for d in rng.sample(pool, min(BLIND, len(pool))):
            chosen.append((d.get("id") or "", d.get("page_content") or "",
                           d.get("metadata") or {}, "盲抽"))

        rows = []
        for doc_id, content, meta, origin in chosen:
            sid = _short_id(doc_id, meta)
            short_map[sid] = doc_id
            rows.append({"short": sid, "origin": origin,
                         "source": meta.get("source", ""),
                         "section": meta.get("section", ""),
                         "date": str(meta.get("date", ""))[:10],
                         "ticker": meta.get("ticker", ""),
                         "preview": _preview(content)})
        items.append({"qid": f"Q{qi:02d}", "query": query, "rows": rows})

    lines: list[str] = []
    lines.append("# L2 检索相关性标注表\n")
    lines.append("填法：在每行末尾 `判：` 后面写一个数字，然后保存。全部填完执行")
    lines.append("`D:\\python\\python.exe scripts/make_label_sheet.py --collect`。\n")
    lines.append("## 评分标准（只按「这段文字能不能回答这条查询」判，不要管它排第几）\n")
    lines.append("| 分 | 含义 |")
    lines.append("|---|---|")
    lines.append("| 3 | 直接回答问题（关键数字/结论就在这段里） |")
    lines.append("| 2 | 强相关，需和其他段落组合才能回答 |")
    lines.append("| 1 | 弱相关：同公司/同主题，但这几行答不了 |")
    lines.append("| 0 | 无关 |")
    lines.append("")
    lines.append("两个容易判错的地方：")
    lines.append("1. **别按排序给分**。标`召回`的行顺序就是系统排序，但你要独立判断，")
    lines.append("   否则 nDCG 恒等于 1，指标就废了。")
    lines.append("2. **`盲抽`行是系统没召回、从全库随机抽的**。如果它其实高度相关，")
    lines.append("   照实打 2 或 3 —— 这正是我们要找的漏召回，不是出题错误。\n")
    lines.append("如果某条查询在整个语料里确实找不到任何相关内容（包括盲抽），")
    lines.append("在该查询标题下一行写 `无可相关文档`，这条会用另一套口径统计。\n")
    lines.append("---\n")

    for it in items:
        lines.append(f"## {it['qid']}　{it['query']}\n")
        lines.append("```")
        for r in it["rows"]:
            head = f"{r['short']}  {r['origin']}  {r['source']}  {r['date']}  §{r['section']}"
            lines.append(head)
            lines.append(f"    {r['preview']}")
            lines.append(f"    判：_")
            lines.append("")
        lines.append("```\n")
        lines.append("无可相关文档：<!-- 若确认全无相关，把下面 this 改成 yes --> this=no\n")

    meta = {"queries": QUERIES, "short_map": short_map,
            "items": items, "top_k": TOP_K, "blind": BLIND, "seed": SEED,
            # 语料口径随实际库取值，避免把某一时点的切片数写死进标注文件
            "corpus_count": store.count(),
            "corpus_reports": store.count(where={"kind": "研报"})}
    return "\n".join(lines), meta


COLLECT_HINT = """
收表规则（--collect 会逐条校验）：
  - 每行「判：」后必须是 0/1/2/3（`判：_2` 与 `判：2` 都认）
  - 每条查询至少要有一行分值，或明确写 `无可相关文档: this=yes`
  - 缺一项就报错并指出是哪条查询哪一行，不静默放行
"""


def collect(meta_path: Path) -> int:
    if not meta_path.exists():
        print("找不到标注表底稿，先跑一次 make_label_sheet.py 生成表")
        return 2
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if "corpus_count" not in meta:  # 旧底稿没有这两个字段，按当前库补
        from app.rag.store import get_knowledge_store

        store = get_knowledge_store()
        meta["corpus_count"] = store.count()
        meta["corpus_reports"] = store.count(where={"kind": "研报"})
    sheet = SHEET_PATH.read_text(encoding="utf-8")

    blocks = _split_queries(sheet)
    errors: list[str] = []
    labels: list[dict] = []

    for it in meta["items"]:
        qid = it["qid"]
        block = blocks.get(qid)
        if block is None:
            errors.append(f"{qid} 标注表里找不到这一节")
            continue
        grades = _parse_grades(block)
        no_rel = "无可相关文档: this=yes" in block.replace("`", "")
        if not grades and not no_rel:
            errors.append(f"{qid} 一行都没填，也没写「无可相关文档」")
            continue
        bad = {k: v for k, v in grades.items() if v not in (0, 1, 2, 3)}
        if bad:
            errors.append(f"{qid} 有分值不在 0/1/2/3: {bad}")
            continue
        if no_rel and grades and any(g > 0 for g in grades.values()):
            errors.append(f"{qid} 既写「无可相关文档」又给了正分")
            continue
        expected = {r["short"] for r in it["rows"]}
        missing = expected - set(grades)
        if missing and not no_rel:
            errors.append(f"{qid} 有 {len(missing)} 行没填: {sorted(missing)}")
            continue

        relevance: dict[str, int] = {}
        for r in it["rows"]:
            real_id = meta["short_map"].get(r["short"], r["short"])
            relevance[real_id] = int(grades.get(r["short"], 0))
        labels.append({"qid": qid, "query": it["query"],
                       "no_relevant_docs": no_rel,
                       "relevance": relevance,
                       "blind_short": [r["short"] for r in it["rows"]
                                       if r["origin"] == "盲抽"]})

    if errors:
        print("标注表还不能用：")
        for e in errors:
            print(f"  !! {e}")
        return 1

    out = {
        "version": 1,
        "labels": "human",
        "annotator_note": "由项目作者人工标注；脚本未预填任何分数",
        "corpus": (f"data/knowledge_db（{meta.get('corpus_count', '—')} 切片，"
                   f"其中研报 {meta.get('corpus_reports', '—')} 条）"),
        "top_k": meta["top_k"],
        "queries": len(labels),
        "relevant_total": sum(1 for q in labels for v in q["relevance"].values() if v > 0),
        "no_relevant_queries": sum(1 for q in labels if q["no_relevant_docs"]),
        "items": labels,
    }
    LABELS_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    try:
        shown = LABELS_PATH.relative_to(ROOT)
    except ValueError:
        shown = LABELS_PATH
    print(f"标注已收录: {shown}")
    print(f"  {out['queries']} 条查询，相关文档共 {out['relevant_total']} 篇，"
          f"空结果查询 {out['no_relevant_queries']} 条")
    print("下一步: D:\\python\\python.exe scripts/eval_retrieval.py --labeled")
    return 0


def _split_queries(sheet: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for line in sheet.splitlines():
        if line.startswith("## Q"):
            if cur:
                blocks[cur] = "\n".join(buf)
            cur = line[3:].strip().split("　")[0].split()[0]
            buf = []
        elif cur:
            buf.append(line)
    if cur:
        blocks[cur] = "\n".join(buf)
    return blocks


def _parse_grades(block: str) -> dict[str, int]:
    """从 `短号  召回/盲抽  source  date  §sec` 与下一行 `判：n` 配对出短号→分。

    模板里的占位是 `判：_`，人填的时候很自然写成 `判：_2`（数字接在下划线后），
    所以下划线是可选前缀，不能当成「没填」。取值域在 collect() 里校验。
    """
    grades: dict[str, int] = {}
    short = None
    for line in block.splitlines():
        stripped = line.strip()
        grade = re.match(r"^判\s*[:：]\s*[_＿]?\s*(\d+)\s*$", stripped)
        if grade:
            if short:
                grades[short] = int(grade.group(1))
            short = None
            continue
        parts = stripped.split()
        if len(parts) >= 2 and parts[1] in ("召回", "盲抽"):
            short = parts[0]
    return grades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true", help="收表并生成 l2_labels.json")
    ap.add_argument("--meta", default=str(SHEET_PATH.with_suffix(".meta.json")),
                    help="底稿路径（存真实 id 映射，别删）")
    args = ap.parse_args()

    if args.collect:
        return collect(Path(args.meta))

    print("生成标注表（首次会加载嵌入模型，约 30s）…")
    sheet, meta = build_sheet()
    SHEET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHEET_PATH.write_text(sheet, encoding="utf-8")
    Path(args.meta).write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    n_rows = sum(len(i["rows"]) for i in meta["items"])
    print(f"已写 {SHEET_PATH.relative_to(ROOT)}")
    print(f"  {len(meta['items'])} 条查询 × 每条 {meta['top_k']}+{meta['blind']} 篇"
          f" = {n_rows} 个判断，按每条 8 秒估算约 {n_rows * 8 // 60} 分钟")
    print(f"  真实 id 映射存于 {Path(args.meta).name}（收表时要用，不要删）")
    print(COLLECT_HINT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
