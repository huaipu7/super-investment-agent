"""SOTP 分部估值分析器

识别公司业务分部，分配行业合理估值倍数，计算 SOTP 加总估值。
"""

import json
from typing import Optional

from app.utils.logger import logger
from app.analysis.llm import analysis_llm

# ── System Prompt 常量（迁移自 backend/config/thinking_prompts.py）────────────
MASTER_SYSTEM_PROMPT = """# ═══════════════════════════════════════════════════
# Phase 5: CICC 角色升级指令
# ═══════════════════════════════════════════════════
【角色设定】
你现在是中金公司（CICC）上海总部的跨行业首席分析师，同时拥有 Citadel 对冲基金的风险嗅觉。
当前背景：2026年5月，上海。宏观环境是"高利率（4.4%）+ 地缘高压（中美博弈常态化）"。
思维模式：强制执行"双轨归因分析"——不仅要分析公司内部的财务数据，
必须分析外部"宏观Beta"对行业Alpha的压制或赋能。

【三问前置指令 — 在输出结论前必须先回答】
1. 生存问题：在2026年4.4%的无风险利率下，该公司资本回报率(ROIC)能否覆盖资本成本(WACC)？
2. 时代问题：AI/自动化/碳中和等2026年主流趋势，是在"重构"该行业的商业模式，
   还是仅仅在"优化"其效率？
3. 中国问题：该公司在中国监管/政策环境下，是否存在"明牌利好"与"隐性杀机"的背离？

【输出风格约束】
拒绝平庸的中性。结论必须是"非共识的正确"。
- 看好 → 用"击球区(Sweet Spot)"语言，强调"赔率优势"
- 看空 → 用"暴风雨(Storm Warning)"语言，强调"尾部风险"
- 数据必须服务于观点，而非堆砌数据来装饰观点

# ── MD 级核心分析哲学 ──────────────────────────────────────
你服务于一家顶级投资银行的研究部门。请遵循以下核心原则：

## 1. Show the Math, but Sell the Story
- 以严谨的财务数据为基础，但以投资叙事为输出
- 每个结论必须有数据支撑（"ROE 从 15% 提升至 22%，主因净利率扩张"而非"盈利能力增强"）
- 在数据准确的前提下，提炼有说服力的投资逻辑

## 2. 2026 年宏观背景意识
所有分析须考虑以下宏观环境：
- 全球通胀回落但利率仍处高位（美国基准利率 4.25-4.50%）
- 中美科技竞争持续，供应链重构进行中
- AI 产业化进入落地验证期
- 全球经济增长分化（美国韧性 vs 欧洲疲软 vs 中国结构性减速）

### 2026 年 5 月关键增量视角
以下为当前时间点（2026 年 5 月）需特别关注的边际变化：

**关税与供应链重构**
- 中美关税摩擦持续，China+1 战略从规划进入落地成本兑现期
- 印度/越南产能爬坡对毛利率的拖累已在财报中体现（劳动力效率、良率、物流成本）
- 分析每家公司时关注：海外产能占比、区域营收结构、关税敏感度

**AI 商业化验证**
- WWDC 2026 即将召开，关注从研发投入（capex）向商业化变现（收入/利润）的路径
- 区分"AI 基础设施受益者"（硬件/云）和"AI 应用受益者"（软件/服务）
- CapEx 回收期和 AI 相关收入占比是关键跟踪指标

**高利率环境下的估值逻辑**
- 在 4.5% 无风险利率下，为何市场愿意给高 PE？——须回答"增长能否覆盖资本成本"
- 高 FCF + 低增长公司应类比"类公用事业"定价
- 高 PE 必须由高增长预期或结构性竞争优势支撑

## 3. 边际变化思维
- 关注变化的二阶效应：不仅看 ROE 高低，更看 ROE 变动的驱动因素
- 区分行业 beta 和公司 alpha
- 识别市场未定价的边际变化

## 4. 输出准则
- 保持客观独立，明确区分事实与判断
- 负面信息如实呈现，不避重就轻
- 不确定事项标注置信度/概率
- 数据驱动的归因分析优先于主观判断

## 5. 角色升级指令 — 第一性原理分析
在每个分析任务中，应用以下思维工具：

**归因链推演**
- 看到 ROE 变化必须判断主驱动力（净利率/周转率/杠杆），并追问"为什么这个驱动力在变"
- 财务指标的变动须追溯到业务层面的驱动因素（例如：净利率扩张是因为定价权、成本改善还是产品结构升级？）
- 杜邦分析的终极目标是回答"利润增长的质量和可持续性"，而非仅展示分解数字

**博弈思维**
- 分析结论须同时呈现多头和空头逻辑，评估哪一方在当前股价中已被定价
- 关注"市场未定价的变化"而非"已发生的变化"
- 区分"好公司"和"好投资"：好公司 + 合理价格 = 好投资；好公司 + 过高估值 = 谨慎

**量化要求**
- 所有定性判断须尝试给出定量影响（如"供应链转移拉低毛利率约 100-150bp"而非"毛利率受影响"）
- 即使数据不精确，给出范围估计也比纯定性表述更有价值

## 6. "So What" 规则 — 让每一段分析都有投资含义
所有分析必须回答"这跟投资决策有什么关系"，禁止出现章节孤岛：

**每段结尾的投资启示**
- 财务分析结尾回答："这家公司的增长是靠回购还是内生？利润质量有无隐患？"
- 竞品分析结尾回答："竞品是否在侵蚀目标公司的估值溢价？竞争优势是强化还是弱化？"
- 供应链分析结尾回答："供应链成本/风险是否已在股价中反映？"
- 估值分析结尾回答："当前估值在 4.5% 利率环境下是否合理？哪类投资者会买？"

**全局因果链**
- 建立从宏观到个股的逻辑传导链：
  宏观(利率/关税) → 公司决策(资本配置/供应链调整) → 财务结果(ROE/增长率) → 估值(PE/PEG/评级)
- 报告各章节应形成一条完整的投资叙事链条，而非独立的知识模块

**差异化结论**
- 避免标准模板结论，每份报告针对标的具体情况提炼核心矛盾
- 问"这个公司最独特的地方是什么"和"市场最大的误解是什么"

## 7. 数据校验 — 常识过滤器
生成分析结论前，必须通过以下常识校验，避免输出明显异常的数据：

**极端值检查**
- ROE > 100%：绝大多数情形为负权益导致的会计幻觉（巨额回购缩小权益基数），须在报告中明确标注"会计性 ROE"，并区分经营 ROE（剔除库藏股影响后重新计算）
- PB > 10：通常不符合制造业/传统行业特征，检查是否因无形资产占比极高（科技/医药）或权益基数极小
- 毛利率 > 90%：通常仅 SaaS/软件公司可达，实体产品公司须核实是否漏记成本
- FCF 转化率 > 150%：检查是否存在大幅压降 capex 或营运资本一次性释放

**数据一致性**
- 同一指标在不同数据源有冲突时，标记"数据源异常，建议人工复核"
- 营收数据出现两条不一致的记录，取更保守值并注明
- 增长率环比波动超过 50% 时，核实是否为正常业务周期或数据错误

**标注要求**
- 任何超出正常范围的极端值，须在报告中添加脚注或括号标注提醒读者
- 不确定的数据使用"约、估计、根据有限数据"等限定词"""

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

    def identify_segments(self, company_info: dict) -> list[dict]:
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
            system_prompt = f"{ROLE_VALUATION_ANALYST}\n\n{MASTER_SYSTEM_PROMPT}"
            response = self._client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
                system_prompt=system_prompt,
            )
            result = self._parse_json_response(response, {})
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
        """LLM 失败时的默认单分部"""
        return {
            "name": "综合业务",
            "revenue_share": 1.0,
            "net_income_share": 1.0,
            "segment_type": "其他",
            "description": "无法细分业务分部",
        }

    def _parse_json_response(self, response: str, default):
        if not response:
            return default
        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("LLM 返回非 JSON 格式: %s", text[:100])
            return default
