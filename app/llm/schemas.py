"""LLM 输出契约 —— 各分析器期望的结构化输出模型。

设计原则：**结构严格，叶子宽容**。
  - 容器层（是不是 dict、必要键在不在）严格 —— 缺了就是模型没按契约返回，必须暴露；
  - 叶子值（数字写成字符串、"35%" 这种）能归一化就归一化，不可救则落默认值，
    不让一个坏叶子毁掉整份结果。

模型与提示词里的 JSON 示例一一对应，改提示词时要同步改这里。
校验入口是 app.llm.structured.parse_llm_json()。
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.utils.logger import logger

__all__ = [
    "IntentResult",
    "FundIntentResult",
    "SwotResult",
    "Competitor",
    "ComparisonResult",
    "GeopoliticalResult",
    "SegmentResult",
    "NewsResult",
    "MeetingResult",
    "Hypothesis",
    "HypothesisSet",
    "EvidenceProposal",
    "RoundPlan",
]


# ── 叶子值归一化 ──────────────────────────────────────────────

def _as_str(v: Any) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


def _as_str_list(v: Any) -> list[str]:
    """标量 → [标量]；None/空串 → []；列表 → 逐项转字符串。"""
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [s for s in (_as_str(x) for x in v) if s]
    return [_as_str(v)]


def _as_optional_str(v: Any) -> str | None:
    """空串 / 空白 / "null" / "未知" → None（模型常这么表达"无"）。"""
    s = _as_str(v).strip()
    if not s or s.lower() in {"null", "none", "n/a", "na", "未知"}:
        return None
    return s


def _as_float(v: Any) -> float | None:
    """尽力转 float；转不了返回 None，不抛错。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _as_str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _as_ratio(v: Any) -> float:
    """占比归一化为小数：0.35 / "0.35" / "35%" → 0.35。无法解析 → 0.0（并告警）。"""
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = _as_str(v).strip().replace(",", "")
    if not s:
        return 0.0
    pct = s.endswith("%")
    if pct:
        s = s[:-1].strip()
    f = _as_float(s)
    if f is None:
        logger.warning("占比字段无法解析为数字，按 0 处理: %r", v)
        return 0.0
    return f / 100.0 if pct else f


# 可复用的叶子类型（before-validator 先于类型检查执行）
Str = Annotated[str, BeforeValidator(_as_str)]
StrList = Annotated[list[str], BeforeValidator(_as_str_list)]
OptStr = Annotated[str | None, BeforeValidator(_as_optional_str)]
OptFloat = Annotated[float | None, BeforeValidator(_as_float)]
Ratio = Annotated[float, BeforeValidator(_as_ratio)]


