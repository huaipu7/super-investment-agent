"""生成《Super-Investment-Agent 项目改进史》Word 文档。

一次性文档生成器：把计划书、项目记忆、评测历史里的事实按时间轴整理成 docx。
所有数字均抄自上述材料，未做估算；材料缺失处写明「未标注」。

运行: D:\\python\\python.exe scripts/build_project_history_doc.py
输出: docs/SIA项目改进史_20260810-20261107.docx
"""
from __future__ import annotations

import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "SIA项目改进史_20260810-20261107.docx"

BODY_FONT = "宋体"
HEAD_FONT = "微软雅黑"
ACCENT = RGBColor(0x1E, 0x3A, 0x5F)
MUTED = RGBColor(0x55, 0x5F, 0x6B)


# ── 基础排版 ──

def _set_font(run, *, name: str = BODY_FONT, size: float | None = None,
              bold: bool = False, color: RGBColor | None = None) -> None:
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), name)
    if size is not None:
        run.font.size = Pt(size)
    run.bold = bold
    if color is not None:
        run.font.color.rgb = color


def _init_styles(doc: Document) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.4

    for style_name, size in (("Heading 1", 16), ("Heading 2", 13), ("Heading 3", 11.5)):
        st = doc.styles[style_name]
        st.font.name = HEAD_FONT
        st.font.size = Pt(size)
        st.font.bold = True
        st.font.color.rgb = ACCENT
        st.element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_FONT)
        st.paragraph_format.space_before = Pt(14 if size >= 13 else 10)
        st.paragraph_format.space_after = Pt(6)

    for section in doc.sections:
        section.top_margin = Cm(2.4)
        section.bottom_margin = Cm(2.2)
        section.left_margin = Cm(2.6)
        section.right_margin = Cm(2.6)


def _add_page_numbers(doc: Document) -> None:
    for section in doc.sections:
        par = section.footer.paragraphs[0]
        par.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = par.add_run()
        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = "PAGE"
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        run._r.append(begin)
        run._r.append(instr)
        run._r.append(end)
        _set_font(run, size=9, color=MUTED)


def h(doc, text: str, level: int = 1) -> None:
    doc.add_heading(text, level=level)


def p(doc, text: str, *, lead: str | None = None, indent: bool = False,
      size: float | None = None, color: RGBColor | None = None):
    par = doc.add_paragraph()
    if indent:
        par.paragraph_format.left_indent = Cm(0.6)
    if lead:
        _set_font(par.add_run(lead), bold=True)
    _set_font(par.add_run(text), size=size, color=color)
    return par


def bullet(doc, text: str, *, lead: str | None = None) -> None:
    par = doc.add_paragraph(style="List Bullet")
    par.paragraph_format.space_after = Pt(3)
    if lead:
        _set_font(par.add_run(lead), bold=True)
    _set_font(par.add_run(text))


def quote(doc, text: str) -> None:
    par = doc.add_paragraph()
    par.paragraph_format.left_indent = Cm(0.5)
    par.paragraph_format.space_before = Pt(4)
    par.paragraph_format.space_after = Pt(8)
    _set_font(par.add_run(text), size=9.5, color=MUTED)


def _shade(cell, hex_color: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_color)
    tcpr.append(shd)


def table(doc, header: list[str], rows: list[list[str]],
          widths: list[float] | None = None, *, font_size: float = 9) -> None:
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, text in enumerate(header):
        cell = t.rows[0].cells[i]
        cell.text = ""
        run = cell.paragraphs[0].add_run(text)
        _set_font(run, name=HEAD_FONT, size=font_size, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF))
        _shade(cell, "1E3A5F")
    for row in rows:
        cells = t.add_row().cells
        for i, text in enumerate(row):
            cells[i].text = ""
            _set_font(cells[i].paragraphs[0].add_run(text), size=font_size)
    if widths:
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Cm(w)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


# ── 文档内容 ──

