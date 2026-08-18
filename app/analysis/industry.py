"""行业分类学 — 用于分析内容按行业路由"""

import re
from enum import Enum, auto


class IndustryClass(Enum):
    """行业大类枚举"""
    TECH_HARDWARE = auto()       # 半导体、电子、硬件、通信设备
    TECH_SOFTWARE = auto()       # 软件、互联网、AI、SaaS
    FINANCIAL = auto()           # 银行、保险、证券、资管
    ENERGY = auto()              # 石油、天然气、新能源、采矿
    HEALTHCARE = auto()          # 医药、生物技术、医疗设备、医疗服务
    CONSUMER_STAPLES = auto()    # 食品、饮料、零售、日化
    INDUSTRIAL = auto()          # 制造、工业、运输、航空
    UTILITIES = auto()           # 电力、水务、燃气
    REAL_ESTATE = auto()         # 房地产、REITs
    TELECOM = auto()             # 电信运营商
    UNKNOWN = auto()             # 未分类


# 关键词 → 行业大类映射（按优先级排序）
_INDUSTRY_KEYWORDS: list[tuple[list[str], IndustryClass]] = [
    # 金融
    (["银行", "banking", "bank", "保险", "insurance", "证券", "券商",
      "brokerage", "asset management", "wealth management", "信贷",
      "commercial bank", "investment bank", "financial services"], IndustryClass.FINANCIAL),
    # 能源
    (["石油", "天然气", "油气", "能源", "oil", "gas", "energy", "petroleum",
      "coal", "mining", "采矿", "冶炼", "refining", "exploration",
      "renewable energy", "新能源", "solar", "wind"], IndustryClass.ENERGY),
    # 医药
    (["医药", "制药", "生物技术", "biotech", "pharmaceutical", "pharma",
      "healthcare", "医疗", "医疗器械", "medical device", "diagnostics",
      "biotechnology", "clinical", "drug", "疫苗", "生物医药"], IndustryClass.HEALTHCARE),
    # 科技硬件
    (["半导体", "semiconductor", "芯片", "电子", "electronics", "硬件",
      "hardware", "计算机", "computer", "通信设备", "telecom equipment",
      "consumer electronics", "消费电子", "面板", "display"], IndustryClass.TECH_HARDWARE),
    # 科技软件
    (["软件", "software", "互联网", "internet", "云计算", "cloud",
      "AI", "人工智能", "saas", "platform", "social media",
      "e-commerce", "电商", "信息技术", "it services", "data processing",
      "游戏", "gaming"], IndustryClass.TECH_SOFTWARE),
    # 电信
    (["电信", "telecom", "telecommunications", "运营商",
      "mobile network", "5g", "宽带"], IndustryClass.TELECOM),
    # 消费
    (["零售", "retail", "电商", "消费", "consumer", "食品", "food",
      "饮料", "beverage", "日化", "家居", "apparel", "服装",
      "restaurant", "餐饮", "超市", "supermarket", "快消",
      "consumer staples", "consumer discretionary"], IndustryClass.CONSUMER_STAPLES),
    # 工业
    (["制造", "manufacturing", "工业", "industrial", "汽车", "automotive",
      "航空", "aerospace", "defense", "铁路", "railway", "物流",
      "logistics", "transportation", "运输", "machinery", "设备"],
     IndustryClass.INDUSTRIAL),
    # 公用事业
    (["电力", "utility", "utilities", "水务", "water", "燃气",
      "electric", "power generation", "电网"], IndustryClass.UTILITIES),
    # 房地产
    (["房地产", "real estate", "reit", "property", "地产",
      "commercial real estate", "residential"], IndustryClass.REAL_ESTATE),
]


