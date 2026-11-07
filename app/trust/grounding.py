"""数值接地：判断一个数字能不能被「已知事实」解释。

两个层面，共用同一套数字抽取与等值比对：

  文本接地  报告正文里的数字 ⊆ 注入数据（含百分比/亿/万等派生写法）
  事实接地  模型撰写的文本里出现的数字，必须能在台账或检索片段里找到对应事实

事实接地只扫**模型写的文本**（见 `_LLM_PROSE`）。模型自己下的判断
（概率、置信度、评级、情绪分）本来就没有真值可比，不算「未接地」，
单独计入 `model_judgment`，并要求它落在合法取值域内。这样报告里的
比率不会因为「模型判断天然无法接地」而永远达不到 1.0——同时也把
「模型说的」和「数据说的」在计数上区分开了。

本模块不调用 LLM、不改产物，纯只读。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.trust import provenance as prov

# 数量级后缀 → 换算倍数
_SCALES = {
    "%": 0.01,   # 0.303 的披露值是 30.3%
    "pp": 0.01,
    "亿": 1e8,
    "万亿": 1e12,
    "万": 1e4,
}

# 前面紧贴字母/数字/点的数字属于标识符（CR3、PE、005930.KS），不是数值声明。
# 后缀分支写成「要么吃掉单位、要么后面不是字母数字」：1902亿FCF 若要求后面非字母，
# 正则会把 亿 吐回去，1902 反而被当成年份。
# 不吃前导负号：中文财经文本里 "-" 既是负号（2023年-2.8%）又是区间连接号
# （35%-55%、宏观背景-4.4%），两者无法仅凭字符区分。接地按绝对值比对，
# 符号正确性不在 L1 覆盖范围内——宁可漏判，不可把区间端点误当负数。
_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9._])"
    r"(\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(万亿|亿|万|pp|bps|bp|%|倍)|(?![A-Za-z0-9_]))"
)

# 「数值断言」判定：数字得是在陈述一个量——后面跟计量单位/货币，或处在带单位的
# 区间里。品牌名（国窖1573）、法条年份（《1974年贸易法》）、海关编码（HS 8517/8471）
# 不是在说某个量的值；把它们算进接地率，只会让指标随标的名和措辞抖动，
# 而抓不住真正「模型自己编了一个数」的情况。
_UNITS_AFTER = ("万亿", "亿", "万", "pp", "bps", "bp", "倍", "%", "％", "元",
                "美元", "港元", "人民币", "欧元", "日元", "股", "手", "个", "家",
                "项", "条", "篇", "场", "次", "只", "吨", "台", "套", "人")
_CURRENCY_PREFIX = "$¥￥€£"
_RANGE_TAIL_RE = re.compile(r"\s*[" + re.escape("-~–—至到") + r"]\s*(\d[\d,]*(?:\.\d+)?)")
_UNIT_TAIL_RE = re.compile(r"\s*(?:万亿|亿|万|pp|bps|bp|%|％|倍)")

# 证券代码是标识符不是数值声明（甲公司 SWOT 里提到乙公司的 600519）
# 收尾用 (?![A-Za-z]) 而不是 \b：中文在 Unicode 下也算 \w，
# 「600519.SS财务数据」在 \b 眼里是一个词，边界匹配不上。
_TICKER_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d{4,6})\.(?:SS|SZ|HK|KS|TW|T|L|DE|PA)(?![A-Za-z])",
    re.IGNORECASE)

_YEAR_RE = re.compile(r"(?:19|20)\d{2}")

# 时间跨度：「6 个月内」「3 年后」在说期限，不是某个量的值。地缘分析里那句
# 「6 个月内难兑现为利润」的 6 来自提示词给的 date_6m 视野，不是模型编的数据；
# 把它算成数值断言，等于逼模型把时间也说得含糊。只认后接 内/后/左右 的写法，
# 「5 年 CAGR」「30 日均线」照旧算断言。
_DURATION_RE = re.compile(r"\s*(?:个月|年|周|天|日|季度)(?:内|后|之后|以后|左右)")

# 模型撰写的文本字段。白名单而非按「未命中即检查」自动发现，是因为后者会把
# 日期、ticker、确定性来源说明一并算成数值声明。新增 LLM 段落时要同步加进来；
# 每份产物至少扫到一个字段，否则 L1 会报「白名单已腐化」。
_LLM_PROSE: tuple[str, ...] = (
    "swot_analysis.swot.strengths[*].point",
    "swot_analysis.swot.strengths[*].source",
    "swot_analysis.swot.weaknesses[*].point",
    "swot_analysis.swot.weaknesses[*].source",
    "swot_analysis.swot.opportunities[*].point",
    "swot_analysis.swot.opportunities[*].source",
    "swot_analysis.swot.threats[*].point",
    "swot_analysis.swot.threats[*].source",
    "swot_analysis.summary",
    "geopolitical_analysis.geopolitical_events.events[*].event",
    "geopolitical_analysis.geopolitical_events.events[*].impact",
    "geopolitical_analysis.geopolitical_events.events[*].transmission_path",
    "geopolitical_analysis.tariff_sensitivity.message",
    "news_analysis.key_drivers[*]",
    "news_analysis.risks_opportunities[*]",
    "news_analysis.summary",
    "competitive_analysis.competitors[*].reason",
    "sotp_analysis.segments[*].description",
)

# 模型判断的合法取值域（(下界, 上界)，闭区间）
_JUDGMENT_DOMAIN: dict[str, tuple[float, float]] = {
    "probability": (0.0, 100.0),
    "confidence": (0.0, 1.0),
    "sentiment_score": (-1.0, 1.0),
    "china_exposure": (0.0, 1.0),
    "gross_margin_impact": (-1.0, 1.0),
}


@dataclass(frozen=True)
class Token:
    raw: str
    value: float
    unit: str | None
    span: tuple[int, int]

    @property
    def decimals(self) -> int:
        return len(self.raw.split(".")[1]) if "." in self.raw else 0

    @property
    def is_year(self) -> bool:
        return self.unit is None and bool(_YEAR_RE.fullmatch(self.raw.replace(",", "")))


def extract_tokens(text: str) -> list[Token]:
    out: list[Token] = []
    for m in _NUM_RE.finditer(text):
        raw, unit = m.group(1), m.group(2)
        try:
            value = float(raw.replace(",", "").replace("−", "-"))
        except ValueError:
            continue
        out.append(Token(raw=raw, value=value, unit=unit, span=m.span()))
    return out


def _unit_after(text: str, pos: int) -> str | None:
    rest = text[pos:pos + 8].lstrip()
    for u in _UNITS_AFTER:
        if rest.startswith(u):
            return u
    return None


def is_claim(text: str, tok: Token) -> bool:
    """这个 token 是否在陈述一个量，而不是名称 / 编码 / 年份。

    判据只认**带精度的写法**，四类任一成立即为断言：
      1. 带单位或百分号（`%`、`亿`、`倍`、`元`、`股`…）
      2. 带小数位（`2.45`、`98.2`）
      3. 带千分位（`27,738,852,603`）
      4. 带货币符号，或处在带单位的区间里（`600-1000 元`、`8.5-9.5%`）

    光秃秃的整数（`国窖1573`、`茅台1935`、`HS 8517`）不算——它们在文本里
    几乎总是名称或编码的一部分。这条线故意从严不从宽：宁可多算一个断言
    （分母变大、比率更难达标），也不把真实数值当噪声放过去。
    """
    if tok.unit is not None:
        return True
    raw = tok.raw
    if "." in raw or "," in raw:
        return True
    if _unit_after(text, tok.span[1]):
        return True
    # 区间端点：600-1000 元 / WACC 8.5-9.5%
    m = _RANGE_TAIL_RE.match(text, tok.span[1])
    if m:
        end = m.end()
        if _UNIT_TAIL_RE.match(text, end) or _unit_after(text, end):
            return True
    if tok.span[0] > 0 and text[tok.span[0] - 1] in _CURRENCY_PREFIX:
        return True
    return False


def is_duration(text: str, tok: Token) -> bool:
    """整数紧跟「个月内/年后/周左右」这类期限词时，是在讲时间不是讲量。"""
    if tok.unit is not None or "." in tok.raw or "," in tok.raw:
        return False
    return bool(_DURATION_RE.match(text, tok.span[1]))


def variants(v: float) -> tuple[float, ...]:
    """同一事实在报告里的等价写法：原值、百分比、万/亿/万亿，以及取负。

    取负是因为抽取时不带符号（见 `_NUM_RE` 的说明）：-2.8% 的增长率会以
    2.8 去比对，必须让负事实也能被它命中。
    """
    base = (v, v * 100, v / 1e4, v / 1e8, v / 1e12)
    return base + tuple(-x for x in base)


def _same_at_display(token: Token, v: float) -> bool:
    """已知值按「显示位数」四舍五入或截断后，是否等于文中写的那个数。

    不用浮点容差：容差的量级判不准边界。写 99.99% 而事实是 100%（cr5=1.0），
    用 ±0.01 的绝对容差会正好落在边界上被吃成「已接地」——伪造值就这么滑过去了。
    改成把事实渲染成同样的位数再逐字比：四舍五入与截断都算对，
    1360.56 亿写成「1,360 亿」和 39.85 倍写成「39.8 倍」都属正常取整，
    而 100.00 写不出 99.99。
    """
    dec = token.decimals
    scale = 10 ** dec
    cands = (round(v, dec), math.floor(v * scale) / scale)
    eps = 1e-9 * max(1.0, abs(token.value))
    return any(abs(c - token.value) <= eps for c in cands)


def best_match(token: Token, known: Sequence[float]) -> tuple[float | None, float]:
    """返回 (匹配到的已知值, 最小偏差)；无匹配时第一项为 None。

    偏差只用于未命中时的诊断输出（告诉人差多远），不参与判定。
    """
    best = float("inf")
    found: float | None = None
    for v in known:
        if not math.isfinite(v):
            # 历史产物里可能残留 NaN（源在当日 K 线未成交时整行 NaN），它不是事实也不能参与匹配
            continue
        for a in variants(v):
            if _same_at_display(token, a):
                return v, abs(a - token.value)
            d = abs(a - token.value)
            if d < best:
                best, found = d, v
    return None, best


# ── 派生值：文本锚定的复算 ──────────────────────────────────────
#
# 模型常把「2022 年 20.5% → 2025 年 33.1%」写成「三年扩张 1260bp」。这不是编造，
# 但也不能按值硬凑——2026-11-05 试过纯按值复算，立刻把「CR3 98.2%」解释成
# 流动比率 2010−2019 的差、把「600 元」解释成 ROE 2003−2017 的差：只看数值
# 和比例尺、不认文本说的是哪个指标，小数差放大几十上百倍就能撞上任意 bp 数。
#
# 所以派生解释必须**在文本里锚定三件事**，缺一不算：
#   1. 同一句里出现指标名（毛利率 / 净利率 / 营业利润率 / ROE / 流动比率 …）
#   2. 同一句里出现两个不同期间（年度）
#   3. 台账里该指标在那两个期间都有事实，且差值（按显示位数）对得上
# 「150-250bp 增量空间」这类没有期间的假设、以及不带指标名的数，仍然判未接地。

_METRIC_ALIASES: tuple[tuple[str, str], ...] = (
    ("毛利率", "GROSS_MARGIN"),
    ("净利率", "NET_MARGIN"),
    ("营业利润率", "OPERATING_MARGIN"),
    ("经营利润率", "OPERATING_MARGIN"),
    ("净资产收益率", "ROE"),
    ("总资产收益率", "ROA"),
    ("流动比率", "CURRENT_RATIO"),
)

_PERIOD_RE = re.compile(r"(?:19|20)\d{2}")
_SENT_END = "。！？；\n"


def _sentence_bounds(text: str, pos: int) -> tuple[int, int]:
    start = max(text.rfind(c, 0, pos) for c in _SENT_END) + 1
    ends = [i for i in (text.find(c, pos) for c in _SENT_END) if i != -1]
    return start, (min(ends) if ends else len(text))


def _sentence_at(text: str, pos: int) -> str:
    start, end = _sentence_bounds(text, pos)
    return text[start:end]


# token 的单位决定换算。两个尺度都要留：台账事实是比率（0.3309），
# 但句中已接地的数字往往是显示写法（33.1），差值分别落在万分之一 / 百分之一
# 两个量级上。bp 的 1e4 对应比率差，1e2 对应「33.1 − 19.4 = 14.6 个百分点」。
_UNIT_SCALES = {"bp": (1e4, 1e2), "%": (1e2, 1.0), "pp": (1e2, 1.0), "倍": (1.0,)}


def _scales_for(tok: Token) -> tuple[float, ...]:
    return _UNIT_SCALES.get(tok.unit, (1e2,))


def match_anchored(token: Token, facts, text: str) -> dict | None:
    """把 token 解释为「同句指标名 + 两个期间」对应的台账两数之差。"""
    sent = _sentence_at(text, token.span[0])
    periods = list(dict.fromkeys(_PERIOD_RE.findall(sent)))
    if len(periods) < 2:
        return None
    metrics = [m for alias, m in _METRIC_ALIASES if alias in sent]
    if not metrics:
        return None

    scales = _scales_for(token)
    for metric in dict.fromkeys(metrics):
        by_period = {f.as_of: f for f in facts if f.metric == metric and f.as_of}
        for i, p in enumerate(periods):
            for q in periods[i + 1:]:
                a, b = by_period.get(p), by_period.get(q)
                if not a or not b:
                    continue
                deltas = (a.value - b.value, b.value - a.value,
                          (round(a.value * 100, 1) - round(b.value * 100, 1)) / 100,
                          (round(b.value * 100, 1) - round(a.value * 100, 1)) / 100)
                for delta in deltas:
                    for scale in scales:
                        if _same_at_display(token, delta * scale):
                            return {"mode": "指标+期间", "metric": metric,
                                    "periods": [p, q],
                                    "values": [round(a.value, 4), round(b.value, 4)]}
    return None


def explain_by_sentence_pair(token: Token, grounded: list[tuple[int, float]],
                             text: str) -> dict | None:
    """同句里两个**已接地**数字之差。

    覆盖「2023 年净利率从 34.0% 骤降至 19.4%（-1460bp）」这种只写了一个年份、
    另一端靠上下文隐含的写法：34.0 与 19.4 本身都已各自接地，差值就可验证。
    只在该句内部配对，且要求显示位数精确相等——比全台账乱配窄得多。
    """
    start, end = _sentence_bounds(text, token.span[0])
    vals = [v for pos, v in grounded if start <= pos < end]
    if len(vals) < 2:
        return None
    for i, a in enumerate(vals):
        for b in vals[i + 1:]:
            for scale in _scales_for(token):
                for cand in ((a - b) * scale, (b - a) * scale):
                    if _same_at_display(token, cand):
                        return {"mode": "同句配对", "metric": None,
                                "periods": None, "values": [a, b]}
    return None


# ── 文本接地（报告正文 ⊆ 注入数据）──────────────────────────────

def flatten_numbers(node, out: list[float] | None = None) -> list[float]:
    """收集任意 JSON 结构里的全部数值叶子。"""
    if out is None:
        out = []
    if isinstance(node, bool):
        return out
    if isinstance(node, (int, float)):
        out.append(float(node))
    elif isinstance(node, dict):
        for v in node.values():
            flatten_numbers(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            flatten_numbers(v, out)
    return out


def ground_text(text: str, data: dict | Sequence[float]) -> dict:
    """报告正文的数字有多少能归到注入数据。"""
    known = flatten_numbers(data) if not isinstance(data, (list, tuple)) else list(data)
    toks = extract_tokens(text)
    unmatched = [t.raw for t in toks if best_match(t, known)[0] is None]
    n = len(toks)
    return {
        "tokens": n,
        "matched": n - len(unmatched),
        "unmatched": unmatched,
        "rate": 0.0 if not n else round((n - len(unmatched)) / n, 4),
    }


# ── 事实接地（模型撰写的文本里带数字的断言）─────────────────────

def _norm(path: str) -> str:
    return prov.normalize(path)


@dataclass(frozen=True)
class DocFact:
    """检索片段里的一个数值：值已换算到基准量纲，另带片段日期与出处链接。

    没有指标名，无法参与「指标+期间」锚定（同一篇文档里的数都是同一个日期，
    凑不出两个期间），它只回答「这个数在检索到的原文里出现过吗」。
    `raw`/`unit` 保留原文写法，供正向控制把它原样写回模型文本。
    """
    value: float
    as_of: str
    evidence_ref: str
    raw: str = ""
    unit: str | None = None


def document_facts(artifact: dict) -> list[DocFact]:
    """把 knowledge.snippets 里的数值断言抽成证据事实。

    纯只读：不写回 Ledger、不进序列化，所以 L0 的 fact_total 与相关断言不变。
    值按 `_SCALES` 换算到基准量纲（91.96% → 0.9196、1.2万亿 → 1.2e12），
    这样模型换个量级写法（12000 亿元）也能被同一批 variants 命中。
    """
    knowledge = artifact.get("knowledge") or {}
    snippets = knowledge.get("snippets") or []
    out: list[DocFact] = []
    seen: set[float] = set()
    for s in snippets:
        if not isinstance(s, dict):
            continue
        text = str(s.get("content") or "")
        if not text.strip():
            continue
        meta = s.get("metadata") or {}
        as_of = str(meta.get("date") or "")[:10]
        ref = str(meta.get("url") or "")
        for tok in extract_tokens(text):
            if not is_claim(text, tok):
                continue
            v = tok.value * _SCALES.get(tok.unit or "", 1.0)
            if not math.isfinite(v) or v in seen:
                continue
            seen.add(v)
            out.append(DocFact(value=v, as_of=as_of, evidence_ref=ref,
                               raw=tok.raw, unit=tok.unit))
    return out


_LLM_PROSE_SET = frozenset(_norm(p) for p in _LLM_PROSE)


def _iter_strings(node, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_strings(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _iter_strings(v, f"{path}[{i}]")
    elif isinstance(node, str) and node.strip():
        yield path, node


def iter_llm_prose(artifact: dict) -> list[tuple[str, str]]:
    return [(p, s) for p, s in _iter_strings(artifact) if _norm(p) in _LLM_PROSE_SET]


def artifact_tickers(artifact: dict) -> set[str]:
    """产物里出现过的证券代码的纯数字部分。"""
    found: set[str] = set()
    for _p, s in _iter_strings(artifact):
        found.update(_TICKER_RE.findall(s))
    for node in (artifact.get("company_info") or {},):
        if isinstance(node.get("ticker"), str):
            found.add(node["ticker"].split(".")[0])
    return found


def ground_llm_prose(ledger, artifact: dict) -> dict:
    """模型文本里的数值断言逐条找工作底既有事实。

    分母只算**数值断言**（`is_claim`：带单位/货币/百分号，或处于带单位的区间）。
    名称内嵌数字、法条年份、海关编码等单列为 `non_claim`，不进分母，
    但抽样列在报告里——不计入不等于看不见。

    年份一律单列为 `year_ref`：晚于产物生成时间的计入 `future_year`。
    期间一致性由 L3 的「期间一致：年份须在事实期间内」负责，不在 L1 重复计分。
    模型自算的派生值（如「三年扩张 1260bp」）**不做按值复算**——理由见上方
    「关于「派生值」的否决记录」：按值匹配会把编造的数字也凑成某个差值。
    provenance 判为 llm 的字段（概率/置信度等）是模型判断，单列为
    `model_judgment`，只校验取值域。

    检索片段里的数（`document_facts`）也算出处：模型照抄年报/政策原文的数字
    不该被判未接地。这类命中单独计 `doc_grounded`，与台账命中区分开，
    台账本身不被修改、不参与序列化。
    """
    ledger_known = [f.value for f in ledger.facts]
    docs = document_facts(artifact)
    doc_known = [f.value for f in docs]
    known = ledger_known + doc_known
    fact_years = {f.as_of[:4] for f in ledger.facts if f.as_of}
    current_year = max([int(y) for y in fact_years if y.isdigit()], default=0)

    fields = iter_llm_prose(artifact)
    tickers = artifact_tickers(artifact)
    matched = unmatched = future = judgment = ticker_ref = anchored = 0
    doc_grounded = 0
    anchored_matches: list[dict] = []
    doc_grounded_samples: list[dict] = []
    non_claim = year_ref = time_ref = 0
    non_claim_grounded = non_claim_unmatched = 0
    non_claim_samples: list[dict] = []
    non_claim_misses: list[dict] = []
    bad_domain: list[dict] = []
    misses: list[dict] = []

    for path, text in fields:
        path_prov, _ = prov.classify(path)
        grounded: list[tuple[int, float]] = []   # 同段里已接地的 (位置, 值)
        pending: list[tuple[Token, float]] = []  # 直接比对没命中的断言，留待解释
        for tok in extract_tokens(text):
            # 提别的公司代码是在指认标的，不是在陈述数值
            if tok.unit is None and tok.raw.replace(",", "") in tickers:
                ticker_ref += 1
                continue
            # 模型判断字段：只校验取值域，不与数据比对
            if path_prov == prov.LLM:
                judgment += 1
                key = _norm(path).rsplit(".", 1)[-1]
                lo_hi = _JUDGMENT_DOMAIN.get(key)
                if lo_hi and not (lo_hi[0] <= tok.value <= lo_hi[1]):
                    bad_domain.append({"path": path, "raw": tok.raw})
                continue

            if tok.is_year:
                year_ref += 1
                y = int(tok.raw.replace(",", ""))
                if current_year and y > current_year:
                    future += 1
                continue

            if is_duration(text, tok):
                time_ref += 1
                continue

            if not is_claim(text, tok):
                # 不进分母，但仍逐条比对：无出处的裸整数要在报告里看得见，
                # 否则「不计入」就成了「看不见」。
                non_claim += 1
                hit, best = best_match(tok, known)
                if hit is None:
                    non_claim_unmatched += 1
                    if len(non_claim_misses) < 10:
                        non_claim_misses.append({"path": path, "raw": tok.raw,
                                                 "nearest_gap": round(best, 6)})
                else:
                    non_claim_grounded += 1
                if len(non_claim_samples) < 10:
                    non_claim_samples.append({"path": path, "raw": tok.raw})
                continue

            # 先台账、再检索片段：两者的命中在计数上分开，出处才说得清
            hit, best = best_match(tok, ledger_known)
            if hit is None and doc_known:
                hit_doc, best_doc = best_match(tok, doc_known)
                best = min(best, best_doc)
                if hit_doc is not None:
                    doc_grounded += 1
                    matched += 1
                    grounded.append((tok.span[0], tok.value))
                    if len(doc_grounded_samples) < 10:
                        doc_grounded_samples.append({"path": path, "raw": tok.raw})
                    continue
            if hit is None:
                pending.append((tok, best))
            else:
                matched += 1
                grounded.append((tok.span[0], tok.value))

        for tok, best in pending:
            explained = match_anchored(tok, ledger.facts, text)
            if explained is None:
                explained = explain_by_sentence_pair(tok, grounded, text)
            if explained is not None:
                anchored += 1
                matched += 1
                if len(anchored_matches) < 20:
                    anchored_matches.append({"path": path, "raw": tok.raw,
                                             **explained})
            else:
                unmatched += 1
                misses.append({"path": path, "raw": tok.raw,
                               "nearest_gap": round(best, 6)})

    denom = matched + unmatched
    return {
        "fields": len(fields),
        "tokens": (matched + unmatched + future + judgment + ticker_ref
                   + non_claim + year_ref + time_ref),
        "matched": matched,
        "anchored": anchored,
        "anchored_matches": anchored_matches,
        "unmatched": unmatched,
        "future_year": future,
        "model_judgment": judgment,
        "ticker_ref": ticker_ref,
        "doc_facts": len(docs),
        "doc_grounded": doc_grounded,
        "doc_grounded_samples": doc_grounded_samples,
        "non_claim": non_claim,
        "non_claim_grounded": non_claim_grounded,
        "non_claim_unmatched": non_claim_unmatched,
        "non_claim_samples": non_claim_samples,
        "non_claim_misses": non_claim_misses,
        "year_ref": year_ref,
        "time_ref": time_ref,
        "rate": 1.0 if denom == 0 else round(matched / denom, 4),
        "misses": misses[:20],
        "domain_violations": bad_domain,
    }


# ── 反向控制：伪造的数字必须被检出 ──────────────────────────────

_CONTROL_VALUE = 99.99


def _inject(artifact: dict, suffix: str | None = None) -> dict | None:
    """把一段文本塞进模型段落里，返回副本；找不到可注入位置返回 None。"""
    import copy

    suffix = suffix or f"。预期市占率将达 {_CONTROL_VALUE}%"
    probe = copy.deepcopy(artifact)
    swot = (probe.get("swot_analysis") or {}).get("swot") or {}
    for key in ("strengths", "weaknesses", "opportunities", "threats"):
        items = swot.get(key) or []
        if items and isinstance(items[0], dict) and items[0].get("point"):
            items[0]["point"] = f"{items[0]['point']}{suffix}"
            return probe
    return None


def probe_document_facts(ledger, artifact: dict) -> dict:
    """正向控制：把片段里真实出现过、台账里没有的数写回模型文本。

    命中台账的数会被台账先接住、doc_grounded 不动，所以探针只挑台账解释不了的值。
    片段里没有这样的数时 applicable=False（不是失败，只是这个产物没得探）。
    """
    ledger_vals = [f.value for f in ledger.facts]
    picked = None
    for f in document_facts(artifact):
        if f.unit is None and "." not in f.raw:
            continue  # 光秃秃的整数写回去也不再是断言，探针无效
        num = f"{f.raw}{f.unit or ''}"
        toks = extract_tokens(num)
        if not toks or best_match(toks[0], ledger_vals)[0] is not None:
            continue  # 台账能解释，测不出片段这一路
        picked = num
        break
    if picked is None:
        return {"applicable": False, "detected": False,
                "note": "检索片段里没有台账之外的数值断言"}
    probe = _inject(artifact, f"。据检索原文，该指标约为 {picked}")
    if probe is None:
        return {"applicable": False, "detected": False,
                "note": "产物中没有可注入的模型文本"}
    before = ground_llm_prose(ledger, artifact)
    after = ground_llm_prose(ledger, probe)
    return {
        "applicable": True,
        "detected": after["doc_grounded"] > before["doc_grounded"],
        "injected": picked,
        "doc_grounded_before": before["doc_grounded"],
        "doc_grounded_after": after["doc_grounded"],
        "rate_before": before["rate"],
        "rate_after": after["rate"],
    }


def control_llm_prose(ledger, artifact: dict) -> dict:
    """在模型文本里混入 99.99%，未接地的断言必须变多。"""
    probe = _inject(artifact)
    if probe is None:
        return {"applicable": False, "detected": False, "note": "产物中没有可注入的模型文本"}
    before = ground_llm_prose(ledger, artifact)
    after = ground_llm_prose(ledger, probe)
    return {
        "applicable": True,
        # 判定看未接地「条数」是否变多，不看比率是否下降：分母极小时（整份产物只有
        # 2 条断言且都未接地）比率恒为 0，注入伪造值也不会更低，会把「匹配器居然
        # 接住了 99.99」这个真问题误报成「控制失效」。
        "detected": after["unmatched"] > before["unmatched"],
        "rate_before": before["rate"],
        "rate_after": after["rate"],
        "injected": _CONTROL_VALUE,
    }
