"""SOTP 分部估值分析器

识别公司业务分部，分配行业合理估值倍数，计算 SOTP 加总估值。
"""

from typing import Optional

from app.utils.logger import logger
from app.analysis.llm import analysis_llm
from app.analysis.master_prompt import build_system_prompt
from app.llm.schemas import SegmentResult
from app.llm.structured import parse_llm_json
from app.trust import strict_mode

ROLE_VALUATION_ANALYST = "你是一位资深投行估值分析师，擅长企业估值建模，精通 DCF、SOTP、可比公司分析等多种估值方法。"

# 分部类型 → 建议 PE 倍数区间
SEGMENT_MULTIPLE_RANGES: dict[str, tuple[float, float]] = {
    "硬件/设备": (15.0, 20.0),
    "软件/服务": (25.0, 30.0),
    "企业级/云": (25.0, 35.0),
    "广告": (20.0, 25.0),
    "金融": (10.0, 15.0),
    "消费品牌": (20.0, 30.0),
    "医疗健康": (15.0, 25.0),
    "工业": (12.0, 18.0),
    "能源": (8.0, 12.0),
    "电信": (10.0, 15.0),
    "新兴业务": (0.0, 5.0),  # 期权价值，用营收倍数而非 PE
    "其他": (10.0, 20.0),
}

# 新兴/期权类分部适用营收倍数（而非 PE）
_NET_INCOME_SEGMENT_TYPES = {"硬件/设备", "软件/服务", "企业级/云", "广告",
                              "金融", "消费品牌", "医疗健康", "工业", "能源",
                              "电信", "其他"}

# CoT Prompt：识别业务分部
SEGMENT_COT_PROMPT = """请按以下三步推理过程，识别目标公司的业务分部结构。

公司名称: {company_name}
行业: {industry}
业务描述: {business_summary}

### 第一步：业务扫描
分析该公司的业务组合：
- 有哪些独立的收入来源/业务线？
- 各业务线的商业模式有何区别（硬件 vs 软件 vs 服务 vs 广告等）？
- 哪些业务处于成熟期，哪些处于增长期/新兴阶段？

### 第二步：分部划分
将业务归纳为 2-5 个可独立估值的分部，每个分部应有：
- 显著的收入贡献（>5% 总营收）
- 可区分的商业模式和盈利特征
- 可适用的行业估值倍数

### 第三步：估值参数估计
对每个分部，估计：
- 营收占比（占总营收百分比）
- 净利润占比（占总净利润百分比，可根据行业利润率特征推断）
- 适用的分部类型（从以下选择：硬件/设备、软件/服务、企业级/云、广告、金融、消费品牌、医疗健康、工业、能源、电信、新兴业务、其他）

输出 JSON：
{{
    "segments": [
        {{
            "name": str,
            "revenue_share": float,
            "net_income_share": float,
            "segment_type": str,
            "description": str
        }}
    ]
}}

要求：
1. revenue_share 和 neti_income_share 为 0-1 之间的小数，各分部之和应 ≈ 1.0
2. segment_type 必须从上述列表中选择
3. 只返回 JSON，不要额外说明
4. 如果信息不足，返回 {{"segments": [{{"name": "综合业务", "revenue_share": 1.0, "net_income_share": 1.0, "segment_type": "其他", "description": "无法细分"}}]}}
"""