# 行业 → AI 战略分析配置
AI_STRATEGY_CONFIG: dict[IndustryClass, dict] = {
    IndustryClass.TECH_HARDWARE: {
        "role": "资深科技行业分析师",
        "angles": [
            "AI战略投入力度评估",
            "端侧AI vs 云端AI 策略定位",
            "AI功能对换机周期/需求周期的潜在影响",
            "对财务表现的潜在影响（ASP趋势、利润率）",
            "行业竞争格局中的AI差异化定位",
        ],
        "context_note": "2026年消费电子整体需求偏弱，硬件升级成本上升可能抑制换机需求，请在此背景下审慎评估AI功能的换机拉动效应。",
        "fallback_label": "科技硬件企业",
        "fallback_core": "技术创新是核心竞争壁垒，持续研发投入对保持行业领先地位至关重要。",
        "fallback_strategy": "端侧AI（On-Device AI）与云端AI的协同布局日益关键——端侧AI提升响应速度和隐私保护，云端AI提供更强算力，两者的平衡决定产品竞争力。",
    },
    IndustryClass.TECH_SOFTWARE: {
        "role": "资深科技行业分析师",
        "angles": [
            "AI战略投入力度评估",
            "AI能力在产品中的集成深度与应用场景",
            "AI对用户增长和付费转化的潜在影响",
            "对财务表现的潜在影响（利润率、客户生命周期价值）",
            "行业竞争格局中的AI差异化定位",
        ],
        "context_note": "2026年企业级AI应用进入落地验证期，请关注AI功能对公司商业模式的实质性影响而非市场热度。",
        "fallback_label": "科技软件企业",
        "fallback_core": "技术创新是核心竞争壁垒，持续研发投入对保持行业领先地位至关重要。",
        "fallback_strategy": "在AI战略层面，将AI能力深度集成至核心产品、提升用户体验和运营效率是关键路径——AI能力有望通过功能差异化为软件产品创造溢价空间。",
    },
    IndustryClass.FINANCIAL: {
        "role": "资深金融科技分析师",
        "angles": [
            "AI在风控、信用评估、反欺诈领域的应用深度",
            "开放银行与数字化战略推进",
            "AI对运营效率和成本优化的影响",
            "对财务表现的潜在影响（成本收入比、客户获取成本）",
            "金融监管对AI应用的约束与许可",
        ],
        "context_note": "金融行业AI应用受强监管约束，请注重合规性与技术可行性的平衡。",
        "fallback_label": "金融机构",
        "fallback_core": "数字化转型是金融行业提升竞争力的关键路径。",
        "fallback_strategy": "在数字化战略层面，AI驱动的风控体系、智能客服和运营自动化正在重塑金融业的成本结构和客户体验。",
    },
    IndustryClass.ENERGY: {
        "role": "资深能源行业分析师",
        "angles": [
            "数字化技术在勘探/生产/炼化环节的应用",
            "能源转型中的技术布局（碳捕集、氢能等）",
            "AI对运营效率和成本优化的影响",
            "对资本支出效率的影响",
            "行业竞争格局中的技术差异化定位",
        ],
        "context_note": "2026年全球能源转型加速但油价波动仍大，请在此背景下审慎评估技术投入的实际回报。",
        "fallback_label": "能源企业",
        "fallback_core": "技术创新是能源企业提升运营效率和实现低碳转型的重要支撑。",
        "fallback_strategy": "在数字化战略层面，AI驱动的勘探数据分析、智能油田/矿山管理和供应链优化正在提升能源行业的运营效率。",
    },
    IndustryClass.HEALTHCARE: {
        "role": "资深医疗健康行业分析师",
        "angles": [
            "AI在药物发现/临床试验中的实际应用进展",
            "AI辅助诊断和医疗信息化布局",
            "AI对研发效率的影响",
            "对财务表现的潜在影响（研发回报率、运营效率）",
            "医疗监管环境对AI应用的约束",
        ],
        "context_note": "2026年AI制药和医疗AI进入临床验证关键期，请注重审慎评估实际进展而非市场预期。",
        "fallback_label": "医疗健康企业",
        "fallback_core": "研发创新是医疗健康企业核心竞争力的基石。",
        "fallback_strategy": "在数字化战略层面，AI驱动的药物发现、临床试验优化和精准医疗正在为行业带来效率突破。",
    },
    IndustryClass.CONSUMER_STAPLES: {
        "role": "资深消费行业分析师",
        "angles": [
            "数字化营销与消费者洞察的AI应用",
            "供应链智能化与库存管理优化",
            "AI对运营效率的影响",
            "对财务表现的潜在影响（毛利率、库存周转）",
            "行业竞争格局变化",
        ],
        "context_note": "2026年消费需求复苏节奏不确定，请关注数字化投入对运营效率的实际提升而非概念性描述。",
        "fallback_label": "消费品企业",
        "fallback_core": "合理的数字化投入有助于提升运营效率和产品竞争力。",
        "fallback_strategy": "在数字化战略层面，AI驱动的消费者洞察、精准营销和供应链优化正在帮助消费品企业提升运营效率和市场响应速度。",
    },
    IndustryClass.INDUSTRIAL: {
        "role": "资深工业行业分析师",
        "angles": [
            "AI在智能制造/工业自动化中的应用",
            "数字化供应链管理",
            "AI对运营效率和成本优化的影响",
            "对资本支出效率的影响",
            "行业竞争格局中的技术差异化定位",
        ],
        "context_note": "2026年全球制造业供应链重构持续，请关注数字化投入对运营韧性的实际提升。",
        "fallback_label": "工业企业",
        "fallback_core": "技术创新是工业企业提升制造效率和产品质量的关键。",
        "fallback_strategy": "在数字化战略层面，AI驱动的智能制造、预测性维护和供应链优化正在提升工业企业的运营效率和竞争力。",
    },
    IndustryClass.UTILITIES: {
        "role": "资深公用事业分析师",
        "angles": [
            "智能电网与数字化运营",
            "AI在需求预测和负荷管理中的应用",
            "AI对运营效率和成本优化的影响",
            "对资本支出效率的影响",
            "监管环境对数字化投入的影响",
        ],
        "context_note": "公用事业AI应用侧重于运营效率提升，请注重实际效果评估。",
        "fallback_label": "公用事业企业",
        "fallback_core": "合理的数字化投入有助于提升运营效率和服务质量。",
        "fallback_strategy": "在数字化战略层面，AI驱动的负荷预测、智能电网管理和运维优化正在提升公用事业的运营效率。",
    },
    IndustryClass.TELECOM: {
        "role": "资深电信行业分析师",
        "angles": [
            "AI在网络运维和客户服务中的应用",
            "5G/6G网络智能化",
            "AI对运营效率和成本优化的影响",
            "对ARPU值和客户流失率的影响",
            "行业竞争格局中的技术差异化定位",
        ],
        "context_note": "2026年电信行业5G投资回报仍承压，请关注AI对运营效率的实际提升。",
        "fallback_label": "电信企业",
        "fallback_core": "技术创新是电信企业提升网络效率和客户体验的关键。",
        "fallback_strategy": "在数字化战略层面，AI驱动的网络优化、智能运维和个性化服务正在提升电信行业的运营效率和客户满意度。",
    },
    IndustryClass.REAL_ESTATE: {
        "role": "资深房地产行业分析师",
        "angles": [
            "数字化在物业管理中的应用",
            "AI在资产管理中的使用",
            "AI对运营效率的影响",
            "对财务表现的潜在影响",
            "行业数字化转型趋势",
        ],
        "context_note": "2026年房地产行业面临周期性调整，请关注数字化投入对运营效率的实际提升。",
        "fallback_label": "房地产企业",
        "fallback_core": "合理的数字化投入有助于提升运营效率和资产管理能力。",
        "fallback_strategy": "在数字化战略层面，AI驱动的物业管理和资产运营优化正在帮助房地产企业提升运营效率。",
    },
    IndustryClass.UNKNOWN: {
        "role": "资深行业分析师",
        "angles": [
            "公司技术投入力度评估",
            "技术投入对业务运营的影响",
            "对财务表现的潜在影响",
            "行业竞争格局中的定位",
        ],
        "context_note": "请根据公司实际业务特征进行分析。",
        "fallback_label": "企业",
        "fallback_core": "合理的研发投入有助于提升运营效率和产品竞争力。",
        "fallback_strategy": "技术创新有助于提升公司的长期竞争力和运营效率。",
    },
}


