"""出处归类表：路径模式 → 数值出处。

只有五种合法出处（外加一个 UNKNOWN 表示未归类）：

    computed  纯 Python 计算（确定性，可复算）
    source    外部数据源原值（财报、行情、检索文档）
    llm       模型产出（自由文本里的数字、模型估计的占比/评分）
    imputed   插补/前向填充
    degraded  降级占位（模块失败时的兜底值）

设计要点：**匹配不上任何模式即 UNKNOWN，而 UNKNOWN 在 L0 评测里判失败**。
所以这张表不能腐化——新增字段若不归类，评测立刻报错。

路径规范化：下标统一写成 `[*]`，例如
    sotp_analysis.segments[2].revenue_share → sotp_analysis.segments[*].revenue_share
"""
from __future__ import annotations

import fnmatch
import re

COMPUTED = "computed"
SOURCE = "source"
LLM = "llm"
IMPUTED = "imputed"
DEGRADED = "degraded"
UNKNOWN = "unknown"

VALID = frozenset({COMPUTED, SOURCE, LLM, IMPUTED, DEGRADED})

_INDEX_RE = re.compile(r"\[\d+\]")


def normalize(path: str) -> str:
    """把列表下标折叠成 `[*]`，使模式表与具体下标无关。"""
    return _INDEX_RE.sub("[*]", path)


