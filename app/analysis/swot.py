"""SWOT 分析器

使用 DeepSeek LLM 结合财务数据和 RAG 检索文档，生成结构化 SWOT 分析。
每项观点标注数据来源。
"""

from typing import Optional

from app.utils.logger import logger
from app.analysis.llm import analysis_llm
from app.analysis.industry import IndustryClass
from app.analysis.master_prompt import build_system_prompt
from app.llm.schemas import SwotResult
from app.llm.structured import parse_llm_json
from app.trust import strict_mode

ROLE_INDUSTRY_RESEARCHER = "你是一位资深行业研究员，擅长从行业格局和竞争态势中识别企业核心竞争力和战略定位。"


SWOT_COT_PROMPT = """请按以下三步推理过程，对目标公司进行 SWOT 分析。

公司名称: {company_name}
行业: {industry}
业务描述: {business_summary}

## 关键财务数据点
{raw_data_points}

## 财务数据摘要
{financial_summary}

## 市场分析摘要
{market_summary}

## 竞品对比摘要
{competitive_summary}

## 相关资讯/文档片段
{rag_context}

{industry_angles}

请按以下推理步骤分析：

### 第一步：现象描述
基于上述关键财务数据点的具体数值和趋势，观察以下方面的变化方向：
- 盈利能力趋势（ROE、毛利率、净利率的升降方向）
- 成长性趋势（营收增长、利润增长的加速或放缓）
- 财务健康度（杠杆水平、流动性变化）
- 现金流质量（经营现金流、自由现金流充裕度）

### 第二步：归因分析
结合市场分析、竞品对比和业务描述，解释上述现象背后的驱动因素：
- 成本/收入结构变化对利润率的影响
- 产品/业务结构调整对增长的影响
- 行业竞争格局对公司定位的影响
- 外部环境（政策、技术、地缘）的影响

### 第三步：横向对比与评估
综合以上分析，输出结构化 SWOT 评估。每项优劣势和机会威胁必须有具体数据来源引用。

输出 JSON 格式：
{{
    "swot": {{
        "strengths": [
            {{"point": "具体优势描述（引用财务或市场数据）", "source": "数据来源"}}
        ],
        "weaknesses": [
            {{"point": "具体劣势描述（引用财务或市场数据）", "source": "数据来源"}}
        ],
        "opportunities": [
            {{"point": "具体机会描述", "source": "数据来源"}}
        ],
        "threats": [
            {{"point": "具体威胁描述", "source": "数据来源"}}
        ]
    }},
    "summary": "综合评估（100 字以内）",
    "rating": "看好|中性|谨慎"
}}

要求：
1. 每类至少 2 项，最多 5 项
2. 观点尽量落到上下文已有数据上；source 只能写上下文中出现过的来源名，不得引用未提供的研报/统计数值（无数据时写"无直接数据"）
3. "rating" 是基于三步推理的客观评级
4. 只返回 JSON，不要额外说明"""