# 行业 → 供应链分析配置
# 字段说明：
#   china_plus_1_relevance — 是否适用 China+1 替代基地成本分析
#   alt_base_cost_premium — 从中国转向替代基地的净成本溢价估计（如 0.03 = 3%）
#   supplier_concentration_risk — 供应商集中度风险（"高" / "中" / "低"）
SUPPLY_CHAIN_CONFIG: dict[IndustryClass, dict] = {
    IndustryClass.TECH_HARDWARE: {
        "label": "电子/科技行业",
        "has_global_supply_chain": True,
        "risk_factors": ["出口管制", "关税政策", "零部件供应中断"],
        "description": "所在电子/科技行业供应链具有全球化分工特征，地缘政治风险（如出口管制、关税政策）可能对核心零部件供应产生影响。",
        "us_china_decooupling": True,
        "china_plus_1_relevance": True,
        "alt_base_cost_premium": 0.03,
        "supplier_concentration_risk": "高",
    },
    IndustryClass.TECH_SOFTWARE: {
        "label": "软件/互联网行业",
        "has_global_supply_chain": False,
        "risk_factors": ["技术出口管制", "数据跨境监管"],
        "description": "软件/互联网行业的供应链以技术人才和云基础设施为核心，主要风险来自技术出口管制和数据跨境流动监管。",
        "us_china_decooupling": False,
        "china_plus_1_relevance": False,
        "supplier_concentration_risk": "低",
    },
    IndustryClass.FINANCIAL: {
        "label": "金融行业",
        "has_global_supply_chain": False,
        "risk_factors": ["监管合规", "外包服务商集中度", "网络安全"],
        "description": "金融行业的供应链风险主要集中在监管合规复杂性、关键外包服务商集中度和网络安全防护能力。",
        "us_china_decooupling": False,
        "china_plus_1_relevance": False,
        "supplier_concentration_risk": "中",
    },
    IndustryClass.ENERGY: {
        "label": "能源行业",
        "has_global_supply_chain": True,
        "risk_factors": ["原油/天然气价格波动", "地缘供应风险", "运输通道安全"],
        "description": "能源行业供应链受全球大宗商品价格波动和地缘政治事件影响显著，供应安全和运输通道稳定性是关键风险变量。",
        "us_china_decooupling": False,
        "china_plus_1_relevance": True,
        "alt_base_cost_premium": 0.02,
        "supplier_concentration_risk": "中",
    },
    IndustryClass.HEALTHCARE: {
        "label": "医疗健康行业",
        "has_global_supply_chain": True,
        "risk_factors": ["原料药供应", "临床试验外包", "冷链物流"],
        "description": "医疗健康行业供应链涉及原料药/活性成分的全球采购、临床试验外包管理以及温度敏感产品的冷链物流保障。",
        "us_china_decooupling": False,
        "china_plus_1_relevance": True,
        "alt_base_cost_premium": 0.03,
        "supplier_concentration_risk": "高",
    },
    IndustryClass.CONSUMER_STAPLES: {
        "label": "消费品行业",
        "has_global_supply_chain": True,
        "risk_factors": ["原材料价格波动", "物流配送效率", "库存管理"],
        "description": "零售/消费品行业的供应链风险主要集中在物流配送效率、库存管理和原材料价格波动。",
        "us_china_decooupling": False,
        "china_plus_1_relevance": True,
        "alt_base_cost_premium": 0.02,
        "supplier_concentration_risk": "中",
    },
    IndustryClass.INDUSTRIAL: {
        "label": "工业制造行业",
        "has_global_supply_chain": True,
        "risk_factors": ["原材料价格波动", "多级供应商管理", "物流中断"],
        "description": "所在制造业供应链涉及多级供应商管理，需关注原材料价格波动和物流中断风险。",
        "us_china_decooupling": False,
        "china_plus_1_relevance": True,
        "alt_base_cost_premium": 0.03,
        "supplier_concentration_risk": "高",
    },
    IndustryClass.UTILITIES: {
        "label": "公用事业行业",
        "has_global_supply_chain": False,
        "risk_factors": ["燃料价格波动", "环保监管", "基础设施老化"],
        "description": "公用事业供应链以燃料供应和运维物资为主，主要风险来自燃料价格波动和环保监管变化。",
        "us_china_decooupling": False,
        "china_plus_1_relevance": False,
        "supplier_concentration_risk": "低",
    },
    IndustryClass.TELECOM: {
        "label": "电信行业",
        "has_global_supply_chain": True,
        "risk_factors": ["设备供应集中度", "技术标准更替", "频谱监管"],
        "description": "电信行业供应链以网络设备为核心，设备供应商集中度和技术标准迭代是关键风险。",
        "us_china_decooupling": False,
        "china_plus_1_relevance": True,
        "alt_base_cost_premium": 0.02,
        "supplier_concentration_risk": "高",
    },
    IndustryClass.REAL_ESTATE: {
        "label": "房地产行业",
        "has_global_supply_chain": False,
        "risk_factors": ["融资环境", "调控政策", "原材料成本"],
        "description": "房地产行业的供应链风险主要集中在融资环境变化、调控政策和建筑材料成本波动。",
        "us_china_decooupling": False,
        "china_plus_1_relevance": False,
        "supplier_concentration_risk": "低",
    },
    IndustryClass.UNKNOWN: {
        "label": None,
        "has_global_supply_chain": False,
        "risk_factors": ["原材料供应稳定性", "地缘政策变化"],
        "description": None,
        "us_china_decooupling": False,
        "china_plus_1_relevance": False,
        "supplier_concentration_risk": "低",
    },
}


