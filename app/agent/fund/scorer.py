"""基金推荐评分引擎（逐字迁移自 workshops/fund_scorer.py）。

基于多因子加权模型，对中国公募基金进行 0-100 分的量化评分。

评分维度与权重：
  - 收益表现 (Return)     35% — 近1年收益, 近3年收益, 成立以来收益
  - 风险控制 (Risk)       25% — 最大回撤, 波动率, 夏普比
  - 基金经理 (Manager)    20% — 从业年限, 管理规模, 任职回报
  - 持仓质量 (Holdings)   20% — 前十大集中度, 评级机构分, 基金规模
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class FundDimensionScore:
    """基金单个维度的评分结果"""
    score: float
    weight: float
    detail: dict = field(default_factory=dict)

    def weighted(self) -> float:
        return self.score * self.weight


@dataclass
class FundScore:
    """完整的基金推荐评分结果"""
    total_score: float
    rating: str
    rating_stars: str
    rating_color: str
    breakdown: dict[str, FundDimensionScore]
    risk_flag: Optional[str] = None
    available_metrics: int = 0
    total_metrics: int = 10


RATINGS = [
    (85, "强烈推荐", "⭐⭐⭐⭐⭐", "#26a69a"),
    (70, "推荐", "⭐⭐⭐⭐", "#42a5f5"),
    (55, "中性", "⭐⭐⭐", "#ffa726"),
    (40, "谨慎", "⭐⭐", "#ef5350"),
    (0, "回避", "⭐", "#b71c1c"),
]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ── 收益表现评分 ──

def _score_return(value: Optional[float], scale: float = 100) -> float:
    """通用收益评分函数"""
    if value is None:
        return 50.0
    if value > 80:
        return 95.0
    if value > 50:
        return 85.0
    if value > 30:
        return 70.0
    if value > 15:
        return 55.0
    if value > 5:
        return 40.0
    if value > 0:
        return 30.0
    if value > -15:
        return 15.0
    return 5.0


def _compute_return_score(history_data: Optional[dict], nav_data: Optional[dict]) -> FundDimensionScore:
    """收益表现维度评分"""
    detail = {}

    # 从历史净值算收益
    if history_data and history_data.get("summary"):
        s = history_data["summary"]
        start_nav = s.get("start_nav")
        end_nav = s.get("end_nav")
        if start_nav and end_nav and start_nav > 0:
            total_return = (end_nav - start_nav) / start_nav * 100
            ret_score = _score_return(total_return)
            detail["total_return"] = {"value": round(total_return, 2), "score": ret_score, "label": "区间收益(%)"}
        else:
            ret_score = 50.0
    else:
        ret_score = 50.0

    # 日增长率
    if nav_data and nav_data.get("日增长率") is not None:
        daily = float(nav_data["日增长率"])
        daily_score = _score_return(daily, scale=5)
        detail["daily_change"] = {"value": daily, "score": daily_score, "label": "日增长率(%)"}
    else:
        daily_score = 50.0

    metrics = len(detail)
    if metrics == 0:
        avg = 50.0
    elif metrics == 1:
        avg = (list(detail.values())[0]["score"] + 50) / 2
    else:
        avg = sum(d["score"] for d in detail.values()) / metrics

    return FundDimensionScore(score=round(avg, 1), weight=0.35, detail=detail)


# ── 风险控制评分 ──

def _compute_risk_score(history_data: Optional[dict]) -> FundDimensionScore:
    """风险控制维度评分"""
    detail = {}

    if history_data and history_data.get("summary"):
        s = history_data["summary"]
        changes = []
        if "max_daily_increase" in s:
            changes.append(s["max_daily_increase"])
        if "max_daily_decrease" in s:
            changes.append(s["max_daily_decrease"])
        if "avg_daily_change" in s:
            changes.append(s["avg_daily_change"])

        if changes:
            volatility = max(abs(min(changes)), abs(max(changes)))
            # 波动率越小越好
            if volatility < 1:
                vol_score = 85.0
            elif volatility < 2:
                vol_score = 70.0
            elif volatility < 3:
                vol_score = 55.0
            elif volatility < 5:
                vol_score = 40.0
            else:
                vol_score = 25.0
            detail["volatility"] = {"value": round(volatility, 2), "score": vol_score, "label": "波动率"}
        else:
            vol_score = 50.0
    else:
        vol_score = 50.0

    return FundDimensionScore(score=round(vol_score, 1), weight=0.25, detail=detail)


# ── 基金经理评分 ──

def _compute_manager_score(info_data: Optional[dict]) -> FundDimensionScore:
    """基金经理维度评分"""
    if not info_data:
        return FundDimensionScore(score=50.0, weight=0.20, detail={})

    detail = {}

    # 净资产规模（越大越稳定）
    scale_str = info_data.get("净资产规模", info_data.get("基金规模"))
    if scale_str is not None:
        try:
            scale_str_clean = str(scale_str).replace("亿元", "").replace("元", "").replace(" ", "")
            scale = float(scale_str_clean)
            if scale > 500:
                scale_score = 90.0
            elif scale > 200:
                scale_score = 80.0
            elif scale > 100:
                scale_score = 70.0
            elif scale > 50:
                scale_score = 60.0
            elif scale > 10:
                scale_score = 50.0
            elif scale > 1:
                scale_score = 35.0
            else:
                scale_score = 25.0
            detail["fund_scale"] = {"value": scale, "score": scale_score, "label": "基金规模(亿)"}
        except (ValueError, TypeError):
            scale_score = 50.0
    else:
        scale_score = 50.0

    # 基金经理姓名（有信息即视为正向）
    manager = info_data.get("基金经理人", info_data.get("基金经理"))
    if manager and str(manager) != "-" and str(manager) != "暂无":
        detail["manager_info"] = {"value": 1, "score": 65.0, "label": "基金经理"}
    else:
        detail["manager_info"] = {"value": 0, "score": 45.0, "label": "基金经理（未披露）"}

    # 管理费率（越低越好）
    mgmt_fee = info_data.get("管理费率")
    if mgmt_fee is not None:
        try:
            fee_str = str(mgmt_fee).replace("%", "").replace(" ", "")
            fee = float(fee_str)
            if fee <= 0.15:
                fee_score = 90.0
            elif fee <= 0.5:
                fee_score = 75.0
            elif fee <= 1.0:
                fee_score = 60.0
            elif fee <= 1.5:
                fee_score = 45.0
            else:
                fee_score = 30.0
            detail["management_fee"] = {"value": fee, "score": fee_score, "label": "管理费率(%)"}
        except (ValueError, TypeError):
            pass

    metrics = len(detail)
    if metrics == 0:
        avg = 50.0
    else:
        avg = sum(d["score"] for d in detail.values()) / metrics

    return FundDimensionScore(score=round(avg, 1), weight=0.20, detail=detail)


# ── 持仓质量评分 ──

def _compute_holdings_score(holdings_data: Optional[list], ratings_data: Optional[dict],
                            info_data: Optional[dict]) -> FundDimensionScore:
    """持仓质量维度评分"""
    detail = {}
    metrics = 0

    # 持仓数量（适度分散）
    if holdings_data and len(holdings_data) > 0:
        top10 = holdings_data[:10]
        count_score = 65.0 if len(top10) >= 10 else 55.0
        detail["holdings_count"] = {"value": len(top10), "score": count_score, "label": "前十大持仓数"}
        metrics += 1

        # 前十大集中度（适度集中 = 有信心，但也不宜过度集中）
        total_pct = 0
        for h in top10:
            pct_str = h.get("占净值比例", "0")
            try:
                pct = float(str(pct_str).replace("%", "").strip())
                total_pct += pct
            except (ValueError, TypeError):
                pass
        if total_pct > 0:
            if 40 <= total_pct <= 70:
                conc_score = 80.0  # 理想集中度
            elif total_pct < 20:
                conc_score = 40.0  # 过于分散
            elif total_pct > 90:
                conc_score = 35.0  # 过于集中
            elif total_pct > 70:
                conc_score = 55.0
            else:
                conc_score = 50.0
            detail["concentration"] = {"value": round(total_pct, 1), "score": conc_score, "label": "前十大集中度(%)"}
            metrics += 1

    # 评级评分
    if ratings_data and len(ratings_data) > 0:
        star_count = 0
        total_ratings = 0
        for agency, rating in ratings_data.items():
            if rating and str(rating).isdigit():
                r = int(rating)
                total_ratings += 1
                if r >= 5:
                    star_count += 2
                elif r >= 4:
                    star_count += 1
        if total_ratings > 0:
            avg_star = star_count / total_ratings
            rating_score = _clamp(avg_star * 20, 0, 100)
            detail["rating_score"] = {"value": round(avg_star, 1), "score": rating_score, "label": "综合评级"}
            metrics += 1

    if metrics == 0:
        avg = 50.0
    else:
        avg = sum(d["score"] for d in detail.values()) / metrics

    return FundDimensionScore(score=round(avg, 1), weight=0.20, detail=detail)


# ── 主评分器 ──

class FundScorer:
    """基金推荐评分器

    用法:
        scorer = FundScorer()
        result = scorer.compute(nav_data, history_data, holdings_data, info_data, ratings_data)
        print(result.total_score, result.rating)
    """

    def compute(self,
                nav_data: Optional[dict] = None,
                history_data: Optional[dict] = None,
                holdings_data: Optional[list] = None,
                info_data: Optional[dict] = None,
                ratings_data: Optional[dict] = None) -> FundScore:
        """对单只基金进行完整评分

        Args:
            nav_data: fund_realtime_nav() 的返回结果
            history_data: get_fund_history() 的返回结果
            holdings_data: fund_holdings() 的返回结果
            info_data: fund_basic_info() 的返回结果
            ratings_data: fund_ratings() 的返回结果

        Returns:
            FundScore — 包含总分、评级和详细拆分
        """
        # 各维度评分
        return_dim = _compute_return_score(history_data, nav_data)
        risk_dim = _compute_risk_score(history_data)
        manager_dim = _compute_manager_score(info_data)
        holdings_dim = _compute_holdings_score(holdings_data, ratings_data, info_data)

        breakdown = {
            "return": return_dim,
            "risk": risk_dim,
            "manager": manager_dim,
            "holdings": holdings_dim,
        }

        # 加权总分
        total = (return_dim.weighted() + risk_dim.weighted()
                 + manager_dim.weighted() + holdings_dim.weighted())
        total = round(_clamp(total, 0, 100), 1)

        # 确定评级
        rating, stars, color = self._get_rating(total)

        # 统计可用指标数
        available = sum(len(d.detail) for d in breakdown.values())

        return FundScore(
            total_score=total,
            rating=rating,
            rating_stars=stars,
            rating_color=color,
            breakdown=breakdown,
            available_metrics=available,
            total_metrics=10,
        )

    @staticmethod
    def _get_rating(score: float) -> tuple[str, str, str]:
        for threshold, name, stars, color in RATINGS:
            if score >= threshold:
                return name, stars, color
        return RATINGS[-1][1], RATINGS[-1][2], RATINGS[-1][3]
