"""市场分析和行业集中度分析

支持市场份额估算、行业集中度（CR3/CR5/HHI）计算和 TAM 估算。
可单公司运行（仅报告已知数据）或多公司对比（计算集中度）。
"""

from typing import Optional

import pandas as pd

from app.constants import INDUSTRY_CLASSIFICATION
from app.analysis.industry import IndustryClass
from app.utils.logger import logger


class MarketAnalyzer:
    """市场份额与行业分析器"""

    # Phase 6: TAM 份额映射（按 IndustryClass）
    TAM_SHARE_BY_INDUSTRY = {
        IndustryClass.FINANCIAL: 0.20,
        IndustryClass.TELECOM: 0.20,
        IndustryClass.UTILITIES: 0.20,
        IndustryClass.REAL_ESTATE: 0.03,
        IndustryClass.CONSUMER_STAPLES: 0.03,
        IndustryClass.ENERGY: 0.10,
        IndustryClass.HEALTHCARE: 0.10,
        IndustryClass.INDUSTRIAL: 0.10,
        IndustryClass.TECH_HARDWARE: 0.10,
        IndustryClass.TECH_SOFTWARE: 0.10,
    }

    def analyze(
        self,
        financials: dict[str, pd.DataFrame],
        company_info: dict,
        competitor_data: Optional[list[dict]] = None,
        industry_cls: Optional[IndustryClass] = None,
    ) -> dict:
        """执行市场分析

        Args:
            financials: 目标公司财务报表
            company_info: 目标公司信息（含 sector, industry 等）
            competitor_data: 竞品数据列表
                [{"name": str, "revenue": float, "period": str, ...}, ...]

        Returns:
            {
                "company": {"name": str, "industry": str, "revenue": float, ...},
                "market_share": float | None,
                "industry_concentration": {"cr3": float|None, "cr5": float|None, "hhi": float|None},
                "tam_estimate": float | None,
                "industry_growth": float | None,
                "competitors": [{"name": str, "revenue": float, "share": float}, ...],
            }
        """
        # 提取目标公司营收
        income = financials.get("income", pd.DataFrame())
        revenue = self._get_latest_revenue(income)
        latest_period = self._get_latest_period(income)

        company_info = company_info or {}
        company_name = company_info.get("name", "")
        industry = company_info.get("industry", "")
        sector = company_info.get("sector", "")

        result = {
            "company": {
                "name": company_name,
                "ticker": company_info.get("ticker", ""),
                "industry": industry,
                "sector": sector,
                "revenue": revenue,
                "latest_period": latest_period,
                "market_cap": company_info.get("market_cap"),
            },
            "market_share": None,
            "industry_concentration": {"cr3": None, "cr5": None, "hhi": None},
            "tam_estimate": None,
            "competitors": [],
        }

        # 如果有竞品数据，计算市场集中度
        if competitor_data:
            result = self._compute_concentration(result, competitor_data, revenue, industry_cls)

        logger.info(
            "市场分析完成: %s, 营收=%s, 竞品=%d 家",
            company_name or "未知", revenue, len(result["competitors"]),
        )
        return result

    def analyze_industry_competitors(
        self,
        target_revenue: Optional[float],
        competitors: list[dict],
        target_name: str = "",
    ) -> dict:
        """分析目标公司在竞品中的市场位置

        Args:
            target_revenue: 目标公司营收
            competitors: 竞品列表 [{"name": str, "revenue": float, "period": str}, ...]
            target_name: 目标公司名

        Returns:
            {
                "total_market": float,         # 已知市场总规模
                "market_share": float | None,  # 目标公司份额
                "rank": int,                   # 排名
                "cr3": float | None,           # 前三集中度
                "cr5": float | None,           # 前五集中度
                "hhi": float | None,           # Herfindahl-Hirschman Index
                "competitors": [...],
            }
        """
        all_players = list(competitors)
        if target_revenue is not None:
            all_players.append({
                "name": target_name,
                "revenue": target_revenue,
            })

        # 按营收排序
        valid = [p for p in all_players if p.get("revenue") is not None and p["revenue"] > 0]
        valid.sort(key=lambda x: x["revenue"], reverse=True)

        if not valid:
            return {"total_market": 0, "market_share": None, "rank": 0,
                    "cr3": None, "cr5": None, "hhi": None, "competitors": []}

        total = sum(p["revenue"] for p in valid)
        n = len(valid)

        # 计算份额
        for p in valid:
            p["share"] = p["revenue"] / total if total > 0 else 0

        # CR3 / CR5
        cr3 = sum(p["share"] for p in valid[:3]) if n >= 3 else sum(p["share"] for p in valid)
        cr5 = sum(p["share"] for p in valid[:5]) if n >= 5 else sum(p["share"] for p in valid)

        # HHI = sum of squared market shares (as %)
        shares_pct = [p["share"] * 100 for p in valid]
        hhi = sum(s * s for s in shares_pct)

        # 目标公司排名
        rank = 0
        target_share = None
        if target_revenue is not None and target_revenue > 0:
            for i, p in enumerate(valid):
                if p["name"] == target_name:
                    rank = i + 1
                    target_share = p.get("share")
                    break
            if rank == 0:
                rank = len(valid)

        return {
            "total_market": total,
            "market_share": target_share,
            "rank": rank,
            "cr3": cr3,
            "cr5": cr5,
            "hhi": hhi,
            "competitors": valid,
        }

    def estimate_tam(
        self,
        industry: str,
        company_revenue: float,
        industry_cls: Optional[IndustryClass] = None,
    ) -> Optional[float]:
        """基于行业和公司营收估算 TAM（总可寻址市场）

        使用行业集中度经验估算：
        - 高度分散行业（零售、餐饮等）：公司份额通常 < 5%
        - 适度集中行业（制造、科技等）：公司份额通常在 5-20%
        - 高度集中行业（电信、银行等）：公司份额可能 > 20%

        Args:
            industry: 行业名称
            company_revenue: 公司营收
            industry_cls: 行业分类枚举（Phase 6，优先于行业名称字符串）

        Returns:
            估算的 TAM（None 表示无法估算）
        """
        if not company_revenue or company_revenue <= 0:
            return None

        # Phase 6: 优先使用 IndustryClass 枚举确定份额
        if industry_cls is not None and industry_cls in self.TAM_SHARE_BY_INDUSTRY:
            assumed_share = self.TAM_SHARE_BY_INDUSTRY[industry_cls]
            return company_revenue / assumed_share

        # 向后兼容：原有硬编码字符串匹配逻辑
        fragmented_industries = ["商贸零售", "COMMERCIAL_RETAIL", "社会服务", "SOCIAL_SERVICES",
                                 "建筑装饰", "BUILDING_DECORATION", "房地产", "REAL_ESTATE"]
        concentrated_industries = ["银行", "BANKING", "通信", "TELECOMMUNICATION",
                                   "国防军工", "NATIONAL_DEFENSE", "公用事业", "PUBLIC_UTILITIES",
                                   "交通运输", "TRANSPORTATION"]

        industry_upper = industry.upper() if industry else ""

        if industry in fragmented_industries or industry_upper in fragmented_industries:
            assumed_share = 0.03  # 假设 3% 份额
        elif industry in concentrated_industries or industry_upper in concentrated_industries:
            assumed_share = 0.20  # 假设 20% 份额
        else:
            assumed_share = 0.10  # 默认假设 10%

        return company_revenue / assumed_share

    # ── 内部辅助方法 ──────────────────────────────────────────

    def _get_latest_revenue(self, income: pd.DataFrame) -> Optional[float]:
        """获取最新一期营收"""
        if income.empty or "REVENUE" not in income.columns:
            return None
        series = income["REVENUE"].dropna()
        if series.empty:
            return None
        try:
            return float(series.iloc[-1])
        except (TypeError, ValueError):
            return None

    def _get_latest_period(self, income: pd.DataFrame) -> Optional[str]:
        """获取最新期间标签"""
        if income.empty:
            return None
        try:
            return str(income.index[-1])
        except (TypeError, IndexError):
            return None

    def _compute_concentration(
        self,
        result: dict,
        competitor_data: list[dict],
        target_revenue: Optional[float],
        industry_cls: Optional[IndustryClass] = None,
    ) -> dict:
        """计算行业集中度"""
        analysis = self.analyze_industry_competitors(
            target_revenue=target_revenue,
            competitors=competitor_data,
            target_name=result["company"]["name"],
        )

        result["market_share"] = analysis["market_share"]
        result["industry_concentration"] = {
            "cr3": analysis["cr3"],
            "cr5": analysis["cr5"],
            "hhi": analysis["hhi"],
        }
        result["tam_estimate"] = self.estimate_tam(
            result["company"].get("industry", ""),
            target_revenue or 0,
            industry_cls=industry_cls,
        ) if target_revenue else None

        # 排除目标公司后返回竞品列表
        result["competitors"] = [
            p for p in analysis["competitors"]
            if p["name"] != result["company"]["name"]
        ]

        return result