# China+1 替代制造基地成本参考数据
# 来源：BCG 制造业成本竞争力指数、德勤全球制造业竞争力指数、高盛中国+1 研报
# 所有数值为相对于中国制造业基线的差值
#   正值 = 更优（成本更低/节省），负值 = 更差（成本更高/损失）
CHINA_PLUS_1_COST_BASES: dict[str, dict] = {
    "越南": {
        "labor_cost_advantage": 0.55,        # 人工成本约低 55%
        "efficiency_loss": -0.12,            # 效率/良率损失约 12%
        "logistics_cost_premium": -0.02,     # 物流成本高 2%
        "tariff_saving": 0.22,               # 对美关税节省约 22pp
        "transition_time_years": 2.0,        # 产能建设约需 2 年
        "description": "最成熟的替代基地，电子/纺织产业集群完善，但土地和人工成本正在快速上升",
    },
    "印度": {
        "labor_cost_advantage": 0.65,
        "efficiency_loss": -0.20,
        "logistics_cost_premium": -0.05,
        "tariff_saving": 0.22,
        "transition_time_years": 3.0,
        "description": "市场规模大、劳动力充足，但基础设施和供应链配套仍需时间完善",
    },
    "墨西哥": {
        "labor_cost_advantage": 0.30,
        "efficiency_loss": -0.08,
        "logistics_cost_premium": 0.01,       # 近岸外包物流成本略低
        "tariff_saving": 0.22,
        "transition_time_years": 1.5,
        "description": "近岸外包首选，USMCA 框架下关税优势明显，适合面向北美市场的产能转移",
    },
    "泰国": {
        "labor_cost_advantage": 0.45,
        "efficiency_loss": -0.10,
        "logistics_cost_premium": -0.03,
        "tariff_saving": 0.20,
        "transition_time_years": 2.0,
        "description": "电子/汽车供应链基础较好，日资企业产能转移主要目的地之一",
    },
}


