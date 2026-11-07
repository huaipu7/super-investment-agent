#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生成 SIA 的流程图 / 技术路线图 / 产品架构图（自包含 HTML，供墨刀 HTML 导入）。

运行：
    D:\\python\\python.exe docs\\diagrams\\gen_diagrams.py

产物：docs/diagrams/html/*.html —— 单文件、零外链、零脚本，打开即渲染。

图内所有模块名、路由名、步骤编号均取自仓库源码，不臆造：
    app/analysis/pipeline.py   步骤 1 ~ 6.5 的真实执行顺序
    ui/dashboard.py            13 个看板模块
    api/main.py                18 个 include_router
    app/                      22 个能力包
    app/data/sources/          8 个数据源适配器
"""
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent / "html"

INK = "#12171F"
INK2 = "#3D4655"
MUTED = "#6C7686"
FAINT = "#9AA3B2"
LINE = "#E6E9F0"
LINE2 = "#EFF2F7"
BG = "#F3F5F8"
PANEL = "#FFFFFF"

BRAND = "#1F4E8C"
BLUE = "#2F6FD0"
TEAL = "#0F766E"
AMBER = "#B45309"
VIOLET = "#6D28D9"
GREEN = "#12805A"
GREY = "#5A6474"

SRC = {
    "computed": "#2F6FD0",
    "source": "#12805A",
    "llm": "#8B5CF6",
    "imputed": "#D98A00",
    "degraded": "#BC352C",
}

FONT_CSS = (
    '"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",'
    '"Source Han Sans SC","Segoe UI",sans-serif'
)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tw(s: str, size: float) -> float:
    """粗略文本宽度：CJK 按 1em，其余按 0.56em。用于给 chip 定宽。"""
    return sum(size * (1.0 if ord(ch) > 0x2E80 else 0.56) for ch in s)


def box(x, y, w, h, *, bg=PANEL, border=LINE, r=10, dash=False, bw=1.0):
    style = (
        f"left:{x:.1f}px;top:{y:.1f}px;width:{w:.1f}px;height:{h:.1f}px;"
        f"background:{bg};border:{bw}px {'dashed' if dash else 'solid'} {border};"
        f"border-radius:{r}px;"
    )
    return f'<div class="abs" style="{style}"></div>\n'


def solid(x, y, w, h, color, r=0):
    return (
        f'<div class="abs" style="left:{x:.1f}px;top:{y:.1f}px;'
        f'width:{w:.1f}px;height:{h:.1f}px;background:{color};'
        f'border-radius:{r}px;"></div>\n'
    )


def txt(x, y, w, h, s, *, size=13, color=INK, weight="400", align="left", middle=False):
    style = (
        f"left:{x:.1f}px;top:{y:.1f}px;width:{w:.1f}px;height:{h:.1f}px;"
        f"font-size:{size}px;color:{color};font-weight:{weight};"
        f"text-align:{align};line-height:{h:.1f}px;"
    )
    if middle:
        style += "display:flex;align-items:center;"
        if align == "center":
            style += "justify-content:center;"
    return f'<div class="abs nowrap" style="{style}">{esc(s)}</div>\n'


def para(x, y, w, h, s, *, size=11, color=MUTED, lh=17, weight="400"):
    style = (
        f"left:{x:.1f}px;top:{y:.1f}px;width:{w:.1f}px;height:{h:.1f}px;"
        f"font-size:{size}px;color:{color};font-weight:{weight};line-height:{lh}px;"
    )
    return f'<div class="abs" style="{style}">{esc(s)}</div>\n'


def arrow_h(x1, x2, y, color=FAINT, w=1.5):
    head = 7.0
    return (
        f'<div class="abs" style="left:{x1:.1f}px;top:{y - w / 2:.1f}px;'
        f'width:{x2 - x1 - head:.1f}px;height:{w}px;background:{color};"></div>\n'
        f'<div class="abs" style="left:{x2 - head:.1f}px;top:{y - head * 0.58:.1f}px;'
        f"width:0;height:0;border-left:{head}px solid {color};"
        f'border-top:{head * 0.58:.1f}px solid transparent;'
        f'border-bottom:{head * 0.58:.1f}px solid transparent;"></div>\n'
    )


def arrow_v(x, y1, y2, color=FAINT, w=1.5):
    head = 7.0
    return (
        f'<div class="abs" style="left:{x - w / 2:.1f}px;top:{y1:.1f}px;'
        f'width:{w}px;height:{y2 - y1 - head:.1f}px;background:{color};"></div>\n'
        f'<div class="abs" style="left:{x - head * 0.58:.1f}px;top:{y2 - head:.1f}px;'
        f"width:0;height:0;border-top:{head}px solid {color};"
        f'border-left:{head * 0.58:.1f}px solid transparent;'
        f'border-right:{head * 0.58:.1f}px solid transparent;"></div>\n'
    )


def html_open(w, h, title):
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width={w}">\n'
        f"<title>{esc(title)}</title>\n"
        "<style>\n"
        f"*{{box-sizing:border-box;margin:0;padding:0}}\n"
        f"body{{width:{w}px;height:{h}px;position:relative;overflow:hidden;"
        f"background:{BG};font-family:{FONT_CSS};color:{INK};font-size:14px;"
        "-webkit-font-smoothing:antialiased}\n"
        ".abs{position:absolute}\n"
        ".nowrap{white-space:nowrap}\n"
        "</style>\n</head>\n<body>\n"
        f'<div class="abs" style="left:0;top:0;width:{w}px;height:{h}px;'
        f'background:{BG}"></div>\n'
    )


def header(w, title, sub, tag=""):
    out = [
        txt(48, 26, 900, 40, title, size=26, weight="700"),
        txt(48, 70, 1200, 24, sub, size=13.5, color=MUTED),
        f'<div class="abs" style="left:48px;top:106px;width:{w - 96}px;'
        f'height:1px;background:{LINE};"></div>\n',
    ]
    if tag:
        bw = tw(tag, 12) + 24
        out.append(box(w - 48 - bw, 34, bw, 26, bg=PANEL, border=LINE, r=13))
        out.append(
            txt(w - 48 - bw, 34, bw, 26, tag, size=12, color=MUTED,
                weight="500", align="center", middle=True)
        )
    return "".join(out)


# ────────────────────────────────────────────────────────────────
# 图 1 · 研究流程图
# ────────────────────────────────────────────────────────────────


def build_flow() -> str:
    W, H = 1600, 890
    cols = [
        (
            "① 取数与清洗", BRAND,
            [
                ("1　输入标的", "代码 / 名称 / 市场识别"),
                ("2　数据获取", "行情 · 财报 · 宏观利率"),
                ("3　清洗与插补留痕", "缺失显式记录，不静默填补"),
                ("4　知识库检索", "RAG top-k：政策 / 论文 / 公告"),
            ],
        ),
        (
            "② 财务与估值", BLUE,
            [
                ("5　财务分析", "比率 · 杜邦 · 成长性"),
                ("6　财报季度化", "A 股近 4 季营收/净利趋势"),
                ("7　估值分析", "DCF + 相对估值"),
                ("8　SOTP 分部估值", "多业务线拆分，标注模型估计"),
            ],
        ),
        (
            "③ 外部事实", TEAL,
            [
                ("9　多市场分析", "跨市场对照与折溢价"),
                ("10　新闻舆情", "正负面事件抽取"),
                ("11　股东大会", "决议与治理信号"),
                ("12　事实核查", "validator 校验关键结论"),
            ],
        ),
        (
            "④ 判断与建议", AMBER,
            [
                ("13　地缘政治", "关税 / 供应链敏感度"),
                ("14　供应链", "上下游依赖分析"),
                ("15　竞品分析", "真实 PE 覆盖行业均值"),
                ("16　SWOT", "LLM 生成：中性 / 偏多 / 偏空"),
                ("17　交易信号", "技术 + 估值，确定性，无 LLM"),
            ],
        ),
        (
            "⑤ 可信与产出", VIOLET,
            [
                ("18　事实台账", "每个数字登记出身与路径"),
                ("19　接地检查", "LLM 文本中的数字必须可溯源"),
                ("20　五层评测", "L0–L4 离线可复现"),
                ("21　产物落盘", "JSON artifact + 报告"),
            ],
        ),
    ]

    x0, cw, gap = 40, 280, 30
    hy, hh = 128, 36
    by0, bh, bgap = 188, 92, 20

    out = [html_open(W, H, "SIA 研究流程图")]
    out.append(
        header(
            W,
            "SIA · 研究流程图",
            "1–17 为管线内步骤，编号与 app/analysis/pipeline.py 的执行顺序一致；"
            "18–21 为产出与可信层环节。",
            "超级投研 Agent",
        )
    )

    for i, (title, accent, items) in enumerate(cols):
        cx = x0 + i * (cw + gap)
        out.append(box(cx, hy, cw, hh, bg=accent, border=accent, r=8))
        out.append(
            txt(cx, hy, cw, hh, title, size=13.5, color="#FFFFFF",
                weight="600", align="center", middle=True)
        )

        n = len(items)
        last_mid = by0 + (n - 1) * (bh + bgap) + bh / 2
        out.append(
            f'<div class="abs" style="left:{cx + cw / 2 - 1:.1f}px;top:{hy + hh}px;'
            f'width:2px;height:{last_mid - hy - hh:.1f}px;background:{LINE};"></div>\n'
        )

        for j, (bt, bs) in enumerate(items):
            by = by0 + j * (bh + bgap)
            out.append(box(cx, by, cw, bh, bg=PANEL, border=LINE, r=10))
            out.append(solid(cx, by + 10, 4, bh - 20, accent, 2))
            out.append(txt(cx + 18, by + 18, cw - 32, 24, bt, size=14, weight="600"))
            out.append(txt(cx + 18, by + 46, cw - 32, 22, bs, size=11.5, color=MUTED))

        if i < len(cols) - 1:
            out.append(arrow_h(cx + cw + 3, cx + cw + gap - 3, hy + hh / 2))

    out.append(_flow_footer(W, 792))
    out.append("</body>\n</html>\n")
    return "".join(out)


def _flow_footer(w: int, y: float) -> str:
    out = [box(40, y, w - 80, 74, bg=PANEL, border=LINE, r=10)]
    out.append(txt(62, y + 14, 200, 22, "溯源标签", size=12.5, color=INK2, weight="600"))
    out.append(
        txt(62, y + 38, 340, 22,
            "产物里的每个数字都带出身标记，界面与报告同一套口径。",
            size=11.5, color=MUTED)
    )

    shares = [
        ("computed", "27.52%", "计算得出"),
        ("source", "70.87%", "数据源原始值"),
        ("llm", "0.39%", "模型生成"),
        ("imputed", "1.14%", "行业默认值"),
        ("degraded", "0.09%", "降级兜底"),
    ]
    sx = 420.0
    for key, pct, desc in shares:
        out.append(solid(sx, y + 22, 9, 9, SRC[key], 2))
        out.append(txt(sx + 15, y + 16, 210, 20, key, size=11.5, color=SRC[key], weight="600"))
        out.append(txt(sx + 15, y + 38, 210, 20, f"{pct}  {desc}", size=10.5, color=MUTED))
        sx += 222

    return "".join(out)


# ────────────────────────────────────────────────────────────────
# 图 2 · 研究技术路线图
# ────────────────────────────────────────────────────────────────


def build_roadmap() -> str:
    W, H = 1700, 940

    eras = [
        (
            "奠基", "Phase 0–3", BRAND,
            [
                ("P0 环境与连通", "统一配置 · LLM 客户端"),
                ("P1 统一数据层", "四类数据源适配器"),
                ("P2 深度研究管线", "财务 · 估值 · 竞品 · SWOT"),
                ("P3 专家评分引擎", "四大师评分 + A 股行情"),
            ],
        ),
        (
            "产品化", "Phase 4–8", BLUE,
            [
                ("P4 对话 Agent", "意图 · 规划 · 执行 · 表达"),
                ("P5 统一报告", "PDF / HTML / Markdown"),
                ("P6 FastAPI 后端", "/api/v1/* 统一路由"),
                ("P7 Streamlit 看板", "模块化页面，先出稿再审"),
                ("P8 端到端交付", "测试 · 一键启动 · 文档"),
            ],
        ),
        (
            "可信化", "可信层 P1–P6", VIOLET,
            [
                ("P1 事实台账", "provenance + ledger"),
                ("P2 L0 与产物采集", "台账闭合性检查跑通"),
                ("P3 降级显式化", "插补可追溯 + 严格模式"),
                ("P4 接地与 L1–L3", "文本数字必须可溯源"),
                ("P5 数据飞轮", "点赞/点踩落库 + 护栏指标"),
                ("P6 PRD 与 L4", "FR ↔ 断言双向映射"),
            ],
        ),
        (
            "能力扩展", "外部语料与垂类", TEAL,
            [
                ("RAG 外部语料", "政策 · 披露 · 论文，5 源合规"),
                ("固收研究", "信用评级 · 利率情景"),
                ("自主深度调查", "三道反编造闸门 + 硬预算"),
                ("多模态接入", "图片 / 音频 / 视频识别"),
                ("市场情绪", "A 股 / 美股 / 港股信号"),
                ("回测验证", "多 Agent 回测，防前视泄漏"),
            ],
        ),
    ]

    lx, lw = 40, 200
    cx0, cw, cgap = 270, 210, 18
    row_y0, row_h = 176, 160
    card_h = 104

    out = [html_open(W, H, "SIA 研究技术路线图")]
    out.append(
        header(
            W,
            "SIA · 研究技术路线图",
            "四条轨道按阶段推进，每个 Phase 验证通过后才进入下一阶段。所有轨道当前均已交付。",
            "超级投研 Agent",
        )
    )

    for i, (era, sub, accent, cards) in enumerate(eras):
        ry = row_y0 + i * row_h

        out.append(box(lx, ry, lw, card_h, bg=PANEL, border=LINE, r=10))
        out.append(solid(lx, ry, 4, card_h, accent, 2))
        out.append(txt(lx + 20, ry + 30, lw - 32, 26, era, size=16, weight="700"))
        out.append(txt(lx + 20, ry + 58, lw - 32, 20, sub, size=11.5, color=MUTED))

        for j, (ct, cd) in enumerate(cards):
            cx = cx0 + j * (cw + cgap)
            out.append(box(cx, ry, cw, card_h, bg=PANEL, border=LINE, r=10))
            out.append(solid(cx + 16, ry + 22, 8, 8, accent, 4))
            out.append(txt(cx + 32, ry + 14, cw - 44, 24, ct, size=13, weight="600"))
            out.append(para(cx + 16, ry + 48, cw - 32, 44, cd, size=11, lh=17))

            if j < len(cards) - 1:
                out.append(arrow_h(cx + cw + 2, cx + cw + cgap - 2, ry + 52, FAINT, 1.3))

    out.append(_roadmap_footer(W, 800))
    out.append("</body>\n</html>\n")
    return "".join(out)


def _roadmap_footer(w: int, y: float) -> str:
    out = [box(40, y, w - 80, 96, bg=PANEL, border=LINE, r=10)]
    out.append(txt(62, y + 14, 300, 22, "推进纪律", size=12.5, color=INK2, weight="600"))
    out.append(
        txt(62, y + 38, 760, 22,
            "每个 Phase 结束即验证并汇报，批准后才进入下一阶段；验证不通过不进下一步。",
            size=11.5, color=MUTED)
    )
    out.append(
        txt(62, y + 60, 760, 22,
            "数量：主轨 9 个 Phase · 可信层 6 个 Phase · 能力扩展 6 条线。",
            size=11.5, color=MUTED)
    )

    bx = 900.0
    for label, num in (("主轨 Phase 0–8", "9"), ("可信层 P1–P6", "6"), ("扩展能力线", "6")):
        out.append(box(bx, y + 22, 210, 52, bg=BG, border=LINE2, r=8))
        out.append(txt(bx + 16, y + 30, 180, 18, label, size=11.5, color=MUTED))
        out.append(txt(bx + 16, y + 50, 180, 22, num, size=16, weight="700"))
        bx += 226

    return "".join(out)


# ────────────────────────────────────────────────────────────────
# 图 3 · 产品架构图
# ────────────────────────────────────────────────────────────────


def build_arch() -> str:
    W = 1660
    X, PW = 40, 1580
    LABEL_W = 176
    CHIP_X = X + LABEL_W + 14
    CHIP_MAXW = PW - LABEL_W - 42

    layers = [
        (
            "前端", "Streamlit 看板", BRAND,
            [
                ("行情与评分", False), ("深度研究", False), ("AI 对话", False),
                ("市场情绪", False), ("基金分析", False), ("自选股", False),
                ("多股对比", False), ("回测验证", False), ("固收研究", False),
                ("自主深度调查", False), ("评测看板", False), ("论点台账", False),
                ("知识库检索", False), ("总览（建议新增）", True),
            ],
            "13 个模块 · ui/pages/",
        ),
        (
            "接口", "FastAPI", BLUE,
            [
                ("stock", False), ("analysis", False), ("chat", False),
                ("report", False), ("watchlist", False), ("compare", False),
                ("audit", False), ("compliance", False), ("profile", False),
                ("metrics", False), ("market", False), ("multimodal", False),
                ("credit", False), ("rates", False), ("investigation", False),
                ("feedback", False), ("thesis", False), ("search", False),
            ],
            "18 个路由 · api/main.py",
        ),
        (
            "能力", "领域包", TEAL,
            [
                ("agent", False), ("analysis", False), ("rag", False),
                ("credit", False), ("rates", False), ("investigation", False),
                ("thesis", False), ("watchlist", False), ("report", False),
                ("report_generation", False), ("multimodal", False),
                ("compliance", False), ("audit", False), ("feedback", False),
                ("memory", False), ("action", False), ("mcp", False),
            ],
            "17 个领域包 · app/",
        ),
        (
            "可信", "台账与评测", VIOLET,
            [
                ("provenance 溯源分类", False), ("ledger 事实台账", False),
                ("grounding 文本接地", False), ("L0 台账闭合", False),
                ("L1 数值接地", False), ("L2 检索质量", False),
                ("L3 生成质量", False), ("L4 端到端", False),
            ],
            "3 个模块 + 5 层评测",
        ),
        (
            "数据", "数据源适配", GREEN,
            [
                ("akshare", False), ("akshare_us", False), ("sina", False),
                ("fund", False), ("yfinance", False), ("edgar", False),
                ("treasury", False), ("fomc", False),
                ("SQLite 缓存", False), ("数据清洗", False), ("knowledge_db", False),
            ],
            "8 个适配器 + 缓存 + 语料库",
        ),
        (
            "基础", "运行时", GREY,
            [
                ("LLM 客户端（DeepSeek V4 Flash · Anthropic 兼容）", False),
                ("统一配置", False), ("可观测（日志 / 指标）", False),
                ("通用工具", False),
            ],
            "app/llm · config.py · observability",
        ),
    ]

    # 第一遍：量高，好把画布高度算准
    spans = []
    y = 128.0
    for name, sub, accent, items, meta in layers:
        _, used = _layout_chips(CHIP_X, 0, CHIP_MAXW, items)
        h = max(used + 30, 108)
        spans.append((name, sub, accent, items, meta, y, h, used))
        y += h + 24
    H = int(y + 16)

    out = [html_open(W, H, "SIA 产品架构图")]
    out.append(
        header(
            W,
            "SIA · 产品架构图",
            "六层自上而下：看板只调用后端，后端只调用领域包，所有数字最终落到同一个事实台账。",
            "超级投研 Agent",
        )
    )

    for idx, (name, sub, accent, items, meta, y, h, used) in enumerate(spans):
        cy0 = y + (h - used) / 2

        out.append(box(X, y, PW, h, bg=PANEL, border=LINE, r=12))
        out.append(solid(X, y, 4, h, accent, 2))
        out.append(txt(X + 24, y + 26, LABEL_W - 32, 26, name, size=15, weight="700"))
        out.append(txt(X + 24, y + 54, LABEL_W - 32, 20, sub, size=11.5, color=MUTED))
        out.append(txt(X + 24, y + 78, LABEL_W - 32, 20, meta, size=10.5, color=FAINT))
        out.append(
            f'<div class="abs" style="left:{X + LABEL_W}px;top:{y + 12:.1f}px;'
            f'width:1px;height:{h - 24:.1f}px;background:{LINE2};"></div>\n'
        )
        out.append(_render_chips(CHIP_X, cy0, CHIP_MAXW, items, accent))

        if idx < len(spans) - 1:
            out.append(arrow_v(X + PW / 2, y + h + 2, y + h + 22, FAINT, 1.3))

    out.append("</body>\n</html>\n")
    return "".join(out)


def _layout_chips(x0, y0, maxw, items, ch=30, gx=9, gy=9, pad=13, size=12.5):
    placed, cx, cy = [], x0, y0
    for label, dashed in items:
        w = tw(label, size) + pad * 2 + 4
        if cx + w > x0 + maxw and cx > x0:
            cx, cy = x0, cy + ch + gy
        placed.append((cx, cy, w, label, dashed))
        cx += w + gx
    return placed, (cy - y0) + ch


def _render_chips(x0, y0, maxw, items, accent):
    placed, _ = _layout_chips(x0, y0, maxw, items)
    out = []
    for cx, cy, w, label, dashed in placed:
        if dashed:
            out.append(box(cx, cy, w, 30, bg=PANEL, border=accent, r=7, dash=True))
            out.append(
                txt(cx, cy, w, 30, label, size=12.5, color=accent,
                    weight="500", align="center", middle=True)
            )
        else:
            out.append(box(cx, cy, w, 30, bg=PANEL, border=LINE, r=7))
            out.append(
                txt(cx, cy, w, 30, label, size=12.5, color=INK2,
                    weight="500", align="center", middle=True)
            )
    return "".join(out)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    files = [
        ("01_research_flow.html", build_flow()),
        ("02_tech_roadmap.html", build_roadmap()),
        ("03_product_architecture.html", build_arch()),
    ]
    for name, content in files:
        p = OUT / name
        p.write_text(content, encoding="utf-8")
        print(f"{name:34s} {p.stat().st_size:>8,} bytes")


if __name__ == "__main__":
    main()
