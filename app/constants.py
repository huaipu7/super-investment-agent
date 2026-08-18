"""
金融常量 —— 从 Research-Agent(config/constants.py) 迁移。

所有财务指标标准化代码、行业分类、比率、单位转换、日期格式统一在此定义。
"""
# 货币代码
CURRENCY_CODES = {
    "CNY": "人民币",
    "USD": "美元",
    "HKD": "港元",
    "EUR": "欧元",
    "GBP": "英镑",
    "JPY": "日元",
}

# 财务指标标准化名称
FINANCIAL_METRICS = {
    # 利润表
    "REVENUE": "营业收入",
    "COST_OF_REVENUE": "营业成本",
    "GROSS_PROFIT": "毛利润",
    "OPERATING_EXPENSE": "营业费用",
    "OPERATING_INCOME": "营业利润",
    "NET_INCOME": "净利润",
    "EBITDA": "息税折旧摊销前利润",
    "INTEREST_EXPENSE": "利息支出",
    "TAX_PROVISION": "所得税费用",
    "RESEARCH_DEV": "研发费用",
    # 资产负债表
    "TOTAL_ASSETS": "总资产",
    "TOTAL_LIABILITIES": "总负债",
    "TOTAL_EQUITY": "股东权益",
    "CURRENT_ASSETS": "流动资产",
    "CURRENT_LIABILITIES": "流动负债",
    "CASH_AND_EQUIVALENTS": "现金及现金等价物",
    "ACCOUNTS_RECEIVABLE": "应收账款",
    "INVENTORY": "存货",
    "FIXED_ASSETS": "固定资产",
    "GOODWILL": "商誉",
    # 现金流量表
    "OPERATING_CASH_FLOW": "经营活动现金流",
    "INVESTING_CASH_FLOW": "投资活动现金流",
    "FINANCING_CASH_FLOW": "筹资活动现金流",
    "FREE_CASH_FLOW": "自由现金流",
    "CAPEX": "资本支出",
    "STOCK_BUYBACK": "股票回购",
}

# 行业分类（申万一级行业）
INDUSTRY_CLASSIFICATION = {
    "银行": "BANKING",
    "非银金融": "NON_BANK_FINANCE",
    "食品饮料": "FOOD_BEVERAGE",
    "医药生物": "PHARMA_BIOTECH",
    "电子": "ELECTRONICS",
    "计算机": "COMPUTER",
    "通信": "TELECOMMUNICATION",
    "电力设备": "ELECTRICAL_EQUIPMENT",
    "汽车": "AUTOMOTIVE",
    "房地产": "REAL_ESTATE",
    "建筑装饰": "BUILDING_DECORATION",
    "基础化工": "BASIC_CHEMICAL",
    "有色金属": "NON_FERROUS_METAL",
    "钢铁": "STEEL",
    "机械设备": "MECHANICAL_EQUIPMENT",
    "国防军工": "NATIONAL_DEFENSE",
    "公用事业": "PUBLIC_UTILITIES",
    "交通运输": "TRANSPORTATION",
    "商贸零售": "COMMERCIAL_RETAIL",
    "社会服务": "SOCIAL_SERVICES",
}

# 财务比率名称
FINANCIAL_RATIOS = {
    "ROE": "净资产收益率",
    "ROA": "总资产收益率",
    "GROSS_MARGIN": "毛利率",
    "NET_MARGIN": "净利率",
    "OPERATING_MARGIN": "营业利润率",
    "CURRENT_RATIO": "流动比率",
    "QUICK_RATIO": "速动比率",
    "DEBT_TO_EQUITY": "资产负债率",
    "PE": "市盈率",
    "PB": "市净率",
    "PS": "市销率",
    "EV_EBITDA": "企业价值/EBITDA",
    "REVENUE_GROWTH": "营收增长率",
    "NET_INCOME_GROWTH": "净利润增长率",
    # 现金流指标
    "OPERATING_CF_MARGIN": "经营现金流利润率",
    "FREE_CASH_FLOW": "自由现金流",
    "FCF_PER_SHARE": "每股自由现金流",
    "CAPEX_TO_REVENUE": "资本支出占比",
    "CASH_CONVERSION_RATIO": "现金流转化率",
    # 研发指标
    "RD_TO_REVENUE": "研发投入占比",
    # 杜邦分解指标
    "ASSET_TURNOVER": "资产周转率",
    "EQUITY_MULTIPLIER": "权益乘数",
    # 资本配置指标
    "NET_DEBT_TO_EQUITY": "净负债权益比",
    "FCF_YIELD": "自由现金流收益率",
    "BUYBACK_YIELD": "回购收益率",
}

# 日期格式模式
DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y年%m月%d日",
    "%Y-%m-%d %H:%M:%S",
    "%Y%m%d",
]

# 单位转换映射
UNIT_MULTIPLIERS = {
    "元": 1,
    "千元": 1000,
    "万元": 10000,
    "亿元": 100000000,
    "亿": 100000000,
    "百万": 1000000,
    "千万": 10000000,
    "十亿": 1000000000,
    "": 1,
}

# ── 合规声明（迁移自 Agent-for-Investment/config.py）─────────────────
# 数据来源按统一数据层实际来源如实标注（美股 Yahoo / A股新浪+东方财富 / 基金天天基金）
DATA_SOURCE_DISCLAIMER = (
    "数据来源: Yahoo Finance / 新浪财经 / 东方财富 | 数据可能存在15分钟延迟，不作为高频交易依据"
)
FUND_DATA_SOURCE_DISCLAIMER = "数据来源: 天天基金网/东方财富 | 数据仅供学习参考，不构成投资建议"
RISK_DISCLAIMER = "⚠ 投资有风险，入市需谨慎。本报告基于客观数据生成，不构成任何投资建议。请独立判断并决策。"