# (路径模式, 出处, evidence_ref 提示)
# 顺序敏感：先具体后宽泛，首个命中生效。
RULES: tuple[tuple[str, str, str | None], ...] = (
    # ── 模型产出的数字：必须单独标记，且要过数值接地 ──
    ("sotp_analysis.segments[*].revenue_share", LLM, "llm:segment_share"),
    ("sotp_analysis.segments[*].net_income_share", LLM, "llm:segment_share"),
    ("sotp_analysis.segments[*].segment_type", LLM, "llm:segment_classify"),
    ("news_analysis.sentiment_score", LLM, "llm:sentiment"),
    ("news_analysis.sentiment", LLM, "llm:sentiment"),
    ("geopolitical_analysis.tariff_sensitivity.*", LLM, "llm:tariff_estimate"),
    ("swot_analysis.rating", LLM, "llm:rating"),

    # ── 财务衍生序列：建立在原始财报之上，但数值本身是算出来的 ──
    ("financial_analysis.dupont_breakdown[*].*", COMPUTED, "dupont"),
    ("financial_analysis.roic[*].*", COMPUTED, "roic"),
    # 历史 PE 用「当前股本」折算全部年份，是近似而非精确复算
    ("financial_analysis.valuation_context.hist_pe[*]", COMPUTED,
     "approx:current_shares_outstanding"),
    ("financial_analysis.valuation_context.current_pe", COMPUTED,
     "approx:current_shares_outstanding"),
    ("financial_analysis.valuation_context.pe_percentile", COMPUTED, "pe_percentile"),

    # 季度趋势：营收/净利是财报原值，同比是计算
    ("financial_analysis.quarterly_trend.periods[*].revenue", SOURCE,
     "financials:quarterly"),
    ("financial_analysis.quarterly_trend.periods[*].net_income", SOURCE,
     "financials:quarterly"),
    ("financial_analysis.quarterly_trend.periods[*].revenue_yoy", COMPUTED, "yoy"),
    ("financial_analysis.quarterly_trend.periods[*].net_income_yoy", COMPUTED, "yoy"),

    # ── 硬编码假设表驱动的确定性计算（必须排在下面的通用通配之前）──
    # supply_chain 的情景系数/概率是 app/analysis/supply_chain.py 里的常量，
    # 数值本身由 Python 算出；其中 china_exposure 来自 LLM，已在 evidence 标明。
    ("supply_chain_analysis.*", COMPUTED, "assumption:SUPPLY_CHAIN_SCENARIOS"),
    ("sector_adapter.*", COMPUTED, "config:industry"),

    ("*.probability", LLM, "llm:probability"),
    ("*.confidence", LLM, "llm:confidence"),

    # ── 外部数据源原值 ──
    ("financial_analysis.current_price", SOURCE, "quote"),
    ("financial_analysis.raw_metrics.*.value", SOURCE, "financials"),
    ("financial_analysis.periods[*]", SOURCE, "financials:period"),
    ("financial_analysis.metadata.period_count", COMPUTED, "len(periods)"),
    ("company_info.employees", SOURCE, "profile"),
    ("company_info.market_cap", SOURCE, "profile"),
    ("company_info.shares_outstanding", SOURCE, "profile"),
    ("market_analysis.company.*", SOURCE, "competitor_table"),
    ("market_analysis.markets.*.company.*", SOURCE, "competitor_table"),
    ("market_analysis.competitors[*]", SOURCE, "competitor_table"),
    ("market_analysis.markets.*.competitors[*]", SOURCE, "competitor_table"),
    ("market_analysis.markets.*.competitor_data[*].revenue", SOURCE, "competitor_table"),
    ("market_analysis.markets.*.competitor_data[*].market_cap", SOURCE, "competitor_table"),
    ("market_analysis.markets.*.competitor_data[*].pe", SOURCE, "competitor_table"),
    ("competitive_analysis.competitors[*]", SOURCE, "competitor_table"),
    ("fact_checks.corrected_competitors[*]", SOURCE, "competitor_table"),
    ("price_history.*", SOURCE, "history"),
    ("knowledge.*", SOURCE, "rag"),

    # ── 纯计算 ──
    ("financial_analysis.cleaning.*", COMPUTED, "cleaning"),
    ("financial_analysis.ratios.*", COMPUTED, "ratio"),
    ("financial_analysis.growth_rates.*", COMPUTED, "growth"),
    ("financial_analysis.fcf_history[*]", COMPUTED, "fcf"),
    ("valuation_analysis.*", COMPUTED, "valuation"),
    ("sotp_analysis.*", COMPUTED, "sotp"),
    ("trade_plan.*", COMPUTED, "trade_plan"),
    ("market_analysis.market_share", COMPUTED, "share"),
    ("market_analysis.industry_concentration.*", COMPUTED, "concentration"),
    ("market_analysis.tam_estimate", COMPUTED, "heuristic:TAM_SHARE_BY_INDUSTRY"),
    ("market_analysis.*.market_share", COMPUTED, "share"),
    ("market_analysis.*.industry_concentration.*", COMPUTED, "concentration"),
    ("market_analysis.*.tam_estimate", COMPUTED, "heuristic:TAM_SHARE_BY_INDUSTRY"),
    ("market_analysis.markets.*.market_share", COMPUTED, "share"),
    ("market_analysis.markets.*.industry_concentration.*", COMPUTED, "concentration"),
    ("market_analysis.markets.*.tam_estimate", COMPUTED, "heuristic:TAM_SHARE_BY_INDUSTRY"),
    ("market_analysis.markets.*.competitor_data[*].share", COMPUTED, "share"),
    ("pipeline_time", COMPUTED, "runtime"),

    # ── 计数：由列表长度得出，不是外部原值 ──
    ("news_analysis.article_count", COMPUTED, "len(articles)"),
    ("news_analysis.briefing_count", COMPUTED, "len(briefings)"),
    ("meeting_analysis.meeting_count", COMPUTED, "len(meetings)"),
)


def _escape_brackets(pattern: str) -> str:
    """fnmatch 里 `[...]` 是字符类，`[*]` 会退化成「匹配一个星号字符」。

    路径规范化后下标就是字面 `[*]`，必须转义成 `[[]*[]]` 才能按字面匹配。
    """
    return "".join(
        "[[]" if c == "[" else "[]]" if c == "]" else c for c in pattern
    )


_COMPILED: tuple[tuple[str, str, str | None], ...] = tuple(
    (_escape_brackets(p), prov, ev) for p, prov, ev in RULES
)


def classify(path: str) -> tuple[str, str | None]:
    """返回 (出处, evidence_ref)。未命中任何模式 → (UNKNOWN, None)。"""
    p = normalize(path)
    for pattern, prov, evidence in _COMPILED:
        if fnmatch.fnmatchcase(p, pattern):
            return prov, evidence
    return UNKNOWN, None


def is_valid(provenance: str) -> bool:
    return provenance in VALID