# 供应链中断情景定义
# 每种情景：概率、描述、对中国敞口的毛利率影响系数、对营收的冲击系数
SUPPLY_CHAIN_SCENARIOS: list[dict] = [
    {
        "name": "温和 decoupling",
        "probability": 0.55,
        "description": "关税维持 25%，技术出口管制有限扩大，企业通过 China+1 逐步分散产能",
        "gm_coefficient": 0.3,       # 中国敞口的 30% 转化为毛利率冲击
        "revenue_impact_coefficient": 0.05,
    },
    {
        "name": "局部 tech ban",
        "probability": 0.25,
        "description": "美国扩大实体清单，限制先进制程芯片/设备对华出口，部分技术脱钩",
        "gm_coefficient": 0.6,
        "revenue_impact_coefficient": 0.12,
    },
    {
        "name": "全面脱钩",
        "probability": 0.20,
        "description": "中美科技完全脱钩，供应链被迫重组，中国业务大幅收缩",
        "gm_coefficient": 1.0,
        "revenue_impact_coefficient": 0.25,
    },
]


# 行业默认毛利率和ROE阈值（用于财务叙事分析）
# 返回 {"gross_margin": [low, mid, high], "roe": [low, mid, high]}
# low = 低于此值视为"偏低"，mid = 低于此值视为"中等"，high = 高于此值视为"优秀"
INDUSTRY_THRESHOLDS: dict[IndustryClass, dict[str, list[float]]] = {
    IndustryClass.TECH_HARDWARE: {
        "gross_margin": [0.25, 0.40, 0.55],
        "roe": [0.08, 0.15, 0.20],
        "net_margin": [0.05, 0.10, 0.15],
    },
    IndustryClass.TECH_SOFTWARE: {
        "gross_margin": [0.50, 0.65, 0.75],
        "roe": [0.10, 0.18, 0.25],
        "net_margin": [0.10, 0.18, 0.25],
    },
    IndustryClass.FINANCIAL: {
        "gross_margin": [None, None, None],  # 银行无毛利率概念
        "roe": [0.08, 0.12, 0.15],
        "net_margin": [0.15, 0.25, 0.35],
    },
    IndustryClass.ENERGY: {
        "gross_margin": [0.15, 0.30, 0.45],
        "roe": [0.06, 0.12, 0.18],
        "net_margin": [0.03, 0.08, 0.15],
    },
    IndustryClass.HEALTHCARE: {
        "gross_margin": [0.40, 0.55, 0.70],
        "roe": [0.08, 0.15, 0.20],
        "net_margin": [0.08, 0.15, 0.22],
    },
    IndustryClass.CONSUMER_STAPLES: {
        "gross_margin": [0.20, 0.35, 0.50],
        "roe": [0.10, 0.18, 0.25],
        "net_margin": [0.03, 0.08, 0.15],
    },
    IndustryClass.INDUSTRIAL: {
        "gross_margin": [0.15, 0.25, 0.40],
        "roe": [0.08, 0.15, 0.20],
        "net_margin": [0.04, 0.08, 0.12],
    },
    IndustryClass.UTILITIES: {
        "gross_margin": [0.30, 0.45, 0.55],
        "roe": [0.06, 0.10, 0.14],
        "net_margin": [0.06, 0.12, 0.18],
    },
    IndustryClass.TELECOM: {
        "gross_margin": [0.40, 0.55, 0.65],
        "roe": [0.08, 0.12, 0.16],
        "net_margin": [0.06, 0.12, 0.18],
    },
    IndustryClass.REAL_ESTATE: {
        "gross_margin": [0.25, 0.40, 0.55],
        "roe": [0.06, 0.10, 0.15],
        "net_margin": [0.10, 0.20, 0.30],
    },
    IndustryClass.UNKNOWN: {
        "gross_margin": [0.15, 0.30, 0.45],
        "roe": [0.06, 0.10, 0.15],
        "net_margin": [0.03, 0.08, 0.15],
    },
}