def _as_optional_int(v: Any) -> int | None:
    """尽力转 int；bool/None/不可解析 → None。

    越界判定留给调用方（负索引、超范围都必须在拿到真实列表后才知道），
    这里不抛错——一个坏叶子不该毁掉整个 RoundPlan。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    s = _as_str(v).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


OptInt = Annotated[int | None, BeforeValidator(_as_optional_int)]


# ── 意图识别 ──────────────────────────────────────────────────

Intent = Literal[
    "query_price", "query_history", "analysis", "compare", "financials", "deep_compare"
]
Timeframe = Literal["1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "5y", "max"]
AnalysisType = Literal["valuation", "trend", "financial_health", "general"]

TimeframeField = Annotated[Timeframe | None, BeforeValidator(_as_optional_str)]
AnalysisTypeField = Annotated[AnalysisType | None, BeforeValidator(_as_optional_str)]


class IntentResult(BaseModel):
    """个股意图识别（app/agent/intent.py）。intent 是唯一必需字段。"""
    model_config = ConfigDict(extra="ignore")

    intent: Intent
    tickers: StrList = Field(default_factory=list)
    timeframe: TimeframeField = None
    analysis_type: AnalysisTypeField = None
    company_names: StrList = Field(default_factory=list)


FundIntent = Literal[
    "fund_nav", "fund_history", "fund_analysis", "fund_compare", "fund_holdings"
]


class FundIntentResult(BaseModel):
    """基金意图识别（app/agent/fund/intent.py）。"""
    model_config = ConfigDict(extra="ignore")

    intent: FundIntent
    fund_codes: StrList = Field(default_factory=list)
    fund_names: StrList = Field(default_factory=list)


# ── SWOT ──────────────────────────────────────────────────────

class SwotItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    point: Str = ""
    source: Str = ""

    @model_validator(mode="before")
    @classmethod
    def _norm(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"point": v}
        if isinstance(v, dict):
            d = dict(v)
            if not d.get("point"):
                for k in ("title", "detail", "description", "text"):
                    if d.get(k):
                        d["point"] = d[k]
                        break
            return d
        return {"point": _as_str(v)}


class SwotQuadrant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    strengths: list[SwotItem] = Field(default_factory=list)
    weaknesses: list[SwotItem] = Field(default_factory=list)
    opportunities: list[SwotItem] = Field(default_factory=list)
    threats: list[SwotItem] = Field(default_factory=list)


class SwotResult(BaseModel):
    """SWOT 输出。「swot」必需 —— 缺它说明模型没按契约返回。"""
    model_config = ConfigDict(extra="ignore")

    swot: SwotQuadrant
    summary: Str = ""
    rating: str = "中性"


# ── 竞品 ──────────────────────────────────────────────────────

class Competitor(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Str = ""
    ticker: OptStr = None
    reason: Str = ""


class MatrixRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dimension: Str = ""
    items: list[Any] = Field(default_factory=list)


class ComparisonResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    matrix: list[MatrixRow] = Field(default_factory=list)
    summary: Str = ""
    competitive_advantage: Str = ""
    competitive_disadvantage: Str = ""
    attribution_notes: StrList = Field(default_factory=list)


# ── 地缘政治 ──────────────────────────────────────────────────

class GeopoliticalEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event: Str = ""
    region: Str = ""
    impact: Str = ""
    probability: Str = ""
    transmission_path: Str = ""


class GeopoliticalResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    events: list[GeopoliticalEvent] = Field(default_factory=list)
    key_regions: StrList = Field(default_factory=list)
    risk_level: Str = ""


# ── 业务分部 ──────────────────────────────────────────────────

class Segment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Str = ""
    revenue_share: Ratio = 0.0
    net_income_share: Ratio = 0.0
    segment_type: Str = ""
    description: Str = ""


class SegmentResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    segments: list[Segment] = Field(default_factory=list)


# ── 新闻舆情 ──────────────────────────────────────────────────

class NewsResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sentiment: OptStr = None
    sentiment_score: OptFloat = None
    key_drivers: list[Any] = Field(default_factory=list)
    risks_opportunities: list[Any] = Field(default_factory=list)
    summary: Str = ""


# ── 股东大会 ──────────────────────────────────────────────────

class MeetingResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key_proposals: list[Any] = Field(default_factory=list)
    governance_signals: Str = ""
    governance_risks: StrList = Field(default_factory=list)
    summary: Str = ""


# ── 自主深度调查（假设驱动取证）───────────────────────────────

class Hypothesis(BaseModel):
    """一条成因假设。

    `family` 故意用裸 `Str` 而非 `Literal`：若用 Literal，模型返回一个没见过的
    族会让**整个容器**校验失败，parse_llm_json 随即回退默认值，于是每一次运行
    都静默退化成"只有规则假设"。改成在代码里过滤越界族（见
    app/investigation/hypotheses.py 的闸门 2），既安全又可观测。
    """
    model_config = ConfigDict(extra="ignore")

    id: Str = ""
    family: Str = ""
    statement: Str = ""
    predicted_observables: StrList = Field(default_factory=list)
    why_it_matters: Str = ""
    prior_confidence: OptStr = None


class HypothesisSet(BaseModel):
    """假设集合。容器层严格：`hypotheses` **必需**（不给默认值）。

    与 IntentResult.intent 同一约定：容器键缺失 = 模型没按契约返回，必须回退
    default 让上层看得见，而不是悄悄给一个空集合。注意 `{"hypotheses": []}`
    仍是合法契约（模型明确表示"无补充"）。
    """
    model_config = ConfigDict(extra="ignore")

    hypotheses: list[Hypothesis]


class EvidenceProposal(BaseModel):
    """模型对证据的**指认**——给索引；可选地自述出处，但自述必须逐字可验证。

    `source_ref_index` 指向本轮第 N 个工具结果（0 起）。`source_ref` 是模型**可选**
    声明的出处字符串（URL/标题/科目代码）：一旦提供，就必须逐字出现在该工具结果里，
    否则 `attribution_valid=False` 计零——所以编造 URL 会被结构性拒绝。不提供时由
    循环从工具结果里取规范出处。
    """
    model_config = ConfigDict(extra="ignore")

    hypothesis_id: Str = ""
    relation: Str = ""                      # supports | contradicts | neutral
    source_ref_index: OptInt = None
    source_ref: Str = ""                    # 可选；给了就必须逐字可验证
    rationale: Str = ""


class RoundPlan(BaseModel):
    """一轮的调查计划：若干动作 + 若干证据指认。"""
    model_config = ConfigDict(extra="ignore")

    actions: list[Any] = Field(default_factory=list)        # [{tool, args}]
    attributions: list[EvidenceProposal] = Field(default_factory=list)
    note: Str = ""