class SwotAnalyzer:
    """SWOT 分析器"""

    # Phase 6: 行业特定 SWOT 分析角度配置
    INDUSTRY_SWOT_ANGLES = {
        IndustryClass.TECH_SOFTWARE: {
            "angles": [
                "技术护城河与专利壁垒",
                "AI/自动化对商业模式的颠覆或重构",
                "用户粘性与网络效应",
                "研发投入转化效率",
                "烧钱期 vs 收割期判断",
            ],
            "risk_focus": ["技术迭代风险", "人才流失风险", "监管（数据安全/算法合规）"],
        },
        IndustryClass.TECH_HARDWARE: {
            "angles": [
                "半导体/硬件周期位置",
                "技术迭代与资本开支强度",
                "供应链地理集中度",
                "中美科技脱钩影响",
                "下游需求（消费电子/数据中心/汽车）可见度",
            ],
            "risk_focus": ["周期性库存调整", "地缘政治风险", "技术替代"],
        },
        IndustryClass.FINANCIAL: {
            "angles": [
                "净息差(NIM)趋势与资产端收益率",
                "不良贷款率与拨备覆盖率",
                "资本充足率与监管约束",
                "财富管理/投行等非息收入占比",
                "利率敏感性与资产负债表久期匹配",
            ],
            "risk_focus": ["信用风险与经济周期", "监管政策变化", "利率风险"],
        },
        IndustryClass.ENERGY: {
            "angles": [
                "大宗商品价格周期位置",
                "储量替换率与产量指引",
                "资本支出纪律与 FCF 分红能力",
                "地缘政治对供应的扰动",
                "能源转型（碳中和）对长期需求的冲击",
            ],
            "risk_focus": ["油价/气价波动", "能源转型政策", "地缘冲突"],
        },
        IndustryClass.HEALTHCARE: {
            "angles": [
                "研发管线（Pipeline）与专利悬崖",
                "集采/医保谈判的政策影响",
                "创新药/器械的审批进度",
                "细分赛道（CXO/创新药/器械/疫苗）景气度",
                "海外市场拓展与 FDA 审批风险",
            ],
            "risk_focus": ["集采降价", "研发失败风险", "监管审批不确定性"],
        },
        IndustryClass.CONSUMER_STAPLES: {
            "angles": [
                "品牌溢价与定价权",
                "渠道渗透与供应链效率",
                "复购率与消费者忠诚度",
                "原材料成本传导能力",
                "消费降级/升级的周期影响",
            ],
            "risk_focus": ["原材料成本波动", "消费者信心变化", "渠道变革"],
        },
        IndustryClass.INDUSTRIAL: {
            "angles": [
                "库存周期位置与产能利用率",
                "订单可见度与 backlog 变化",
                "成本传导能力（原材料/能源/人工）",
                "中国+1 供应链转移影响",
                "制造业回流与政策刺激",
            ],
            "risk_focus": ["经济周期下行", "原材料成本", "供应链中断"],
        },
        IndustryClass.TELECOM: {
            "angles": [
                "5G/6G 资本开支节奏与回报",
                "用户 ARPU 值与流量增长",
                "竞争格局（传统运营商 vs 新兴势力）",
                "数字基础设施的长期价值",
                "监管（频谱分配/网间结算）",
            ],
            "risk_focus": ["资本开支回报周期", "监管政策变化", "竞争加剧"],
        },
        IndustryClass.UTILITIES: {
            "angles": [
                "监管回报率（RAB/ROE 上限）",
                "可再生能源转型 Capex 计划",
                "分红稳定性与现金流覆盖",
                "利率敏感性与负债成本",
                "电价/气价管制与市场化改革",
            ],
            "risk_focus": ["利率上行", "监管回报率下调", "新能源投资风险"],
        },
        IndustryClass.REAL_ESTATE: {
            "angles": [
                "NAV（净资产价值）与折价率",
                "租金增长与出租率趋势",
                "利率敏感性与融资成本",
                "资产周转率与开发周期",
                "REITs 分红率 vs 无风险利率利差",
            ],
            "risk_focus": ["利率风险", "物业估值下行", "流动性风险"],
        },
    }

    def __init__(self):
        self._client = analysis_llm

    def build_raw_data_points(self, financial_analysis: dict) -> str:
        """从财务分析结果中提取多期关键数据点及趋势

        Args:
            financial_analysis: FinancialAnalyzer.analyze() 的输出
                {"ratios": {code: [{"period": str, "value": float}], ...},
                 "growth_rates": {...}, "fcf_history": [float, ...]}

        Returns:
            结构化的多行文本，形如：
            "ROE: 2024: 15.2%, 2023: 14.8%, 2022: 12.1% — 呈上升趋势 ↑"
        """
        parts = []
        ratios = financial_analysis.get("ratios", {})
        growth_rates = financial_analysis.get("growth_rates", {})
        fcf_history = financial_analysis.get("fcf_history", [])

        # 盈利能力
        for code, label in [("ROE", "ROE"), ("GROSS_MARGIN", "毛利率"),
                             ("NET_MARGIN", "净利率"), ("OPERATING_MARGIN", "营业利润率")]:
            series = ratios.get(code, [])
            values = [(s["period"], s["value"]) for s in series if s["value"] is not None]
            if values:
                vals_str = ", ".join(f"{p}: {v:.1%}" for p, v in values)
                trend = self._get_trend([v for _, v in values])
                parts.append(f"{label}: {vals_str} — {trend}")

        # 成长性
        for code, label in [("REVENUE_GROWTH", "营收增长率"),
                             ("NET_INCOME_GROWTH", "净利润增长率")]:
            series = growth_rates.get(code, [])
            values = [(s["period"], s["value"]) for s in series if s["value"] is not None]
            if values:
                vals_str = ", ".join(f"{p}: {v:+.1%}" for p, v in values)
                trend = self._get_trend([v for _, v in values])
                parts.append(f"{label}: {vals_str} — {trend}")

        # 财务健康度
        for code, label in [("CURRENT_RATIO", "流动比率"),
                             ("DEBT_TO_EQUITY", "资产负债率")]:
            series = ratios.get(code, [])
            values = [(s["period"], s["value"]) for s in series if s["value"] is not None]
            if values:
                vals_str = ", ".join(f"{p}: {v:.2f}" for p, v in values)
                trend = self._get_trend([v for _, v in values])
                parts.append(f"{label}: {vals_str} — {trend}")

        # 现金流
        ocf_series = ratios.get("OPERATING_CF_MARGIN", [])
        ocf_values = [(s["period"], s["value"]) for s in ocf_series if s["value"] is not None]
        if ocf_values:
            vals_str = ", ".join(f"{p}: {v:.1%}" for p, v in ocf_values)
            trend = self._get_trend([v for _, v in ocf_values])
            parts.append(f"经营现金流/营收比率: {vals_str} — {trend}")

        if fcf_history:
            parts.append(f"最近一期自由现金流: {fcf_history[-1]:,.0f}")
            if len(fcf_history) >= 2:
                parts.append(f"自由现金流趋势: {'增长 ↑' if fcf_history[-1] > fcf_history[0] else '下降 ↓'}")

        # 研发投入
        rd_series = ratios.get("RD_TO_REVENUE", [])
        rd_values = [(s["period"], s["value"]) for s in rd_series if s["value"] is not None]
        if rd_values:
            latest_rd = rd_values[-1][1]
            level = "极高" if latest_rd > 0.15 else "较高" if latest_rd > 0.08 else "中等" if latest_rd > 0.03 else "较低"
            vals_str = ", ".join(f"{p}: {v:.1%}" for p, v in rd_values)
            parts.append(f"研发投入占比: {vals_str} — 研发强度{level}")

        return "\n".join(parts) if parts else "暂无关键财务数据点"

    def analyze(
        self,
        company_name: str,
        industry: str = "",
        business_summary: str = "",
        financial_summary: str = "",
        market_summary: str = "",
        competitive_summary: str = "",
        rag_context: str = "",
        financial_analysis: dict = None,
        industry_cls: IndustryClass = None,
        macro_line: str = "",
    ) -> dict:
        """执行 SWOT 分析

        Args:
            company_name: 公司名称
            industry: 行业
            business_summary: 业务描述
            financial_summary: 财务数据摘要
            market_summary: 市场分析摘要
            competitive_summary: 竞品对比摘要
            rag_context: RAG 检索到的相关文档片段
            industry_cls: 行业分类（Phase 6，用于注入行业特定分析角度）

        Returns:
            {
                "swot": {
                    "strengths": [{"point": str, "source": str}],
                    "weaknesses": [...],
                    "opportunities": [...],
                    "threats": [...],
                },
                "summary": str,
                "rating": str,
            }
        """
        if not company_name:
            logger.warning("缺少公司名，无法生成 SWOT")
            return self._empty_swot()

        # 构建关键数据点（如提供）
        raw_data_points = ""
        if financial_analysis:
            raw_data_points = self.build_raw_data_points(financial_analysis)
            if raw_data_points:
                logger.info("提取关键数据点用于 SWOT 分析")

        # Phase 6: 行业特定分析角度
        industry_angles = ""
        if industry_cls is not None and industry_cls in self.INDUSTRY_SWOT_ANGLES:
            cfg = self.INDUSTRY_SWOT_ANGLES[industry_cls]
            angles_text = "；".join(cfg["angles"])
            risk_text = "、".join(cfg["risk_focus"])
            industry_angles = f"""
【行业特定分析角度（{industry_cls.value}）】
在 SWOT 分析中，请重点从以下维度展开：
{angles_text}

风险焦点：{risk_text}
"""
            logger.info("注入行业特定 SWOT 分析角度: %s", industry_cls.value)

        prompt = SWOT_COT_PROMPT.format(
            company_name=company_name,
            industry=industry or "未知",
            business_summary=(business_summary or "暂无")[:500],
            raw_data_points=raw_data_points or "暂无关键财务数据点",
            financial_summary=financial_summary or "暂无财务数据",
            market_summary=market_summary or "暂无市场分析数据",
            competitive_summary=competitive_summary or "暂无竞品分析数据",
            rag_context=rag_context or "无相关文档片段",
            industry_angles=industry_angles,
        )

        try:
            system_prompt = build_system_prompt(ROLE_INDUSTRY_RESEARCHER, macro_line)
            response = self._client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=2048,
                system_prompt=system_prompt,
            )
            result = parse_llm_json(response, model=SwotResult, default={},
                                    context="SWOT")
            if not isinstance(result, dict) or "swot" not in result:
                return self._empty_swot()

            logger.info("SWOT 分析完成: %s, 评级=%s",
                        company_name, result.get("rating", "未知"))
            return result

        except Exception as e:
            logger.error("SWOT 分析失败: %s", e)
            return self._empty_swot()

    def build_market_summary(self, market_result: dict) -> str:
        """从市场分析结果构建摘要"""
        parts = []
        company = market_result.get("company", {})
        if company.get("revenue"):
            parts.append(f"最新营收: {company['revenue']:,.2f}")

        conc = market_result.get("industry_concentration", {})
        # 无行业总规模时份额只是样本内占比，不能以「市场份额」的名义喂给模型
        has_conc = conc.get("cr3") is not None
        share = market_result.get("market_share")
        if share is not None:
            parts.append(f"{'市场份额' if has_conc else '已知同业样本内份额'}: {share:.1%}")
        if conc.get("cr3") is not None:
            parts.append(f"CR3: {conc['cr3']:.1%}")
        if conc.get("hhi") is not None:
            parts.append(f"HHI: {conc['hhi']:.0f}")

        tam = market_result.get("tam_estimate")
        if tam:
            parts.append(f"估算 TAM: {tam:,.0f}")

        competitors = market_result.get("competitors", [])
        if competitors:
            parts.append(f"识别竞品: {', '.join(c.get('name', '?')[:8] for c in competitors[:5])}")

        return "；".join(parts) if parts else "暂无市场数据"

    def build_rag_context(
        self,
        retriever,
        query: str,
        max_docs: int = 5,
    ) -> str:
        """从 RAG 检索器获取相关文档片段"""
        if retriever is None:
            return ""

        try:
            docs = retriever.invoke(query)
            if not docs:
                return ""

            parts = []
            for i, doc in enumerate(docs[:max_docs]):
                score = doc.metadata.get("score", 0)
                section = doc.metadata.get("section", "")
                source = doc.metadata.get("source", "")
                date = doc.metadata.get("date", "")
                snippet = doc.page_content[:200].strip()
                src = source or ""
                if date:
                    src = f"{src}, {date}" if src else f"日期 {date}"
                parts.append(
                    f"[片段 {i+1}]"
                    f"{'(' + section + ') ' if section else ''}"
                    f"{snippet}"
                    f"{' (来源: ' + src + ')' if src else ''}"
                    f"{' [相关度: ' + f'{score:.2f}' + ']' if score else ''}"
                )

            return "\n\n".join(parts)
        except Exception as e:
            logger.warning("RAG 检索失败: %s", e)
            return ""

    # ── 内部方法 ──────────────────────────────────────────────

    @staticmethod
    def _get_trend(values: list[float]) -> str:
        """判断数值序列的趋势方向"""
        if len(values) < 2:
            return "数据点不足"
        if values[-1] > values[0] * 1.02:
            return "呈上升趋势 ↑"
        elif values[-1] < values[0] * 0.98:
            return "呈下降趋势 ↓"
        else:
            return "基本稳定 →"

    def _empty_swot(self) -> dict:
        """LLM 失败时的空壳。评级宁可留空，也不冒充「中性」。

        给出「中性」等于告诉读者「我们评过了，结论是中性的」——这是编造。
        """
        return {
            "swot": {
                "strengths": [],
                "weaknesses": [],
                "opportunities": [],
                "threats": [],
            },
            "summary": "SWOT 分析生成失败",
            "rating": None if strict_mode() else "中性",
            "degraded": True,
            "reason": "LLM 未返回有效 SWOT 结构（校验失败或调用异常）",
        }