def _keyword_in_text(keyword: str, text: str) -> bool:
    """关键词匹配: ASCII 词用 \\b 单词边界, CJK/多词用子串匹配"""
    if keyword.isascii() and " " not in keyword:
        return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text))
    return keyword in text


def classify_industry(industry: str, sector: str = "", business_desc: str = "") -> IndustryClass:
    """根据行业/部门/业务描述判断行业大类

    匹配优先级：
    1. 仅匹配 industry + sector（高置信度，避免业务描述中的通用词造成误判）
    2. 扩展至 business_desc（低置信度，仅在前一步未匹配时启用）
    """
    # 优先：仅在 industry 和 sector 中匹配
    ctx_primary = (f"{industry} {sector}").lower()
    for keywords, cls in _INDUSTRY_KEYWORDS:
        if any(_keyword_in_text(kw.lower(), ctx_primary) for kw in keywords):
            return cls

    # 回退：加入 business_desc 扩大匹配（注意通用词可能造成误判）
    ctx_full = (f"{ctx_primary} {business_desc[:300]}").lower()
    for keywords, cls in _INDUSTRY_KEYWORDS:
        if any(_keyword_in_text(kw.lower(), ctx_full) for kw in keywords):
            return cls

    return IndustryClass.UNKNOWN


def get_ai_config(industry_cls: IndustryClass) -> dict:
    """获取行业对应的 AI 分析配置"""
    return AI_STRATEGY_CONFIG.get(industry_cls, AI_STRATEGY_CONFIG[IndustryClass.UNKNOWN])


def get_supply_chain_config(industry_cls: IndustryClass) -> dict:
    """获取行业对应的供应链分析配置"""
    return SUPPLY_CHAIN_CONFIG.get(industry_cls, SUPPLY_CHAIN_CONFIG[IndustryClass.UNKNOWN])


def get_thresholds(industry_cls: IndustryClass, metric: str) -> list[float]:
    """获取行业特定指标的阈值 [low, mid, high]"""
    thresholds = INDUSTRY_THRESHOLDS.get(industry_cls, INDUSTRY_THRESHOLDS[IndustryClass.UNKNOWN])
    return thresholds.get(metric, [0.15, 0.30, 0.45])


def is_financial(industry_cls: IndustryClass) -> bool:
    """判断是否为金融行业"""
    return industry_cls == IndustryClass.FINANCIAL


def has_gross_margin(industry_cls: IndustryClass) -> bool:
    """判断行业是否适用毛利率指标"""
    th = get_thresholds(industry_cls, "gross_margin")
    return th[0] is not None


# 市场配置映射：根据公司国家/地区返回需要获取的市场列表
# 格式: [(market_key, label), ...]
MARKET_CONFIG_BY_COUNTRY: dict[str, list[tuple[str, str]]] = {
    "US": [("global", "全球"), ("us", "美国"), ("china", "中国")],
    "USA": [("global", "全球"), ("us", "美国"), ("china", "中国")],
    "UNITED STATES": [("global", "全球"), ("us", "美国"), ("china", "中国")],
    "CN": [("global", "全球"), ("china", "中国"), ("asia", "亚洲")],
    "CHN": [("global", "全球"), ("china", "中国"), ("asia", "亚洲")],
    "HK": [("global", "全球"), ("china", "中国"), ("asia", "亚洲")],
    "JP": [("global", "全球"), ("japan", "日本"), ("asia", "亚洲")],
    "JPN": [("global", "全球"), ("japan", "日本"), ("asia", "亚洲")],
    "GB": [("global", "全球"), ("europe", "欧洲"), ("us", "美国")],
    "UK": [("global", "全球"), ("europe", "欧洲"), ("us", "美国")],
    "DE": [("global", "全球"), ("europe", "欧洲"), ("us", "美国")],
    "DEU": [("global", "全球"), ("europe", "欧洲"), ("us", "美国")],
    "FR": [("global", "全球"), ("europe", "欧洲"), ("us", "美国")],
    "FRA": [("global", "全球"), ("europe", "欧洲"), ("us", "美国")],
    "KR": [("global", "全球"), ("korea", "韩国"), ("asia", "亚洲")],
    "TW": [("global", "全球"), ("taiwan", "台湾"), ("asia", "亚洲")],
}