def build() -> Path:
    doc = Document()
    _init_styles(doc)
    _add_page_numbers(doc)

    # 封面
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_before = Pt(90)
    _set_font(title.add_run("Super-Investment-Agent（SIA）"), name=HEAD_FONT,
              size=24, bold=True, color=ACCENT)
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_font(sub.add_run("项目改进史"), name=HEAD_FONT, size=24, bold=True, color=ACCENT)

    line = doc.add_paragraph()
    line.alignment = WD_ALIGN_PARAGRAPH.CENTER
    line.paragraph_format.space_before = Pt(18)
    _set_font(line.add_run("2026-08-10  —  2026-11-07"), name=HEAD_FONT, size=13, color=MUTED)

    desc = doc.add_paragraph()
    desc.alignment = WD_ALIGN_PARAGRAPH.CENTER
    desc.paragraph_format.space_before = Pt(10)
    _set_font(desc.add_run("以时间为轴，逐项记录：为什么改 · 怎么改 · 改出了什么"),
              size=11, color=MUTED)

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta.paragraph_format.space_before = Pt(120)
    _set_font(meta.add_run("生成日期：2026-11-07　｜　数据来源：9 份计划书 · 10 份项目记忆 · 192 条评测记录 · 15 份产物"),
              size=9, color=MUTED)

    doc.add_page_break()

    # 目录
    h(doc, "目录", 1)
    for item in [
        "一、这份文档怎么读", "二、项目是什么：一页概览", "三、演进总览：十个阶段",
        "四、分阶段详述（阶段一 ~ 阶段十）", "五、四条主线：因果归纳",
        "六、量化成果汇总", "七、踩过的坑与教训", "八、遗留问题（诚实清单）",
        "九、附录：材料来源与工作约束",
    ]:
        par = doc.add_paragraph()
        par.paragraph_format.space_after = Pt(2)
        _set_font(par.add_run(item), size=10.5)

    doc.add_page_break()

    # 一
    h(doc, "一、这份文档怎么读", 1)
    p(doc, "这份文档回答三个问题：这个项目在什么背景下做了什么改进、每一项改进是被什么具体问题逼出来的、"
           "改完之后拿到了什么可验证的结果。它不写口号，只写能对上号的事实。")
    bullet(doc, "9 份计划书（项目根目录 3 份 + plan/ 6 份）、10 份项目记忆文件、"
                "data/eval/history.jsonl 的 192 条评测记录、data/artifacts/ 下的 15 份管线产物。",
           lead="材料来源：")
    bullet(doc, "计划书与记忆中的日期为本地时间（UTC+8）；history.jsonl 记的是 UTC，引用时已换算。"
                "产物文件名用本地时间。", lead="日期口径：")
    bullet(doc, "一律抄录材料里的实测值，不估算、不取整凑整。材料没写的写「未标注」。"
                "凡是「计划预测值」与「实测值」不一致的，两者都列出来。", lead="数字口径：")
    p(doc, "价格交易日时间戳那份计划书预测 as_of_coverage 会从「204/2713 → 约 199/2713」（≈92.5%），"
           "实测基线为 0.9251、改后为 0.9306。方向的判断（提升）是对的，具体数值的预测偏保守。"
           "这份文档一律采用实测值。", lead="已知不一致：")
    p(doc, "项目仓库自 2026-08-18 起只有一个初始提交（d0fc617），此后所有改动都在工作区、未提交。"
           "因此这份历史不是从 git log 复原的，而是从计划书、记忆与评测记录三条独立线索交叉核对出来的："
           "计划书给出动机与方案，记忆给出实施细节与教训，history.jsonl 给出每轮评测的时间戳与硬指标。"
           "三者对不上的地方，本文以硬指标为准并注明。")

    # 二
    h(doc, "二、项目是什么：一页概览", 1)
    p(doc, "SIA 是一个单用户的投资研究 Agent：输入一个标的代码，输出一份带数字、带估值、带交易信号的研究报告，"
           "并且——这是它后来所有改进的核心——报告的每一个数字都能被追溯、被质疑、被复核。")
    p(doc, "它由三个原本独立运行的项目在 2026-08-10 整合而成：", lead="出身：")
    bullet(doc, "负责深度研究管线（FastAPI + LangChain + yfinance/akshare），提供财务分析与估值。")
    bullet(doc, "即 Violetz，负责对话层（FastAPI + SQLite + SSE + DeepSeek）。")
    bullet(doc, "即 Violet Z，负责行情与「四大师」评分（Flask + akshare/Sina）。")
    bullet(doc, "因依赖 Neo4j 被明确排除（用户在计划书阶段即决策不引入图数据库）。")
    p(doc, "DeepSeek V4 Flash（走 Anthropic 兼容端点）、FastAPI 后端 + Streamlit 看板、"
           "SQLite、Chroma 向量库、reportlab 报告。当时被逐条否决的技术选型包括：Redis 与 Neo4j（只留 SQLite）、"
           "Java（瓶颈在网络 I/O 与 LLM 延迟，计算仅毫秒级，跨语言桥接成本大于收益）、"
           "整套 LangChain（按需剥离为 sentence-transformers + faiss-cpu）。", lead="技术栈：")
    p(doc, "五层评测体系，全部离线跑在已落盘的产物上，不现跑管线、不烧 token：L0 台账闭合性、"
           "L1 数值接地（散文里的数字能否在台账里找到出处）、L2 检索质量（nDCG/Recall/MRR）、"
           "L3 生成规则、L4 端到端（真实跑三支标的）。", lead="评测骨架：")
    p(doc, "55 个 verify_*.py 守卫脚本、11 个 smoke_*.py、8 个 eval_*.py；15 份产物、33,352 条事实、"
           "unknown 计数为 0；知识库 22,943 个切片。", lead="当前规模：")

    # 三
    h(doc, "三、演进总览：十个阶段", 1)
    p(doc, "十个阶段不是事先规划好的，每一步都是被上一步暴露出来的问题推着走的。这张表是全貌，"
           "之后的第四节逐段展开因果。")
    table(doc,
          ["时间", "阶段", "触发原因（为什么改）", "关键结果"],
          [
              ["08-10 ~ 08-11", "一 · 三项目整合", "三套系统各自为战，研究与对话割裂",
               "Phase 0–8 完成，9 个测试脚本 EXIT=0"],
              ["08-13", "二 · 回测接入", "结论无法回看；评分恒为 HOLD",
               "回测页上线；修恒 HOLD，BUY/SELL/HOLD=4/8/4"],
              ["08-14", "三 · 企业级增强", "无审计、无配置分层、无数据时点标记",
               "意图 1.0 / 接地 1.0 / 搜索 nDCG 0.988"],
              ["08-16", "四 · 多模态与情绪", "无法读图读音频；情绪维度缺基准",
               "5 步全绿；修 router.py 的 DataFrame 崩溃"],
              ["09-01", "五 · 固收扩展", "对标固收研究岗职责（能力缺口）",
               "信用/利率/固收平台；5 组守卫全绿"],
              ["09-21", "六 · 深度调查与美股源", "只描述不追问；Yahoo 403 全面封锁",
               "三道反编造闸门；东财 F10 98 断言全绿"],
              ["11-04", "七 · 可信层与五层评测", "59 个脚本只有 3 个有指标；三处降级伪装",
               "L0–L4 全建，as_of 覆盖 0.29→0.92"],
              ["11-05", "八 · 指标变严", "指标看着高但判别力弱、不可复现",
               "L4 判据 v1→v2；L2 定序；产物质量三修"],
              ["11-06", "九 · 语料·根因·门禁", "L1 接地率下跌的真因未查明；门禁误报",
               "22,943 切片；提示词利率 4.84%；基线重录"],
              ["11-07", "十 · 价格时间戳", "现价无日期；刷新产物混血",
               "pricing 块上线；as_of 覆盖 0.9251→0.9306"],
          ],
          widths=[2.4, 3.0, 5.4, 5.6])

    # 四
    h(doc, "四、分阶段详述（阶段一 ~ 阶段十）", 1)

    # 阶段一
    h(doc, "阶段一 · 2026-08-10 ~ 08-11：把三个项目合成一个", 2)
    p(doc, "三套系统分别在跑：一个能做深度研究但不对话，一个能对话但不出研究，一个只看行情。"
           "用户要的是「输入代码 → 出一份能拿去用的报告」，这需要把三者的能力接起来，而不是再写第四个。", lead="为什么：")
    p(doc, "先写计划书（08-10，状态「待审核」），定 8 个 Phase，每个 Phase 验收通过才进下一个；"
           "并在计划书里立下一条后来贯穿整个项目的红线——缺失数据必须显式标 None，不许让 LLM 编数。", lead="怎么改：")
    p(doc, "Phase 8 完成，9 个测试脚本全部 EXIT=0：smoke_phase1 15/15、smoke_phase2 16/18（2 项软校验）、"
           "test_e2e 21/21。实跑茅台：现价 1348.41、PE 19.79、ROE 33.65%、DCF 2332.52。", lead="结果：")
    p(doc, "当日还修掉两处：Sina 源把空价（<=0）当成真实价格缓存起来；端到端脚本的中文键与 UTF-8 兜底。"
           "这两处都属同一类问题——**坏数据被当成好数据**——是整个项目此后反复处理的主题。", lead="顺带修：")

    # 阶段二
    h(doc, "阶段二 · 2026-08-13：回测接入，与第一个「总是 HOLD」的 bug", 2)
    p(doc, "报告是静态的，做完就没法回看「当初这么判，后来对不对」。于是把 backtest_workflow 以库的形式接进来，"
           "看板加第 7 页「🔬 回测验证」，支持三种模式。", lead="为什么：")
    p(doc, "接入过程中发现评分永远是 HOLD。查根因：四个维度里有三个恒为 50 分，情绪权重只占 7%，"
           "总分 = 46.25 + 0.07 × 情绪分，永远落在 (40, 70) 区间——无论输入什么，结论都被钉死在中性。"
           "这不是模型判断，是权重设计的算术必然。", lead="问题：")
    p(doc, "新增纯价格计算的 _tech_score() 提供真实技术面信号；权重改为 w_tech = 1 − 可用维度占比 / 总数，"
           "缺失维度越多，技术分权重越大。", lead="怎么改：")
    p(doc, "滚动 600519 回测，结论分布变为 BUY 4 / SELL 8 / HOLD 4——评分第一次具有区分度。"
           "同日首次记录 Yahoo YFRateLimitError，当时只当噪声记下，一个多月后它成了美股通道全面失效的伏笔。", lead="结果：")

    # 阶段三
    h(doc, "阶段三 · 2026-08-14：企业级增强 A–D", 2)
    p(doc, "能出报告不等于能长期用：谁在什么时候问了什么、这次用了哪个模型哪个提示词版本、"
           "一条结论对应的是哪天的数据——这些当时都没有记录。", lead="为什么：")
    table(doc, ["批次", "内容", "结果"],
          [["A", "审计日志、配置分层、合规规则检查", "接入 compliance/rules"],
           ["B", "as_of 数据时点标记（vintage）", "区分「真实的点对点」与「当前值被标了个日期」"],
           ["C", "长时记忆 + RAG + MCP + deep_compare", "对话可跨会话记忆"],
           ["D", "LLM 路由、指标、评测集", "意图 1.0 · 接地 1.0 · 检索 nDCG 0.988"]],
          widths=[1.2, 7.2, 8.0])
    p(doc, "研究页新增「📊 数据可视化」6 个页签，离线守卫 verify_viz 18/18。", lead="附带：")
    p(doc, "批次 B 的 as_of vintage 是后来一切「可追溯」工作的种子：它第一次把「这个数字是哪天的」"
           "变成一个显式的字段，而不是读者自己去猜。三个月后的 pricing 块，本质上是把这条线补完。", lead="长期意义：")

    # 阶段四
    h(doc, "阶段四 · 2026-08-16：多模态与市场情绪", 2)
    p(doc, "只吃文本等于放弃一大半现实信息：研报截图、电话会议录音、行情情绪指标都吃不进来。", lead="为什么：")
    p(doc, "先做环境实测，结果否决了两条主流路线：Docker 方案（docker ps 失败，Desktop 引擎未启动；"
           "且 qwen2.5-vl:7b 约 5GB、paddleocr 需 paddlepaddle 约 200MB，磁盘不足）；"
           "DeepSeek API 直接拒绝 image 内容块。VIX 三个源实测均不可用（yfinance ^VIX 限流、东财无映射、Sina 不支持）。", lead="先撞墙：")
    p(doc, "所有模态在本地预处理成文本再喂模型：OCR 用已装的 easyocr，音频用 faster-whisper（模型约 500MB），"
           "视频抽帧 + 抽音轨。情绪基准改用 CBOE VIX。信号缺失一律写 None，不补造。"
           "明确不做：Docker 化、长视频流式、实时推送。", lead="怎么改：")
    p(doc, "五步全部完成；市场情绪覆盖 A 股 6 维、美股 3 维、港股 4 维；看板扩到 8 个模块。"
           "回归中发现并修复 app/data/router.py 的一处真 bug——用 `or` 给 DataFrame 做兜底，"
           "pandas 的 __bool__ 语义会直接抛错。", lead="结果：")
    p(doc, "视觉语义（本地 Qwen2.5-VL）暂缓，Dockerfile 未做。", lead="遗留：")

    # 阶段五
    h(doc, "阶段五 · 2026-09-01：固收研究扩展", 2)
    p(doc, "这一阶段不是由缺陷触发，而是能力扩展：对标固收研究员岗位说明的三条职责"
           "（信用评级、利率情景、固收研究平台），补齐另一条业务线。", lead="为什么：")
    p(doc, "并入 SIA 做增量扩展，不另起炉灶。信用评级用结构模型（Merton/KMV、Altman Z''、Ohlson O-score）"
           "加市场隐含信息（OAS/CDS）做代理，明确不接商业评级机构的付费数据；数据源优先用零 key 的"
           "（美国财政部、EDGAR、CME、ECB SDW）。防泄漏纪律写死在设计里：训练集 ≤ 截止日 < 测试集，"
           "EDGAR 用 filing date 而非 period end，宏观数据用 vintage 快照。", lead="怎么改：")
    p(doc, "Phase 0 与 1、3–7 交付：信用评级、利率情景、FI Agent、固收看板、确定性动作化、API 与评测。"
           "守卫：verify_data 13/13、verify_rates 20/20、verify_credit 14/14、verify_action 15/15、"
           "verify_fi_dashboard 27/27。", lead="结果：")
    p(doc, "三个坑都值得记：① 美国财政部接口返回空曲线，必须**同时**改「URL 按年」与「换浏览器 UA」两处才通，"
           "只改一处仍为空；② Merton 模型恒不可用，是因为传进去的特征帧不带 liabilities，"
           "改回查原始 panel 后 dd=11.0、pd=0.0、sigma_V=0.2606；③ 三张报表按报表本币、行情按挂牌币种，"
           "混用会差约 1400 倍（ADR 的经典陷阱，美股源那轮又踩了一次）。", lead="踩坑：")

    # 阶段六
    h(doc, "阶段六 · 2026-09-21：记忆盘点、自主调查、美股数据源", 2)
    p(doc, "同一天推进三件事，各有各的痛点。", lead="为什么：")

    h(doc, "6.1 五层记忆盘点", 3)
    deprecate = ("把系统的记忆拆成五层逐层盘点，发现第 2 层（会话内摘要）与第 3 层（跨会话写入）"
                 "「只写不读」——数据写进去了，但从来没有被读回提示词，等于没有记忆。"
                 "据此确立判定标准：**持久化 ≠ 记忆，写进去并且读进 prompt 才算**。")
    p(doc, deprecate)

    h(doc, "6.2 自主深度调查：让系统自己去问「为什么」", 3)
    p(doc, "原来的 SIA 拿到财报后只描述事实（「毛利率下降 3 个百分点、存货上升 20%」），"
           "不问为什么，也不去取证。用户要的判定标准是：不止给结论，每一步都要能指回真实来源。", lead="为什么：")
    p(doc, "三道**代码级**闸门，而不是提示词里的软约束：① 没有 material 级信号就不许生成零假设"
           "（阈值表故意不配置化，防运行时放宽）；② 模型返回的假设族必须落在播种集合内；"
           "③ 模型只能「指认」证据——索引必须在全局清单内、source_ref 必须逐字命中，"
           "不通过的记 attribution_valid=False、展示在「未采信」里但计零。"
           "「已验证」状态的判定是纯代码：需 ≥2 条支持且来自 ≥2 种不同来源类型。", lead="怎么改：")
    p(doc, "预算双档硬约束：标准档/深度档分别为 6/12 轮、14/32 次工具、10/20 次 LLM、"
           "300/720 秒、4/8 条假设、4/6 条证据、3/5 家同业；另有 no_progress_window=3、"
           "min_round_seconds=60、查询去重与振荡检测。并坦诚标注 deadline 不是硬停"
           "（单次 LLM 调用上限 120 秒），超时量写进 budget_overshoot_seconds。", lead="防跑飞：")
    p(doc, "否决了「在 chat_with_tools 上加闸」的方案——那个接口无条件执行整轮工具、没有否决点；"
           "改为新增 chat_once_with_tools（约 40 行）。", lead="被否决的方案：")
    p(doc, "验收 159 + 17 条断言。600519 标准档真跑：6 轮 / 11 次工具调用 / 7 次 LLM / 87 秒，"
           "4 条证据全部过闸，状态沿「证据不足 → 未验证 → 部分验证」推进，最终判**未验证**"
           "——因为模型找到的是反驳证据。这不是失败，这是系统按设计工作。", lead="结果：")
    p(doc, "过程中又修两处：归因必须来自「工具调用」而不是模型在文本里编的 JSON"
           "（原设计下 8 次取数换来 0 条证据）；引用编号必须全局而非本轮。", lead="顺带修：")

    h(doc, "6.3 美股数据源：Yahoo 403，改走东财 F10", 3)
    p(doc, "为出 Sea Limited（SE）研报，实测发现美股通道整体不可用：本机代理有效"
           "（gstatic.com/generate_204 返回 204），但 Yahoo 连续 3 次重试均返回 HTTP 403（此前是 429）；"
           "而 yfinance_source.py 早已把系统代理注入网络配置——因此判定是**反爬封锁，不是网络不通，换节点大概率无效**。"
           "具体症状：行情 Quotes/History 正常（1260 行真实数据），但 fetch_company_info 抛 YFRateLimitError "
           "且 pipeline.py 没有 try，直接打断整条管线；三张财报全为 (0,0)。", lead="为什么：")
    p(doc, "新增 akshare_us_source.py，走东方财富美股 F10（国内直连、零 key、免代理）。实测利润表 39 个科目、"
           "资产负债表 51 个、现金流 55 个，覆盖 12 年以上；比率面板 12 年 × 49 列。"
           "映射 26 个标准科目码；非美股代码立即返回空以自然触发 yfinance 兜底；不删任何既有路径。", lead="怎么改：")
    p(doc, "verify_us_source 98 条断言全绿；AnalysisOrchestrator().run('SE') 用时 87.8 秒，"
           "产出 md/html/pdf 与 13 张图。FY2025 营收 $229.4 亿（+36.4%）、毛利率 44.7%、净利率 6.9%、"
           "归母净利 $15.78 亿、自由现金流 $45.1 亿；「资产 = 负债 + 权益」与「毛利 = 收入 − 成本」两项交叉校验差额均为 0。", lead="结果：")
    p(doc, "真实运行又暴露三处：① 跨表口径污染——现金流表里的「存货」实为营运资金变动额、"
           "「净利润」是现金流起点，一律映射成 INVENTORY/NET_INCOME 就会算错，改为分表映射；"
           "② ADR 币种量级错误——GRVY 的 ₩5605 亿被当成 5605 亿美元，差约 1400 倍（修正后 BABA ¥1.02 万亿 → $143.3B）；"
           "③ 摘要与正文自相矛盾（「PE +146.0%」vs「+26.8%」），补 _refresh_relative_valuation_summary 重建。", lead="踩坑：")

    # 阶段七
    h(doc, "阶段七 · 2026-11-04：可信层与五层评测（全项目最大的一次跃迁）", 2)
    p(doc, "这一天之前，项目有一个致命的结构性问题：**它看起来很专业，但没有任何机制能证明它的数字是真的**。"
           "逐文件核查得到六个缺口：", lead="为什么：")
    bullet(doc, "59 个脚本（36 verify / 11 smoke / 4 eval / 1 e2e / 3 run）里，只有 3 个带有真实指标；")
    bullet(doc, "检索评测是自造夹具：3 个查询、4 篇文档，top_k 覆盖全部语料，召回率天然饱和；")
    bullet(doc, "零 JSONL 标注、零反馈采集、零 CI、零回归基线；")
    bullet(doc, "三处「降级伪装」：swot.py 失败时返回 rating=\"中性\"（看起来像正常结论）；"
                "segment.py 伪造一个 100% 的「综合业务」分部并让它进入 SOTP 计算；news.py 失败仍标 available=True。")
    p(doc, "这四条合起来意味着：系统在数据不全时不会说「我不知道」，而是给出一个看起来很正常的答案。", lead="性质：")
    p(doc, "建立事实台账（ledger）：把产物里每个数值叶子落成一条带出处的事实，出处只有五类——"
           "computed / source / llm / imputed / degraded，匹配不上任何一类就是 unknown，"
           "而 unknown 会让 L0 直接判失败。再在其上加第 4 道数值接地闸（L1）：模型写的散文里的每个数字，"
           "必须能在台账里找到出处。评测全部离线跑已落盘的产物。", lead="怎么改：")
    p(doc, "明确否决了四件事：用第二个 LLM 当事实裁判（不可复现、成本翻倍）；引入 CI；"
           "把用户反馈自动塞进回归集；为了跑评测去重构整条管线。", lead="被否决：")
    table(doc, ["层级", "首日建成时的指标", "意义"],
          [["L0 台账", "产物 1→4 份，检查 12/12 → 48/48；as_of 覆盖 0.2941 → 0.9244；单位覆盖 0.3506 → 0.8512",
            "「这条数字哪来的、哪天的」第一次可查"],
           ["L1 接地", "0.9933 → 0.9955（未接地 2 条）", "模型散文里的数字必须能回查"],
           ["L2 检索", "人工标注 25 查询 × 6 篇 = 150 判断；nDCG@4 0.9461 / Recall 0.8813 / MRR 0.96",
            "把「自造夹具」换成真标注"],
           ["L3 生成", "28/28 → 49/49", "六条确定性断言替代 LLM 打分"],
           ["L4 端到端", "3 支标的 4/4；221.2/261.1/85.1 秒；tokens 入 13460 出 16901", "真实跑通才算交付"]],
          widths=[2.2, 8.4, 5.8])
    p(doc, "同日还交付：TRUST_STRICT_MODE（默认开启）+ 七处降级显式化（verify_trust 37/37）、"
           "评测看板 ui/pages/evals.py（verify_eval_dashboard 29/30）、数据飞轮（verify_feedback 61/61）、"
           "PRD 文档与 FR↔断言双向映射（verify_prd 68/68）。", lead="同时：")
    p(doc, "顺带修三处上游：基金源对空字符串缺值崩溃、EDGAR 的 CIK 空映射被缓存 24 小时、"
           "fi_http 里 SEC 的 User-Agent 写反（浏览器 UA 换 403，声明性 UA 换 200）。"
           "最后这条尤其说明问题：**同样的代码，只因为一个 UA 字符串，数据就有还是没有。**", lead="顺带修：")

    # 阶段八
    h(doc, "阶段八 · 2026-11-05：把指标变「有判别力」", 2)
    p(doc, "指标建起来了，但有几个看起来是绿的，其实测不出东西。这一天的主题是「让指标变严」，"
           "而每一次变严都伴随一次残差暴露。", lead="为什么：")

    h(doc, "8.1 L4 判据 v1 → v2：从「结构上不可能达成」到「可解释」", 3)
    p(doc, "v1 要求「降级事实数为 0」。实测发现这不可达——4 条降级全部来自零 key 数据源的覆盖缺口"
           "（A 股没有股东大会数据、AAPL 没有新闻/公告），而系统正确地**不把「查不到」写成 0**。"
           "于是判据改为「降级均可解释」：每条降级事实必须带 evidence_ref 且路径可定位。"
           "变更留痕（criterion_version / criterion_history / rescored_at），实测数据不动，只重判分。", lead="怎么改：")

    h(doc, "8.2 L1 收敛为「数值断言」，并且更严了", 3)
    p(doc, "原本把散文里所有数字都算断言，导致名称里的数字（国窖 1573、茅台 1935、HS 8517）"
           "和年份也被算作「未接地」，指标被噪声污染。新增 is_claim()：带单位、带小数、带千分位、"
           "带货币符号或处在带单位区间内的才算断言；裸整数与年份单列统计——**但仍逐条比对**，"
           "报告里保留 non_claim_samples / non_claim_misses（100 个非断言中 92 个可解释、8 个无出处）。", lead="怎么改：")
    p(doc, "把 bp 补进数字正则与单位表后，多抓出 4 条此前整段漏掉的派生值（约 330bp / 1260bp / "
           "150–250bp / −1460bp），未接地从 4 条升到 8 条，L1 从 29/30 退到 28/30。"
           "**指标变严不是变松，退步反而是诚实的证据。**", lead="直接后果：")

    h(doc, "8.3 派生值复算：先做、发现不对、回退、再做", 3)
    p(doc, "模型常写「34.0% → 19.4%（−1460bp）」这类差值，差值本身不在台账里，于是被判未接地。"
           "第一版方案是**纯按值复算**：只要两个台账事实之差对得上就算接地。接了之后指标立刻变好看"
           "（接地率 0.9981、未接地仅 1 条、派生 7 条）——但它是怎么做到的？把「CR3 98.2%」"
           "凑成流动比率差、把「600 元」凑成 ROE 差、把「150–250bp」凑成毛利率差。"
           "这等于**给编造发通行证**，当天回退。", lead="第一轮（否决）：")
    p(doc, "改为**文本锚定复算**：同一句里必须同时出现指标名与两个年度，且台账里该指标确实存在对应两期事实，"
           "按显示位数对得上才认（match_anchored）；另加 explain_by_sentence_pair 处理同句两个已接地数字之差。"
           "实测 3 条复算全部语义正确：1260bp = 营业利润率 2025−2022、330bp = 毛利率 2024−2025、"
           "1460bp = 34.0−19.4。verify_evals 扩到 50/50，新增 7 条边界断言（无指标名不认、差值不符不认、"
           "指标不同不认、端点未接地不认）。", lead="第二轮（采纳）：")

    h(doc, "8.4 检索不可复现 → 确定性定序", 3)
    p(doc, "同一份快照连跑三次，nDCG 在 0.9409 / 0.9436 / 0.9474 之间抖动。根因是 Chroma 对同分边界的截断"
           "是任意的，以及 TF-IDF 稀疏乘在多线程下 BLAS 累加顺序不定。修法：_rank_key 按（分数, id）定序、"
           "dense 多取候选后确定性截断、稀疏乘用 threadpoolctl 锁单线程。修完稳定在 0.9461。"
           "**指标先要可复现，才谈得上可比较。**", lead="怎么改：")

    h(doc, "8.5 产物质量三修", 3)
    bullet(doc, "源在当日 K 线未成交时整行 NaN，math.floor(nan) 让匹配器崩溃，且 pe_percentile 会算出假 0.0。"
                "修在源头：剔非有限 K 线、取最近有效收盘、样本不足返回 None，并在匹配器加非有限兜底。")
    bullet(doc, "无行业总规模就不出集中度：此前用「标的 + 已知同业」作分母，CR5 恒为 100%；"
                "现在缺 industry_total 时 cr3/cr5/hhi 返回 None，份额改称「已知同业样本内份额」。")
    bullet(doc, "浮点噪声 cr5 = 0.9999999999999999 显示成「99.99」——足以让一个伪造的 99.99% 蒙过反向控制。"
                "第二条从根上消除了这个假接地源。")

    h(doc, "8.6 L1 改为「每标的取最新一份」", 3)
    p(doc, "产物越攒越多，同一标的的旧版本会把新版本的问题稀释掉。改为按标的去重、只评最新一次，"
           "被跳过的历史文件逐份列在报告的 skipped_superseded 里（不静默放过）；verify_evals 新增 4 条断言锁住选择逻辑。", lead="怎么改：")

    h(doc, "8.7 外部语料的四条范围决策", 3)
    p(doc, "知识库里 93.4% 是本项目自己生成的研报，等于用自己的话去验证自己。要引外部权威语料，"
           "但券商研报受著作权保护、公开渠道抓全文违反站点条款。用户拍板四条：① 跳过机构研报；"
           "② 覆盖现有 3 个标的 + 宏观，不扩标的；③ L2 双轨（冻结 v1 快照只读，新语料不入 v1）；"
           "④ 先约 200 篇跑通全链路，不一次上量。", lead="背景与决策：")

    # 阶段九
    h(doc, "阶段九 · 2026-11-06：语料、根因、门禁与产物健壮性", 2)
    p(doc, "这一天是项目里改动最密集的一天，五条线同时在收。", lead="性质：")

    h(doc, "9.1 外部语料正式入库", 3)
    p(doc, "按阶段八的四条决策执行，正式库达到 22,943 个切片（外部政策文件、SEC/HKEX/cninfo 披露文件、"
           "arXiv 论文）。合规边界写死：研报零采集，抓不到 ≠ 抓到空（前者落 not_collected(reason_code)，"
           "后者落 empty_text，两者都进清单，不允许静默跳过）。", lead="结果：")

    h(doc, "9.2 0700.HK 重采：Yahoo 429 之后的兜底", 3)
    p(doc, "Yahoo 对港股仍返回 429，于是加 akshare 港股行情兜底（代码补零、不复权，与 yfinance 的 Close 逐日一致）。"
           "新产物 300 根 K 线、无 NaN、1,574 条事实、unknown 0。", lead="结果：")
    p(doc, "代价是本轮 LLM 文本的数字密度骤降（数字 token 62 → 4、断言 36 → 2），接地率从 0.9444 掉到 0.0，"
           "但**未接地的绝对条数仍是 2 条**——质量并没有实质变差，0.0 是小分母假象。"
           "教训：判断接地要同时看比率与绝对条数。", lead="重要提醒：")

    h(doc, "9.3 L1 残差的真因：提示词里写死了利率", 3)
    p(doc, "0700 那条一直被判未接地的「4.4%」，不是模型幻觉——MASTER_SYSTEM_PROMPT 里写死了"
           "「高利率（4.4%）」「美国基准利率 4.25–4.50%」「4.5% 无风险利率」，模型只是忠实照抄；"
           "而这份提示词在 swot/competitive/geopolitical/segment 四个文件里逐字节相同。", lead="发现：")
    p(doc, "① 合并为 app/analysis/master_prompt.py，利率改为运行时占位符；② 新增 "
           "estimates.get_risk_free_rate()，美股/港股走美国财政部官方每日收益率曲线的 10Y"
           "（实测 4.84%），回落 akshare，再回退常量；③ 低置信时**不把数字喂给模型**，"
           "占位符直接换成「禁止写具体利率数值」；④ 港股按联系汇率用美元利率近似并在 note 写明。", lead="怎么改：")
    p(doc, "机制验证：同一台账下写 4.84% 判接地、写 4.4% 判不接地——**不是靠放宽匹配换来的绿**。"
           "这条教训被单独记下：L1 报未接地时，先查提示词有没有自带这个数，再怀疑模型。", lead="验证：")

    h(doc, "9.4 时间跨度不计入断言", 3)
    p(doc, "0700 最后一条红是「但 6 个月内难兑现为利润」里的 6——它来自地缘提示词给的 6 个月视野，"
           "不是编造的数字。新增 is_duration()：整数后接「内/后/之后/以后/左右」时单列为 time_ref、不进分母；"
           "而「5 年 CAGR」「30 日均线」「6 亿」照旧算断言（双向守卫，防止滥用）。", lead="怎么改：")

    h(doc, "9.5 门禁治理：治三个「报警器自己出问题」", 3)
    bullet(doc, "构成比误报：新增采集后 computed_rate 从 0.3062 掉到 0.2752，被报成退化。"
                "但五个出处占比恒和为 1，新增 1,500+ 条 source 事实必然稀释比例——逐份检查全过、unknown=0。"
                "用户选择「跳过构成比」而非再录一次基线。修法：新增 COMPOSITION_METRICS，用**产物路径集合**"
                "判断集合是否变动，变动时跳过构成比并打印增删份数与跳过清单；new_reds、unknown_count、"
                "层状态、grounding_rate 照旧比。")
    bullet(doc, "_new_reds 的比对键必须分层：L1 按标的目录比（每标的只评最新一份，重采换文件名不算新红），"
                "L0 按文件名比（评全部产物，按目录比会让第二份产物的同类失败被遮住）。")
    bullet(doc, "收尾三修：① l2 档原本不带 --db，跑的是正式库 → nDCG 0.7399「假退化」，改为从 "
                "l2_v1.lock.json 取快照、缺失时告警不静默回落；② markdown.py 在 dcf.upside 为 None 时执行 "
                "None × 100，让整个 L3 层崩溃（表现为 ERROR 0/0，而不是 FAIL）；③ SOTP 的分部声明"
                "嵌在 per_share 判断里，缺现价时连「分部占比为模型估计」的标注一起消失。②③ 在 md/pdf 两个渲染器同步修。")

    h(doc, "9.6 基线重录与门禁语义修正", 3)
    p(doc, "用户批准按当时 11 份产物重录基线（L0 131/132、L1 21/22、L2 1/1、L3 88/88）。"
           "重录同时补三处，让基线不再形同虚设：① 层状态退步告警（PASS→FAIL 即报警，此前只看指标，"
           "基线建在 FAIL 上时再退一步无人报警）；② 基线文件新增 known_reds，自动记录建基线时就不过的检查项，"
           "使「基线本来如此」与「有人把失败录成基线」可区分；③ 退出码只看**变化**而非绝对状态，"
           "已知红不再把 --baseline 永久钉在 1。", lead="决策与做法：")

    h(doc, "9.7 产物健壮性与 DCF P0", 3)
    p(doc, "0700 产物里含字面 NaN，JSON.parse 直接抛错；另一份 0700 财务段全空（periods=[]、ratios={}）"
           "却没有任何降级标记；AAPL 现价是 331.8999938964844 这种 float32 精度；"
           "最严重的是 600519 的 DCF：wacc 3.17%、永续增长 g 3%，相差 0.17 个百分点，算出 fair_value = 74074.26，"
           "而现价 1446.44——产物仍然写着「低估」「强烈看多」「上涨 5021%」。", lead="发现：")
    p(doc, "① sanitize：非有限值 → None 并写进 data_quality.non_finite，同时归一 numpy 标量"
           "（np.float32 不是 float 子类，靠 default=str 兜底会把它悄悄写成字符串）；干净产物必须逐字节不变，"
           "否则 content_hash 会无故漂移。② completeness：四个核心章节缺数据即标 gap。"
           "③ DCF 双闸：MIN_WACC_GROWTH_SPREAD = 0.02（wacc 与 g 差距小于 2 个百分点即拒绝）、"
           "FAIR_VALUE_PRICE_BAND = (0.3, 3.0)（估值/现价越界即拒绝）。", lead="怎么改：")
    p(doc, "自查时发现自己一个错误：NaN 原本被照常建成事实，加 sanitize 后 fact_total 从 1936 变成 1930"
           "（干净产物 2663 → 2663 不变）——说明之前确实有 6 条坏事实在台账里。"
           "verify_phase3 的夹具现价从 8.0 改到 40.0（原夹具的 DCF 与自己差 8.7 倍），41/41、verify_evals 112/112。", lead="结果：")

    h(doc, "9.8 增量刷新与事实级跨 run 对比", 3)
    p(doc, "两个痛点：① 全量重算不可持续——600519 两次跑的 pipeline_time 是 355.34 秒与 221.10 秒，"
           "而一天之内真正变的只有行情、技术指标和由现价决定的估值；② 现有 diff 只看 Markdown 文本，"
           "数字变了它看不出来。", lead="为什么：")
    p(doc, "实测按 path 硬对两份 AAPL 产物：共有键 2,706–2,707（早期探针会把重复键后者覆盖前者、少算一批，"
           "正式实现按 #2/#3 后缀保留），其中 1,704 条被判「改值」——全是行情序列多一天导致的全表错位，"
           "不是真的变了。把末尾下标换成该事实的 as_of 之后降到 1,380 条，再按幅度分档："
           "噪声 1,180（float32 往返）、微小 16、实质 184。这就是对齐键必须含 as_of 的由来。", lead="发现：")
    p(doc, "刷新只重取行情、现价、PE、DCF 空间与交易信号，全程不加载 LLM 客户端（守卫用源码扫描 + "
           "sys.modules 双保险）；事实对比输出五类变化（新增/消失/改值/换源/降级反转）与三档幅度。", lead="怎么改：")
    p(doc, "真实刷新 600519 用时 3.3 秒（对比全量 221–355 秒），价格取到 1453.0（2026-11-06），"
           "2,659 条事实、unknown 0；trade_plan 的进场区间与止盈位从 74074.26 修正为 1480.60，"
           "盈亏比 1628.63 → 4.94。跨 run 对比在 600519 相邻两份里抓到 30 条实质变化，其中"
           "**SOTP 分部 value_high 两次跑相差 −25%**，为「分部数据是模型现编」提供了量化证据。", lead="结果：")
    p(doc, "过程中被守卫抓到两个真 bug：① 对齐键正则写成宽匹配 \\[\\d+\\]\\.\\w+$，"
           "把 sotp_analysis.segments[2].revenue_share 归一成 segments@2024.revenue_share，分部顺序被打乱、"
           "对比结果是假的；② 刷新绕过了 DCF 倍数闸，把基线里旧的 74074 原样带过来，重新算出「+4646% 低估」。", lead="被守卫抓到的坑：")

    # 阶段十
    h(doc, "阶段十 · 2026-11-07：价格交易日时间戳", 2)
    p(doc, "产物里最核心的那个数——现价——没有日期。AAPL 那份产物生成于 2026-11-05 00:22，"
           "current_price = 351.92，而行情最后一根 K 线是 2026-11-04；这个日期在产物里根本找不到，"
           "只能去翻 price_history.dates[-1] 反推。台账侧同样空着：AAPL 2,713 条事实里 204 条 as_of 为空"
           "（600519 是 2,659 条里 167 条），价格派生的那几条全在其中。", lead="为什么：")
    bullet(doc, "hist_pe 是五个裸浮点 [43.94, 35.9, 29.63, 22.79, 22.45]，没有年份，只能靠和 periods 对齐去猜；")
    bullet(doc, "刷新产物是混血的：current_pe = 22.103340689355033 用的是今天的收盘，"
                "pe_percentile = 0.0 还是上一期的，同一个 dict 里两个时点，产物一个字都没说；")
    bullet(doc, "同一指标两条路径精度不一致：全量管线 round 到 22.10，刷新不 round。")
    p(doc, "① 新增 app/analysis/pricing.py，在产物顶层写一个 pricing 块：price_as_of / source / "
           "is_point_in_time / requested_as_of / note，**只放字符串与布尔**，因此不产生任何事实、"
           "fact_total 一个不变；日期口径与 financial.py 取 current_price 的规则逐字对齐"
           "（按收盘价 dropna 取最后一行，而不是简单取 index[-1]，当日 K 线可能整行缺失）。"
           "② 台账对五条价格派生路径补 as_of，且只补空值。③ 分位补 hist_pe_periods 与 pe_percentile_as_of。"
           "④ 报告与看板显示「样本 2021–2025 年度均价」「现价截至 YYYY-MM-DD」。", lead="怎么改：")
    p(doc, "明确不做的两件事：技术位（进场/止损/止盈/盈亏比）不加 as_of——它们来自 60 日均线等滚动窗口，"
           "单一交易日定义不了它的「期间」，这是写进文档的已知边界；老产物不回填。", lead="边界：")
    p(doc, "真实刷新 600519 用时 3.4 秒，pricing.price_as_of = 2026-11-06、source = akshare；"
           "台账里 current_price / current_pe / trade_plan.current_price → 2026-11-06，"
           "而 pe_percentile → 2026-11-04（它确实是用 11-04 的价算的）；as_of 覆盖从基线 0.9251 升到 0.9306；"
           "L0 179/180（唯一红仍是历史 NaN 那份）；verify_pricing 44/44、verify_refresh 16/16。", lead="结果：")
    p(doc, "① 守卫一开始是假红——用了一份 DCF 已被倍数闸降级的产物做台账用例，dcf.upside 根本不存在"
           "（None 不产生事实），断言自然失败；改为按「五条事实俱在且无 pricing」挑基线。"
           "② 第一版刷新把分位标错了：基线是「上一代刷新产物」，它自己既没有 pe_percentile_as_of 也没有 pricing，"
           "直接取它的现价日会把 11-04 的分位标成 11-06。改为沿 refresh.base_artifact 回溯（≤3 层）"
           "找到真正算分位的那份产物，然后删掉按旧逻辑生成的那份产物重跑。", lead="过程中修了两处：")

    # 五
    doc.add_page_break()
    h(doc, "五、四条主线：因果归纳", 1)
    p(doc, "十个阶段看似分散，抽出来其实是四条线在同时推进。每一条都有明确的起点、转折与当前状态。")

    h(doc, "主线 A：从「说得好听」到「可审计」", 2)
    p(doc, "2026-08-10 计划书里立下「缺失数据显式 None、LLM 不产生数字」的红线 → 08-14 的 as_of vintage "
           "让它有了字段载体 → 11-04 的事实台账让它变成可执行的门禁（unknown 直接判失败）→ "
           "11-06 的 sanitize 让坏值在落盘前就被拦住 → 11-07 的 pricing 把「哪天的价」补齐。"
           "这条线的终点是一个可以被第三方复核的产物：每个数字都有出处、有期间、有降级理由。")

    h(doc, "主线 B：评测从「空转」到「有判别力」", 2)
    p(doc, "11-04 之前，59 个脚本里只有 3 个带指标，检索评测的 top_k 覆盖全部语料、召回率天然饱和。"
           "11-04 建成五层评测；11-05 把 L1 的度量对象收敛为「数值断言」、把 L2 换成人工标注、把 L4 判据从"
           "不可达改为可解释；11-06 把构成比、层状态、比对键三个门禁缺陷修掉。"
           "这条线的判据很朴素：**指标变严导致分数下降是可以接受的，指标变松导致分数上升不可接受。**"
           "8.3 的派生值两轮验证是这条主线最典型的一次：第一版让指标变好看，但它给编造发了通行证，当天回退。")

    h(doc, "主线 C：失败必须可见，而不是被伪装成正常", 2)
    p(doc, "起点是三处降级伪装：SWOT 失败返回「中性」、SOTP 伪造 100% 分部、新闻失败仍标 available=True。"
           "处理方式是让「失败」有专门的出处类型（degraded / imputed）并在 L0 里可统计。"
           "这条线最反直觉的一条原则写在记忆里：**历史 NaN 产物保留不删**——"
           "0700_HK/20261105T014111.json 的 6 个 NaN 让 L0 永远停在 179/180，"
           "不为了让指标全绿而把它删掉或排除在评测之外。")

    h(doc, "主线 D：成本与时效", 2)
    p(doc, "全量管线 221–355 秒，一天之内真正变化的信息只占很小一部分。9.8 的增量刷新把这个数字压到 3.3 秒，"
           "且结构上不加载 LLM 客户端。它不是「跑得更快」这一个收益：因为刷新便宜，"
           "事实级跨 run 对比才有意义，SOTP 分部两次跑差 25% 这种问题才可能被发现。"
           "**成本的下降直接改变了「能问什么问题」。**")

    # 六
    h(doc, "六、量化成果汇总", 1)
    p(doc, "下表全部取自 data/eval/history.jsonl 与 data/eval/baseline.json，时间为 UTC（+8 小时为本地时间）。")
    table(doc,
          ["时间(UTC)", "产物", "L0", "L1 接地率", "其它"],
          [
              ["11-04 07:07", "1", "12/12", "—", "事实 348，as_of 0.546"],
              ["11-04 07:21", "3", "36/36", "—", "事实 7,198，as_of 0.2941→0.9244"],
              ["11-04 13:22", "4", "48/48", "0.9955", "L2 旧夹具饱和；L3 28/28"],
              ["11-04 14:34", "4", "48/48", "0.9955", "L2 定序后稳定 0.9461"],
              ["11-04 16:22", "3 标的", "各 12/12", "—", "L4 4/4（v2），189.1s"],
              ["11-04 16:52", "7", "84/84", "0.9846（28/30）", "bp 修后未接地 4→8"],
              ["11-05 00:59", "11", "131/132", "0.9876（21/22）", "事实 24,877；L3 77/77"],
              ["11-06 08:49", "12", "143/144", "0.9856", "事实 26,451；L3 96/96"],
              ["11-06 09:55", "13", "155/156", "0.9952（22/22 PASS）", "L1 首次全绿"],
              ["11-06 14:38", "14", "167/168", "0.9952", "事实 30,693（+刷新产物）"],
              ["11-06 16:04", "15", "179/180", "0.9952（207/208）", "事实 33,352；as_of 0.9306"],
          ],
          widths=[3.0, 1.8, 2.4, 4.2, 7.0])
    p(doc, "L0 的断言总数随产物数量增长（132 → 144 → 156 → 168 → 180），因此「通过/总数」不可跨期直接比较；"
           "唯一一条失败始终是历史 NaN 产物，已作为 known_reds 记录在基线里。", lead="读表提示：")

    table(doc, ["维度", "现状"],
          [["守卫脚本", "55 个 verify_*.py、11 个 smoke_*.py、8 个 eval_*.py"],
           ["产物与事实", "15 份产物、33,352 条事实、unknown 计数 0"],
           ["评测", "L0 179/180（唯一红为历史 NaN）· L1 22/22 全绿 0.9952 · L2 0.9461/0.8813/0.96 · L3 104/104 · L4 4/4"],
           ["刷新耗时", "行情增量刷新 3.3–3.4 秒（全量 221–355 秒）"],
           ["知识库", "22,943 个切片（外部政策/披露/论文 + 内部研报）"],
           ["可追溯性", "as_of 覆盖 0.9251 → 0.9306；价格派生事实全部带交易日"]],
          widths=[3.2, 13.2])

    # 七
    doc.add_page_break()
    h(doc, "七、踩过的坑与教训", 1)
    p(doc, "这些坑的共同点是：它们都不是「代码写错了一行」，而是**某个默认假设本身是错的**。")
    lessons = [
        ("DataFrame 兜底不能用 or", "pandas 的 __bool__ 语义有歧义，直接抛错。SIA 的 router.py 与固收模块各踩一次，"
                                    "统一改为 is not None。"),
        ("Yahoo 403/429 是反爬，不是代理", "本机代理实测有效（204 通），仍然 403——换节点大概率无效。先看东财 F10/akshare。"),
        ("L1 报未接地，先查提示词", "0700 的「4.4%」是提示词自己写死的，模型只是照抄。先怀疑输入，再怀疑模型。"),
        ("纯按值派生复算 = 给编造发通行证", "指标会变好看，但任何两个数字的差值都能对上号。必须带文本锚定。"),
        ("层状态 ERROR ≠ FAIL", "ERROR 是评测自己崩了（例如 None × 100），不是被测对象不达标。混为一谈会修错对象。"),
        ("小分母效应", "断言数从 82 降到 48 再降到 36，单条残差权重上升；0.9444 与 1.0 的差别可能只是分母不同。"
                     "判断接地要同时看比率与绝对条数。"),
        ("构成比不能跨语料集合比", "五类出处占比恒和为 1，新增事实必然稀释比例。这不是质量退化。"),
        ("比对键要分层", "L1 按标的目录、L0 按文件名；否则要么重复报警，要么遮住第二份产物的同类失败。"),
        ("历史坏产物不删不排除", "删掉能让指标全绿，但那就等于把「系统会产出坏数据」这件事藏起来。"),
        ("三表本币 vs 挂牌币种", "混用会差约 1400 倍（ADR 经典陷阱），美股源与固收各踩一次。"),
        ("结构模型要原始科目", "build_feature_frame 只输出特征、不保留原始台账，Merton 因此恒不可用，须回查 panel。"),
        ("slope < 0 不代表倒挂", "收益率曲线正常情况下就是向上的，符号方向容易读反。"),
    ]
    for lead_text, body in lessons:
        bullet(doc, body, lead=f"{lead_text}：")

    # 八
    h(doc, "八、遗留问题（诚实清单）", 1)
    p(doc, "以下问题在材料中明确写为「未修 / 待决策 / 已知边界」，未在本轮解决。列出来的目的是不让它们被忘记。")
    table(doc, ["问题", "现状与影响"],
          [["SOTP 分部占比无来源与置信度", "两次跑相差 −25%（Garena 净利占比 45% → 85%），已量化但未修"],
           ["市场份额 / HHI 跨市场同分母", "6007 与 Amazon $716.9B 共用分母，经济含义不成立"],
           ["美股无 sector/industry 字段", "行业分类缺失，影响同业比较与适配"],
           ["美股新闻接口报 ArrowInvalid", "新闻章节对美股恒为空"],
           ["竞品 3/5 被静默丢弃", "6 家取 3 家，丢弃原因未落盘（与「失败必须可见」冲突）"],
           ["调查阈值对高毛利业务偏粗", "已改为相对变化（1.0%/0.5pp），但 91% 毛利率下 0.91pp 仍被地板压住"],
           ["技术位无 as_of", "进场/止损/止盈来自滚动窗口，单一交易日定义不了期间（明确边界）"],
           ["老产物不回填", "11-07 之前的产物无 pricing，台账 as_of 仍为空"],
           ["重排（reranker）受阻", "本机只有 bge-small-zh 等小模型且 HF 离线，C 方案待用户拍板"],
           ["UI 版式未目视验证", "11-07 的界面改动只做了导入/调用级验证"]],
          widths=[5.2, 11.2])

    # 九
    h(doc, "九、附录", 1)
    h(doc, "A.1 材料来源", 2)
    bullet(doc, "计划书（项目根）：计划书.md、计划书_美股数据源.md、计划书_自主深度调查.md")
    bullet(doc, "计划书（plan/）：多模态与市场情绪、可信层与数据飞轮_v2、产物健壮性、增量刷新与跨run对比、"
                "价格交易日时间戳、固收研究扩展")
    bullet(doc, "专题：docs/计划_对标Qovest_ABC_v1.md、docs/PRD_SIA_v2.md、README.md")
    bullet(doc, "记忆：project_super_investment_agent、project_sia_trust_evals、project_fixed_income_extension、"
                "project_backtest_workflow、project_sia_memory_layers、project_sia_external_corpus、"
                "project_sia_investigation、project_sia_us_data_source")
    bullet(doc, "硬指标：data/eval/history.jsonl（192 条）、baseline.json、latest.json、l0~l4_report.json")

    h(doc, "A.2 用户为我设定的九条工作约束", 2)
    p(doc, "这些约束直接塑造了项目的交付方式（每阶段先出计划书、逐 Step 审批、不提交 git、只跑指定解释器），"
           "也是「不编数据」这条红线能落到每一行代码的制度原因。")
    for i, text in enumerate([
        "只在指定目录工作：D:\\Desktop\\ClaudeProjects\\Super-Investment-Agent",
        "只用 D:\\python\\python.exe 解释器，依赖直接装到该解释器",
        "设计看板/UI 先提交用户审核，批准后再实现",
        "不确定用哪个 model/MCP/API 时先问用户，不自行决定",
        "每个 Phase 验证成功后返回审批；大阶段内每个实施 Step 同样逐次汇报",
        "先生成计划文档获批，再按顺序完成 Phase",
        "绝不提供捏造/错误/扭曲/编造的数据——缺失字段显式标 None/「—」，数值必须来自真实数据源或真实计算",
        "如需 NodeJS，解释器为 D:\\Node.js\\Node.js",
        "Java 已安装，仅当能明显带来更好的分析结果时才用，否则说明理由",
    ], 1):
        par = doc.add_paragraph()
        par.paragraph_format.space_after = Pt(2)
        _set_font(par.add_run(f"第 {i} 条　"), bold=True, size=10)
        _set_font(par.add_run(text), size=10)

    quote(doc, "本文档由 scripts/build_project_history_doc.py 生成。所有数字均可回到上列材料中逐项核对；"
               "材料未覆盖的内容一律标注「未标注」，未做任何推算补齐。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.core_properties.title = "Super-Investment-Agent 项目改进史"
    doc.core_properties.subject = "2026-08-10 至 2026-11-07 的改进与因果梳理"
    doc.core_properties.author = "SIA 项目记录"
    doc.save(OUT)
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"[OK] 已生成 {path}")
