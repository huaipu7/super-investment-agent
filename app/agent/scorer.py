"""Violetz 推荐评分引擎（逐字迁移自 Agent-for-Investment/workshops/scorer.py）。

基于多因子加权模型，对股票进行 0-100 分的量化评分。
分数越高，投资推荐度越高。

评分维度与权重：
  - 估值价值 (Value)     30% — PE, PB, PEG
  - 成长能力 (Growth)    25% — 营收增长率, ROE, 利润率
  - 财务健康 (Health)    25% — 负债权益比, 自由现金流, EPS
  - 市场情绪 (Sentiment) 20% — 新闻情绪, 52周位置, 股息率

纯规则、确定性评分，无 LLM。输入为 yfinance 形状的财务指标 dict（A 股经
app.agent.executor._normalize_financials 适配）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── 评分结果数据类型 ──

@dataclass
class DimensionScore:
    """单个维度的评分结果"""
    score: float            # 0-100
    weight: float           # 权重 (小数, 如 0.30)
    detail: dict = field(default_factory=dict)  # {指标名: {value, score, weight}}

    def weighted(self) -> float:
        return self.score * self.weight


@dataclass
class VioletzScore:
    """完整的 Violetz 推荐评分结果"""
    total_score: float                          # 0-100
    rating: str                                 # 评级中文名
    rating_stars: str                           # ⭐ 星星标记
    rating_color: str                           # 主题色 hex
    breakdown: dict[str, DimensionScore]        # 各维度评分
    risk_flag: Optional[str] = None             # 风险标记
    available_metrics: int = 0                  # 可用指标数
    total_metrics: int = 12                     # 总指标数


# ── 评级阈值 ──

RATINGS = [
    (85, "强烈推荐", "⭐⭐⭐⭐⭐", "#26a69a"),   # 绿色
    (70, "推荐", "⭐⭐⭐⭐", "#42a5f5"),        # 蓝色
    (55, "中性", "⭐⭐⭐", "#ffa726"),          # 橙色
    (40, "谨慎", "⭐⭐", "#ef5350"),           # 红色
    (0, "回避", "⭐", "#b71c1c"),              # 深红
]


# ── 辅助函数 ──

def _clamp(value: float, low: float, high: float) -> float:
    """将值限制在 [low, high] 区间"""
    return max(low, min(high, value))


def _pct_to_ratio(value: Optional[float]) -> Optional[float]:
    """将百分比转换为小数"""
    if value is None:
        return None
    # yfinance 的 profit_margin 和 revenue_growth 已经是小数形式
    # 但如果值 > 1，说明可能已经乘了 100，需要转换回来
    if abs(value) > 1:
        return value / 100.0
    return value


# ── 指标评分函数（各指标 → 0-100）──

def _score_pe(pe: Optional[float]) -> float:
    """PE 市盈率评分"""
    if pe is None:
        return 50.0
    if pe <= 0:
        return 10.0  # 亏损：低分
    if pe <= 10:
        return 95.0  # 极低
    if pe <= 15:
        return 85.0  # 低估
    if pe <= 25:
        return 70.0  # 合理偏低
    if pe <= 40:
        return 55.0  # 合理偏高
    if pe <= 60:
        return 35.0  # 偏高
    return 15.0      # 极高


def _score_pb(pb: Optional[float]) -> float:
    """PB 市净率评分"""
    if pb is None:
        return 50.0
    if pb <= 0:
        return 30.0  # 负净资产
    if pb <= 1.0:
        return 90.0  # 破净——但需要区分是低估还是陷阱
    if pb <= 1.5:
        return 80.0
    if pb <= 3.0:
        return 65.0
    if pb <= 5.0:
        return 50.0
    if pb <= 10.0:
        return 30.0
    return 15.0


def _score_peg(peg: Optional[float]) -> float:
    """PEG 比率评分（PEG < 1 表示可能低估）"""
    if peg is None:
        return 50.0
    if peg <= 0:
        return 40.0  # 负 PEG (盈利下降)
    if peg <= 0.5:
        return 90.0
    if peg <= 1.0:
        return 80.0
    if peg <= 1.5:
        return 65.0
    if peg <= 2.5:
        return 45.0
    return 25.0


def _score_revenue_growth(growth: Optional[float]) -> float:
    """营收增长率评分"""
    g = growth
    if g is None:
        return 50.0
    if g > 0.50:
        return 95.0  # 高速增长
    if g > 0.30:
        return 85.0
    if g > 0.15:
        return 70.0
    if g > 0.05:
        return 55.0
    if g >= 0:
        return 40.0
    # 负增长
    if g > -0.10:
        return 25.0
    return 10.0


def _score_roe(roe: Optional[float]) -> float:
    """ROE 净资产收益率评分"""
    r = roe
    if r is None:
        return 50.0
    if r > 0.35:
        return 95.0
    if r > 0.25:
        return 85.0
    if r > 0.15:
        return 70.0
    if r > 0.08:
        return 55.0
    if r > 0.03:
        return 35.0
    if r > 0:
        return 20.0
    return 10.0


def _score_profit_margin(margin: Optional[float]) -> float:
    """净利润率评分"""
    m = margin
    if m is None:
        return 50.0
    if m > 0.30:
        return 90.0
    if m > 0.20:
        return 80.0
    if m > 0.10:
        return 65.0
    if m > 0.05:
        return 50.0
    if m > 0:
        return 35.0
    if m > -0.10:
        return 20.0
    return 10.0


def _score_debt_to_equity(de: Optional[float]) -> float:
    """负债权益比评分（越低越安全）"""
    if de is None:
        return 60.0  # 通常无数据意味着债务结构简单
    if de < 0:
        return 30.0  # 负权益
    if de < 0.3:
        return 85.0
    if de < 0.6:
        return 75.0
    if de < 1.0:
        return 60.0
    if de < 2.0:
        return 40.0
    if de < 3.0:
        return 25.0
    return 15.0


def _score_free_cash_flow(fcf: Optional[float]) -> float:
    """自由现金流评分"""
    if fcf is None:
        return 50.0
    if fcf > 50e9:
        return 95.0   # >500亿
    if fcf > 10e9:
        return 80.0
    if fcf > 1e9:
        return 65.0
    if fcf > 0:
        return 50.0
    # 负 FCF
    if fcf > -5e9:
        return 30.0
    return 15.0


def _score_eps(eps: Optional[float]) -> float:
    """每股收益评分"""
    if eps is None:
        return 50.0
    if eps > 20:
        return 90.0
    if eps > 10:
        return 80.0
    if eps > 5:
        return 70.0
    if eps > 3:
        return 60.0
    if eps > 1:
        return 50.0
    if eps > 0:
        return 35.0
    return 15.0


def _score_news_sentiment(sentiment_score: Optional[float]) -> float:
    """新闻情绪评分（将 -1~+1 映射到 0-100）"""
    if sentiment_score is None:
        return 50.0
    # 从 [-1, +1] 线性映射到 [0, 100]
    return _clamp((sentiment_score + 1) * 50, 0, 100)


def _score_52w_position(current_price: Optional[float],
                        high_52w: Optional[float],
                        low_52w: Optional[float]) -> float:
    """52周位置评分

    当前价格在 52 周高低范围内的位置。
    低位（0-20%）→ 可能被低估；过高（80-100%）→ 可能追高风险
    """
    if current_price is None or high_52w is None or low_52w is None:
        return 50.0
    if high_52w <= low_52w:
        return 50.0
    position = (current_price - low_52w) / (high_52w - low_52w)
    position = _clamp(position, 0, 1.5)

    if position > 1.0:
        return 60.0  # 创新高，强势
    if position >= 0.8:
        return 40.0  # 高位
    if position >= 0.6:
        return 45.0
    if position >= 0.4:
        return 55.0
    if position >= 0.2:
        return 65.0
    return 75.0  # 低位


def _score_dividend_yield(yield_val: Optional[float]) -> float:
    """股息率评分"""
    y = _pct_to_ratio(yield_val)
    if y is None:
        return 45.0
    if y == 0:
        return 25.0
    if y > 0.06:
        return 80.0   # >6% 高息（注意可持续性）
    if y > 0.04:
        return 70.0
    if y > 0.02:
        return 60.0
    if y > 0.01:
        return 48.0
    return 35.0


# ── 维度评分 ──

def _compute_value_score(financials: dict, quote: dict) -> DimensionScore:
    """估值价值维度评分"""
    # PE: 优先用 quote 的（更实时），fallback 到 financials
    pe = quote.get("pe_ratio") or financials.get("pe_ratio") or financials.get("forward_pe")
    pb = financials.get("price_to_book")
    peg = financials.get("peg_ratio")

    pe_score = _score_pe(pe)
    pb_score = _score_pb(pb)
    peg_score = _score_peg(peg)

    detail = {}
    metrics = 0
    if pe is not None:
        detail["pe_ratio"] = {"value": pe, "score": pe_score, "label": "市盈率(PE)"}
        metrics += 1
    if pb is not None:
        detail["price_to_book"] = {"value": pb, "score": pb_score, "label": "市净率(PB)"}
        metrics += 1
    if peg is not None:
        detail["peg_ratio"] = {"value": peg, "score": peg_score, "label": "PEG"}
        metrics += 1

    # 平均分（可用指标数不足时以 50 分补齐）
    if metrics == 0:
        avg = 50.0
    elif metrics == 1:
        details = [s for k, s in detail.items()]
        avg = (sum(d["score"] for d in details) + 50 * (2 - metrics)) / 3
    else:
        avg = sum(d["score"] for d in detail.values()) / 3

    return DimensionScore(score=round(avg, 1), weight=0.30, detail=detail)


def _compute_growth_score(financials: dict) -> DimensionScore:
    """成长能力维度评分"""
    rev_growth = financials.get("revenue_growth")
    roe = financials.get("return_on_equity")
    margin = financials.get("profit_margin")

    rev_score = _score_revenue_growth(rev_growth)
    roe_score = _score_roe(roe)
    margin_score = _score_profit_margin(margin)

    detail = {}
    metrics = 0
    if rev_growth is not None:
        detail["revenue_growth"] = {"value": rev_growth, "score": rev_score, "label": "营收增长率"}
        metrics += 1
    if roe is not None:
        detail["return_on_equity"] = {"value": roe, "score": roe_score, "label": "净资产收益率(ROE)"}
        metrics += 1
    if margin is not None:
        detail["profit_margin"] = {"value": margin, "score": margin_score, "label": "净利润率"}
        metrics += 1

    if metrics == 0:
        avg = 50.0
    elif metrics == 1:
        s = list(detail.values())[0]["score"]
        avg = (s + 50 + 50) / 3
    elif metrics == 2:
        s = [d["score"] for d in detail.values()]
        avg = (sum(s) + 50) / 3
    else:
        avg = sum(d["score"] for d in detail.values()) / 3

    return DimensionScore(score=round(avg, 1), weight=0.25, detail=detail)


def _compute_health_score(financials: dict) -> DimensionScore:
    """财务健康维度评分"""
    de = financials.get("debt_to_equity")
    fcf = financials.get("free_cash_flow")
    eps = financials.get("eps")

    de_score = _score_debt_to_equity(de)
    fcf_score = _score_free_cash_flow(fcf)
    eps_score = _score_eps(eps)

    detail = {}
    metrics = 0
    if de is not None:
        detail["debt_to_equity"] = {"value": de, "score": de_score, "label": "负债权益比"}
        metrics += 1
    if fcf is not None:
        detail["free_cash_flow"] = {"value": fcf, "score": fcf_score, "label": "自由现金流"}
        metrics += 1
    if eps is not None:
        detail["eps"] = {"value": eps, "score": eps_score, "label": "每股收益(EPS)"}
        metrics += 1

    if metrics == 0:
        avg = 50.0
    elif metrics == 1:
        s = list(detail.values())[0]["score"]
        avg = (s + 50 + 50) / 3
    elif metrics == 2:
        s = [d["score"] for d in detail.values()]
        avg = (sum(s) + 50) / 3
    else:
        avg = sum(d["score"] for d in detail.values()) / 3

    return DimensionScore(score=round(avg, 1), weight=0.25, detail=detail)


def _compute_sentiment_score(quote: dict, sentiment: dict) -> DimensionScore:
    """市场情绪维度评分"""
    # 新闻情绪
    news_score_raw = sentiment.get("score") if sentiment else None
    news_score = _score_news_sentiment(news_score_raw)

    # 52周位置
    current = quote.get("current_price")
    high_52w = quote.get("52_week_high")
    low_52w = quote.get("52_week_low")
    pos_score = _score_52w_position(current, high_52w, low_52w)

    # 股息率
    div = quote.get("dividend_yield") or (sentiment or {}).get("dividend_yield")
    div_score = _score_dividend_yield(div)

    detail = {}
    metrics = 0
    if news_score_raw is not None:
        detail["news_sentiment"] = {"value": news_score_raw, "score": news_score, "label": "新闻情绪"}
        metrics += 1
    if current is not None and high_52w is not None and low_52w is not None:
        detail["week52_position"] = {"value": round(pos_score, 1), "score": pos_score, "label": "52周位置"}
        metrics += 1
    if div is not None:
        detail["dividend_yield"] = {"value": div, "score": div_score, "label": "股息率"}
        metrics += 1

    # 加权：新闻情绪 40%，52周位置 35%，股息率 25%
    total = news_score * 0.40 + pos_score * 0.35 + div_score * 0.25
    return DimensionScore(score=round(total, 1), weight=0.20, detail=detail)


# ── 主评分器 ──

class VioletzScorer:
    """Violetz 推荐评分器

    用法:
        scorer = VioletzScorer()
        result = scorer.compute(financials_dict, quote_dict, sentiment_dict)
        print(result.total_score, result.rating)
    """

    def compute(self,
                financials: Optional[dict] = None,
                quote: Optional[dict] = None,
                sentiment: Optional[dict] = None) -> VioletzScore:
        """对单只股票进行完整评分

        Args:
            financials: get_financials() 的返回结果
            quote: get_quote() 的返回结果
            sentiment: get_news_sentiment() 的返回结果

        Returns:
            VioletzScore — 包含总分、评级和详细拆分
        """
        fin = financials or {}
        q = quote or {}
        sent = sentiment or {}

        # 计算各维度
        value_dim = _compute_value_score(fin, q)
        growth_dim = _compute_growth_score(fin)
        health_dim = _compute_health_score(fin)
        sent_dim = _compute_sentiment_score(q, sent)

        breakdown = {
            "value": value_dim,
            "growth": growth_dim,
            "health": health_dim,
            "sentiment": sent_dim,
        }

        # 加权总分
        total = (value_dim.weighted() + growth_dim.weighted()
                 + health_dim.weighted() + sent_dim.weighted())
        total = round(_clamp(total, 0, 100), 1)

        # 确定评级
        rating, stars, color = self._get_rating(total)

        # 统计可用指标数
        total_metrics = 12
        available = sum(len(d.detail) for d in breakdown.values())

        return VioletzScore(
            total_score=total,
            rating=rating,
            rating_stars=stars,
            rating_color=color,
            breakdown=breakdown,
            available_metrics=available,
            total_metrics=total_metrics,
        )

    @staticmethod
    def _get_rating(score: float) -> tuple[str, str, str]:
        """根据分数返回评级"""
        for threshold, name, stars, color in RATINGS:
            if score >= threshold:
                return name, stars, color
        return RATINGS[-1][1], RATINGS[-1][2], RATINGS[-1][3]

    @staticmethod
    def get_rating_from_score(score: float) -> tuple[str, str, str]:
        """从分数直接获取评级（无需实例化）"""
        for threshold, name, stars, color in RATINGS:
            if score >= threshold:
                return name, stars, color
        return RATINGS[-1][1], RATINGS[-1][2], RATINGS[-1][3]