# 默认市场配置（未匹配到国家的公司）
_DEFAULT_MARKET_CONFIG: list[tuple[str, str]] = [("global", "全球")]


# ── 行业平均估值倍数（用于相对估值） ──
# 各指标: PE, PB, PS, EV_EBITDA
INDUSTRY_AVG_MULTIPLES: dict[IndustryClass, dict[str, float]] = {
    IndustryClass.TECH_HARDWARE: {"pe": 22.0, "pb": 4.5, "ps": 3.5, "ev_ebitda": 14.0},
    IndustryClass.TECH_SOFTWARE: {"pe": 28.0, "pb": 6.0, "ps": 6.0, "ev_ebitda": 18.0},
    IndustryClass.FINANCIAL: {"pe": 12.0, "pb": 1.2, "ps": 2.5, "ev_ebitda": None},
    IndustryClass.ENERGY: {"pe": 10.0, "pb": 1.5, "ps": 0.8, "ev_ebitda": 6.0},
    IndustryClass.HEALTHCARE: {"pe": 18.0, "pb": 3.5, "ps": 3.0, "ev_ebitda": 13.0},
    IndustryClass.CONSUMER_STAPLES: {"pe": 20.0, "pb": 4.0, "ps": 1.8, "ev_ebitda": 12.0},
    IndustryClass.INDUSTRIAL: {"pe": 16.0, "pb": 2.5, "ps": 1.2, "ev_ebitda": 10.0},
    IndustryClass.UTILITIES: {"pe": 15.0, "pb": 1.5, "ps": 1.5, "ev_ebitda": 9.0, "fcf_yield": 0.045},
    IndustryClass.TELECOM: {"pe": 14.0, "pb": 1.3, "ps": 1.0, "ev_ebitda": 7.0},
    IndustryClass.REAL_ESTATE: {"pe": 18.0, "pb": 1.1, "ps": 4.0, "ev_ebitda": 15.0},
    IndustryClass.UNKNOWN: {"pe": 16.0, "pb": 2.5, "ps": 2.0, "ev_ebitda": 11.0},
}


def get_avg_multiples(industry_cls: IndustryClass) -> dict[str, float]:
    """获取行业平均估值倍数"""
    return INDUSTRY_AVG_MULTIPLES.get(industry_cls, INDUSTRY_AVG_MULTIPLES[IndustryClass.UNKNOWN])


# ── 行业感知竞争对比维度 ──
# 各行业 5 个定制化对比维度
COMPARISON_DIMENSIONS_BY_INDUSTRY: dict[IndustryClass, list[str]] = {
    IndustryClass.TECH_HARDWARE: [
        "市场份额", "产品矩阵", "技术壁垒", "研发投入", "国际化程度",
    ],
    IndustryClass.TECH_SOFTWARE: [
        "市场份额", "产品生态", "技术壁垒", "研发投入", "用户规模",
    ],
    IndustryClass.FINANCIAL: [
        "市场份额", "资本充足率", "净息差", "不良贷款率", "财富管理能力",
    ],
    IndustryClass.ENERGY: [
        "市场份额", "资源储量", "运营效率", "成本控制", "能源转型布局",
    ],
    IndustryClass.HEALTHCARE: [
        "市场份额", "研发管线", "品牌信誉", "研发投入", "国际化程度",
    ],
    IndustryClass.CONSUMER_STAPLES: [
        "市场份额", "品牌矩阵", "渠道覆盖", "成本控制", "品类创新",
    ],
    IndustryClass.INDUSTRIAL: [
        "市场份额", "技术壁垒", "运营效率", "成本控制", "客户粘性",
    ],
    IndustryClass.UTILITIES: [
        "市场份额", "运营效率", "资本回报", "监管关系", "新能源布局",
    ],
    IndustryClass.TELECOM: [
        "市场份额", "网络覆盖", "用户ARPU", "频谱资源", "5G布局",
    ],
    IndustryClass.REAL_ESTATE: [
        "市场份额", "土地储备", "品牌信誉", "融资成本", "运营效率",
    ],
    IndustryClass.UNKNOWN: [
        "市场份额", "产品矩阵", "品牌定位", "研发投入", "国际化程度",
    ],
}