class SegmentAnalyzer:
    """SOTP 分部估值分析器"""

    def __init__(self):
        self._client = analysis_llm

    def identify_segments(self, company_info: dict, macro_line: str = "") -> list[dict]:
        """使用 LLM 识别业务分部

        Args:
            company_info: 包含 name/industry/summary 的公司信息

        Returns:
            [{"name": str, "revenue_share": float, "net_income_share": float,
              "segment_type": str, "description": str}, ...]
        """
        name = company_info.get("name", "")
        industry = company_info.get("industry", "")
        summary = company_info.get("summary", "")

        if not name and not industry:
            logger.warning("缺少公司信息，无法识别业务分部")
            return [self._default_segment()]

        prompt = SEGMENT_COT_PROMPT.format(
            company_name=name or "未知",
            industry=industry or "未知",
            business_summary=summary[:800] if summary else "暂无详细业务描述",
        )

        try:
            system_prompt = build_system_prompt(ROLE_VALUATION_ANALYST, macro_line)
            response = self._client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
                system_prompt=system_prompt,
            )
            result = parse_llm_json(response, model=SegmentResult, default={},
                                    context="业务分部")
            segments = result.get("segments", []) if isinstance(result, dict) else []
            if not segments:
                logger.warning("LLM 未返回有效分部，使用默认单分部")
                return [self._default_segment()]

            # 归一化占比
            total_rev = sum(s.get("revenue_share", 0) for s in segments)
            total_ni = sum(s.get("net_income_share", 0) for s in segments)
            if total_rev > 0:
                for s in segments:
                    s["revenue_share"] = s.get("revenue_share", 0) / total_rev
            if total_ni > 0:
                for s in segments:
                    s["net_income_share"] = s.get("net_income_share", 0) / total_ni

            logger.info("识别到 %d 个业务分部: %s", len(segments),
                        [s.get("name", "?") for s in segments])
            return segments
        except Exception as e:
            logger.error("业务分部识别失败: %s", e)
            return [self._default_segment()]

    def compute_sotp(
        self,
        segments: list[dict],
        total_net_income: Optional[float],
        shares_outstanding: Optional[float],
        current_price: Optional[float] = None,
    ) -> dict:
        """计算 SOTP 加总估值

        Args:
            segments: identify_segments 的输出
            total_net_income: 公司总净利润
            shares_outstanding: 流通股数
            current_price: 当前股价（可选，用于计算 upside）

        Returns:
            {
                "segments": [
                    {
                        "name": str,
                        "revenue_share": float,
                        "net_income_share": float,
                        "segment_type": str,
                        "pe_low": float,
                        "pe_high": float,
                        "value_low": float | None,
                        "value_high": float | None,
                    }
                ],
                "sotp_value_low": float | None,
                "sotp_value_high": float | None,
                "upside_low": float | None,
                "upside_high": float | None,
                "per_share_low": float | None,
                "per_share_high": float | None,
            }
        """
        if not segments or total_net_income is None or total_net_income <= 0:
            return {"segments": segments, "sotp_value_low": None, "sotp_value_high": None,
                    "per_share_low": None, "per_share_high": None,
                    "upside_low": None, "upside_high": None,
                    "note": "净利润数据不足，无法计算 SOTP"}

        # 分部没识别出来时，所谓「加总」只剩一个 100% 的分部，本质是拿单一
        # PE 倍数乘以净利润——那是相对估值，不是分部加总。不出数，说明原因。
        if any(isinstance(s, dict) and s.get("degraded") for s in segments):
            return {"segments": segments, "sotp_value_low": None, "sotp_value_high": None,
                    "per_share_low": None, "per_share_high": None,
                    "upside_low": None, "upside_high": None,
                    "degraded": True,
                    "reason": "业务分部识别失败，单分部不构成加总估值，故不输出 SOTP 数值"}

        sotp_result = []
        total_value_low = 0.0
        total_value_high = 0.0

        for seg in segments:
            seg_type = seg.get("segment_type", "其他")
            ni_share = seg.get("net_income_share", 0)
            seg_ni = total_net_income * ni_share

            # 获取适用倍数区间
            pe_range = SEGMENT_MULTIPLE_RANGES.get(seg_type, (10.0, 20.0))
            pe_low, pe_high = pe_range

            # 对新兴业务等非净利润型分部使用营收倍数
            if seg_type == "新兴业务":
                # 新兴业务使用营收倍数（0-5x），但需要营收数据
                # 无独立营收数据时标记为期权价值
                seg_value_low = None
                seg_value_high = None
            elif seg_ni > 0:
                seg_value_low = seg_ni * pe_low
                seg_value_high = seg_ni * pe_high
                total_value_low += seg_value_low
                total_value_high += seg_value_high
            else:
                seg_value_low = None
                seg_value_high = None

            sotp_result.append({
                "name": seg.get("name", ""),
                "revenue_share": seg.get("revenue_share", 0),
                "net_income_share": ni_share,
                "segment_type": seg_type,
                "pe_low": pe_low,
                "pe_high": pe_high,
                "value_low": seg_value_low,
                "value_high": seg_value_high,
            })

        result = {
            "segments": sotp_result,
            "sotp_value_low": total_value_low if total_value_low > 0 else None,
            "sotp_value_high": total_value_high if total_value_high > 0 else None,
            "per_share_low": None,
            "per_share_high": None,
            "upside_low": None,
            "upside_high": None,
        }

        if shares_outstanding and shares_outstanding > 0:
            if total_value_low > 0:
                result["per_share_low"] = total_value_low / shares_outstanding
            if total_value_high > 0:
                result["per_share_high"] = total_value_high / shares_outstanding

            if current_price and current_price > 0:
                low_ps = result.get("per_share_low")
                high_ps = result.get("per_share_high")
                if low_ps:
                    result["upside_low"] = (low_ps - current_price) / current_price
                if high_ps:
                    result["upside_high"] = (high_ps - current_price) / current_price

        return result

    def _default_segment(self) -> dict:
        """LLM 失败时的单分部占位。

        占位分部保留 100% 占比只是为了让下游算术不崩；这不是识别结果，
        必须带 degraded 标记，否则「综合业务 100%」会被当成真实分部。
        """
        seg = {
            "name": "综合业务",
            "revenue_share": 1.0,
            "net_income_share": 1.0,
            "segment_type": "其他",
            "description": "无法细分业务分部",
        }
        if strict_mode():
            seg["degraded"] = True
            seg["reason"] = "LLM 未返回有效分部，此为单分部占位而非识别结果"
        return seg