def get_comparison_dimensions(industry_cls: IndustryClass) -> list[str]:
    """获取行业对应的竞争对比维度"""
    return COMPARISON_DIMENSIONS_BY_INDUSTRY.get(
        industry_cls, COMPARISON_DIMENSIONS_BY_INDUSTRY[IndustryClass.UNKNOWN]
    )


def get_market_config(country: str) -> list[tuple[str, str]]:
    """根据公司国家返回市场配置"""
    key = country.strip().upper() if country else ""
    return MARKET_CONFIG_BY_COUNTRY.get(key, _DEFAULT_MARKET_CONFIG)


# ══════════════════════════════════════════════════════════════════════
# 行业适配器 — 按行业类型动态调整分析重心（Phase 5.2）
# 纯配置类，无 LLM 依赖。每个行业大类映射到四个分析类型之一，
# 返回对应的分析关键词、关键问题、分析权重。
# 迁移自 backend/analysis/sector_adapter.py（SectorAdapter 类）。
# ══════════════════════════════════════════════════════════════════════


class SectorAdapter:
    """行业适配器 — 按行业类型返回分析重心配置"""

    # 行业大类 → 分析类型映射
    SECTOR_MAP = {
        IndustryClass.ENERGY: "cyclical",
        IndustryClass.INDUSTRIAL: "cyclical",
        IndustryClass.CONSUMER_STAPLES: "consumer",
        IndustryClass.TECH_HARDWARE: "tech_growth",
        IndustryClass.TECH_SOFTWARE: "tech_growth",
        IndustryClass.HEALTHCARE: "tech_growth",
        IndustryClass.TELECOM: "tech_growth",
        IndustryClass.FINANCIAL: "financial_utilities",
        IndustryClass.UTILITIES: "financial_utilities",
        IndustryClass.REAL_ESTATE: "asset_heavy",
    }

    # 各分析类型的适配配置
    ADAPTER_CONFIG = {
        "asset_heavy": {
            "focus_keywords": [
                "资产周转率", "融资成本", "估值折价",
                "现金流覆盖", "NAV折价率",
            ],
            "key_question": "当前资产是'优质生息资产'(可穿越周期)还是'高杠杆资产'(依赖低利率)？",
            "analysis_weight": {
                "capital": 0.4,
                "cycle": 0.3,
                "cost": 0.3,
            },
        },
        "cyclical": {
            "focus_keywords": [
                "库存周期", "供需错配", "产能出清", "供应链溢价", "地缘溢价",
            ],
            "key_question": "当前利润是价格暴涨（不可持续）还是产能出清（可持续）？",
            "analysis_weight": {
                "management": 0.2,
                "cycle": 0.5,
                "cost": 0.3,
            },
        },
        "consumer": {
            "focus_keywords": [
                "品牌溢价", "复购率", "流量成本", "消费者信心",
            ],
            "key_question": "该品牌是情绪价值载体（抗周期）还是可替代快消品（随周期）？",
            "analysis_weight": {
                "brand": 0.4,
                "channel": 0.3,
                "cost": 0.3,
            },
        },
        "tech_growth": {
            "focus_keywords": [
                "渗透率", "商业化落地", "AI变现",
                "烧钱期vs收割期", "商业模式韧性",
            ],
            "key_question": "技术处于烧钱期（负现金流）还是收割期（正现金流）？",
            "analysis_weight": {
                "rd": 0.3,
                "monetization": 0.4,
                "competition": 0.3,
            },
        },
        "financial_utilities": {
            "focus_keywords": [
                "风险敞口", "监管博弈", "资产负债表", "特许经营权",
            ],
            "key_question": "高利率环境下资产负债表是否健康？",
            "analysis_weight": {
                "risk": 0.4,
                "regulation": 0.3,
                "capital": 0.3,
            },
        },
    }

    @classmethod
    def get_config(cls, industry_cls: IndustryClass) -> dict:
        """获取行业适配配置

        Args:
            industry_cls: 行业大类枚举值

        Returns:
            {
                "sector_type": str,      # 分析类型名称
                "focus_keywords": list,  # 分析关键词
                "key_question": str,     # 核心问题
                "analysis_weight": dict, # 权重分配
            }
        """
        sector_type = cls.SECTOR_MAP.get(industry_cls, "tech_growth")
        config = cls.ADAPTER_CONFIG.get(sector_type, cls.ADAPTER_CONFIG["tech_growth"])
        return {
            "sector_type": sector_type,
            **config,
        }
