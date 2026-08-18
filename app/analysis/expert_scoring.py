"""专家评分引擎 —— 四位大师确定性评分（Violet Z 逐字迁移）。

纯规则、无 LLM。输入为 A 股财务指标 dict（字段契约与 akshare_source.get_financial_indicators
完全一致：roe / debt_ratio / gross_margin / net_margin / profit_growth /
revenue_growth / current_ratio / eps_cash_flow / eps / nav_per_share）。

评分逻辑、阈值、文案与 Stock_Assistant/app.py 的 score_buffett / score_munger /
score_duan / score_dalio / get_decision / generate_summary 逐项一致。
"""

from __future__ import annotations

import pandas as pd

from app.data.router import get_market_data, is_ashare
from app.data.sources.akshare_source import normalize_ashare_code


def safe_float(val, default=None):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ──────────────────────────────────────────────
# 专家评分引擎（Violet Z 逐字迁移）
# ──────────────────────────────────────────────

def score_buffett(fin):
    score = 0; details = []
    roe = fin.get('roe')
    if roe is not None:
        if roe >= 20:
            score += 3; details.append(('ROE超过20%，股东回报能力强', 'advantage'))
        elif roe >= 15:
            score += 2; details.append(('ROE超过15%，股东回报良好', 'advantage'))
        elif roe >= 10:
            score += 1; details.append(('ROE超过10%，尚可接受', 'advantage'))
        else:
            details.append(('ROE低于10%，股东回报能力不足', 'risk'))
    debt = fin.get('debt_ratio')
    if debt is not None:
        if debt < 30:
            score += 3; details.append(('资产负债率低于30%，财务结构稳健', 'advantage'))
        elif debt < 50:
            score += 2; details.append(('资产负债率低于50%，财务风险可控', 'advantage'))
        else:
            details.append(('资产负债率过高，财务杠杆风险大', 'risk'))
    net_margin = fin.get('net_margin')
    if net_margin is not None:
        if net_margin >= 20:
            score += 2; details.append(('净利润率超过20%，定价能力强', 'advantage'))
        elif net_margin >= 10:
            score += 1; details.append(('净利润率超过10%，盈利能力尚可', 'advantage'))
        else:
            details.append(('净利润率低于10%，盈利能力偏弱', 'risk'))
    cf = fin.get('eps_cash_flow')
    if cf is not None and cf > 0:
        score += 2; details.append(('自由现金流为正，企业能持续产生现金', 'advantage'))
    else:
        details.append(('现金流为负，需关注现金流状况', 'risk'))
    adv = [d[0] for d in details if d[1] == 'advantage']
    risks = [d[0] for d in details if d[1] == 'risk']
    return min(score, 10), adv, risks


def score_munger(fin):
    score = 0; details = []
    roe = fin.get('roe')
    if roe is not None:
        if roe >= 20:
            score += 3; details.append(('ROE超过20%，资本回报效率卓越', 'advantage'))
        elif roe >= 15:
            score += 2; details.append(('ROE超过15%，资本回报效率良好', 'advantage'))
        elif roe >= 10:
            score += 1; details.append(('ROE超过10%，资本回报尚可', 'advantage'))
        else:
            details.append(('ROE低于10%，资本回报效率不佳', 'risk'))
    gm = fin.get('gross_margin')
    if gm is not None:
        if gm >= 60:
            score += 3; details.append(('毛利率超过60%，护城河深厚', 'advantage'))
        elif gm >= 40:
            score += 2; details.append(('毛利率超过40%，具备竞争优势', 'advantage'))
        else:
            details.append(('毛利率低于40%，竞争优势不够突出', 'risk'))
    debt = fin.get('debt_ratio')
    if debt is not None:
        if debt < 30:
            score += 2; details.append(('资产负债率低于30%，几乎无债务风险', 'advantage'))
        elif debt < 50:
            score += 1; details.append(('资产负债率低于50%，债务风险可控', 'advantage'))
        else:
            details.append(('资产负债率过高，芒格会担忧', 'risk'))
    cf = fin.get('eps_cash_flow')
    if cf is not None and cf > 0:
        score += 2; details.append(('经营性现金流为正，现金产出能力强', 'advantage'))
    else:
        details.append(('现金流为负，需警惕现金消耗', 'risk'))
    adv = [d[0] for d in details if d[1] == 'advantage']
    risks = [d[0] for d in details if d[1] == 'risk']
    return min(score, 10), adv, risks


def score_duan(fin, market_cap=None):
    score = 0; details = []
    gm = fin.get('gross_margin')
    if gm is not None:
        if gm >= 60:
            score += 3; details.append(('毛利率超过60%，生意模式优秀', 'advantage'))
        elif gm >= 40:
            score += 2; details.append(('毛利率超过40%，生意模式良好', 'advantage'))
        else:
            details.append(('毛利率低于40%，生意模式不够理想', 'risk'))
    growth = fin.get('profit_growth')
    if growth is not None:
        if growth >= 20:
            score += 3; details.append(('净利润增长率超过20%，成长性突出', 'advantage'))
        elif growth >= 10:
            score += 2; details.append(('净利润增长率超过10%，成长性良好', 'advantage'))
        elif growth >= 0:
            details.append(('净利润增长停滞，需关注', 'risk'))
        else:
            details.append(('净利润负增长，段永平不会考虑', 'risk'))
    debt = fin.get('debt_ratio')
    if debt is not None:
        if debt < 30:
            score += 2; details.append(('资产负债率低于30%，财务非常健康', 'advantage'))
        elif debt < 50:
            score += 1; details.append(('资产负债率低于50%，债务尚可', 'advantage'))
        else:
            details.append(('高负债率是段永平一票否决的信号', 'risk'))
    if market_cap is not None:
        mcap_yi = market_cap / 1e8
        if mcap_yi >= 1000:
            score += 2; details.append(('总市值超千亿，行业龙头地位稳固', 'advantage'))
        elif mcap_yi >= 100:
            score += 1; details.append(('总市值超百亿，有一定规模', 'advantage'))
        else:
            details.append(('市值规模较小，抗风险能力有限', 'risk'))
    adv = [d[0] for d in details if d[1] == 'advantage']
    risks = [d[0] for d in details if d[1] == 'risk']
    return min(score, 10), adv, risks


def score_dalio(fin):
    score = 0; details = []
    cr = fin.get('current_ratio')
    if cr is not None:
        if cr >= 2.0:
            score += 3; details.append(('流动比率超过2.0，短期偿债能力优秀', 'advantage'))
        elif cr >= 1.5:
            score += 2; details.append(('流动比率超过1.5，短期流动性良好', 'advantage'))
        elif cr >= 1.0:
            score += 1; details.append(('流动比率超过1.0，流动性尚可', 'advantage'))
        else:
            details.append(('流动比率低于1.0，流动性风险较高', 'risk'))
    debt = fin.get('debt_ratio')
    if debt is not None:
        if debt < 30:
            score += 3; details.append(('资产负债率低于30%，债务周期风险低', 'advantage'))
        elif debt < 50:
            score += 2; details.append(('资产负债率低于50%，债务风险可控', 'advantage'))
        elif debt < 70:
            score += 1; details.append(('资产负债率适中，需关注债务周期', 'advantage'))
        else:
            details.append(('高负债率在债务周期下行时风险极大', 'risk'))
    cf = fin.get('eps_cash_flow')
    if cf is not None and cf > 0:
        score += 2; details.append(('经营性现金流为正，现金流健康', 'advantage'))
    else:
        details.append(('现金流为负，风险平价模型会预警', 'risk'))
    rev = fin.get('revenue_growth')
    if rev is not None:
        if rev > 10:
            score += 2; details.append(('营收增长超过10%，处于扩张周期', 'advantage'))
        elif rev > 0:
            score += 1; details.append(('营收保持增长，处于稳定期', 'advantage'))
        else:
            details.append(('营收下滑，可能处于周期下行阶段', 'risk'))
    adv = [d[0] for d in details if d[1] == 'advantage']
    risks = [d[0] for d in details if d[1] == 'risk']
    return min(score, 10), adv, risks


def get_decision(score):
    if score >= 7: return '做多', '#22C55E'
    elif score >= 4: return '持有', '#F59E0B'
    else: return '做空', '#EF4444'


def generate_summary(name, score, adv, risks):
    base = f'{name}认为'
    if score >= 7:
        return base + '该企业整体质量优秀，核心指标表现强劲，具有良好的投资价值和安全边际，建议重点关注。'
    elif score >= 4:
        return base + '该企业具备部分值得看好的特质，但存在一些需要关注的风险因素，建议继续观察等待更好的介入时机。'
    else:
        return base + '该企业存在较多风险隐患，核心指标表现不佳，在当前估值水平下缺乏足够的安全边际，建议回避。'


# ──────────────────────────────────────────────
# 综合专家分析（经统一数据层）
# ──────────────────────────────────────────────

def run_expert_analysis(ticker: str) -> dict:
    """对 A 股代码做四位专家综合分析。

    数据流（与 Violet Z /api/analysis 一致，但数据经统一数据层）：
      1. 实时行情（Sina，经 MarketData）→ 报价/振幅/涨跌幅/PE/PB/市值/换手率
      2. 财务指标（akshare stock_financial_analysis_indicator）→ 四位专家评分
      3. 行业（东财公司信息）

    Returns:
        {
            "stock": {...},          # 行情 + PE/PB/市值/换手率/振幅
            "experts": [...],        # 四位专家：id/name/icon/score/decision/color/summary/advantages/risks
            "comparison": [...],     # 指标对比（PE/PB/ROE/负债率/毛利率/净利率/增速）
            "industry_name": str,
        }
    """
    if not is_ashare(ticker):
        raise ValueError(f'专家评分仅支持 A 股代码，收到: {ticker}')

    md = get_market_data()
    code = normalize_ashare_code(ticker)

    # 1. 实时行情（Sina spot 已含振幅/换手率/PE/PB/市值）
    spot = md.get_quote(code)
    if not spot or spot.get('price') is None:
        raise ValueError(f'未找到该股票代码: {ticker}')

    price = spot['price']
    pre_close = spot['pre_close']
    high = spot['high']
    low = spot['low']
    amplitude = round((high - low) / pre_close * 100, 2) if pre_close > 0 else 0
    change_pct = (price - pre_close) / pre_close * 100 if pre_close > 0 else 0

    stock = {
        'code': code, 'name': spot.get('name', code),
        'price': price, 'change_pct': round(change_pct, 2),
        'change_amount': round(price - pre_close, 2),
        'volume': spot.get('volume', 0), 'amount': spot.get('amount', 0),
        'high': high, 'low': low,
        'open': spot.get('open', 0), 'pre_close': pre_close,
        'turnover': spot.get('turnover'), 'amplitude': amplitude,
        'pe': spot.get('pe'), 'pb': spot.get('pb'), 'market_cap': spot.get('market_cap'),
    }

    # 2. 财务指标 + 四位专家评分
    fin = md.akshare.get_financial_indicators(code)

    # 2b. 公司信息：股本（推算市值）/ 行业（东财为主，yfinance 兜底）
    info = {}
    shares_outstanding = None
    industry_name = 'N/A'
    try:
        info = md.fetch_company_info(code)
        shares_outstanding = info.get('shares_outstanding')
        if info.get('industry'):
            industry_name = info['industry']
    except Exception:
        pass
    if not info.get('industry'):
        try:
            yf_info = md.yfinance.fetch_company_info(f'{code}.SS')
            if yf_info.get('sector'):
                industry_name = yf_info['sector']
            elif yf_info.get('industry'):
                industry_name = yf_info['industry']
        except Exception:
            pass

    # 市值：优先换手率推导（Violet Z 原逻辑），缺失时用 股本×现价 兜底（东财报表真实股本）
    if stock.get('market_cap') is None and shares_outstanding and price:
        stock['market_cap'] = round(shares_outstanding * price, 2)

    experts_conf = [
        ('buffett', 'Warren Buffett', '🛡️', score_buffett),
        ('munger', 'Charlie Munger', '🧠', score_munger),
        ('duan', '段永平', '⚖️', lambda f: score_duan(f, stock.get('market_cap'))),
        ('dalio', 'Ray Dalio', '🌊', score_dalio),
    ]
    experts = []
    for eid, ename, eicon, score_fn in experts_conf:
        s, adv, risks = score_fn(fin)
        decision, dcolor = get_decision(s)
        summary = generate_summary(ename, s, adv, risks)
        experts.append({
            'id': eid, 'name': ename, 'icon': eicon,
            'score': s, 'decision': decision, 'decision_color': dcolor,
            'summary': summary, 'advantages': adv, 'risks': risks,
        })

    stock['industry'] = industry_name

    # 4. 指标对比（PE/PB + 财务核心指标）
    comparison = []
    if stock.get('pe') is not None:
        comparison.append({'label': '市盈率', 'value': stock['pe']})
    if stock.get('pb') is not None:
        comparison.append({'label': '市净率', 'value': stock['pb']})
    compare_fields = [
        ('roe', 'ROE(%)', True), ('debt_ratio', '资产负债率(%)', False),
        ('gross_margin', '毛利率(%)', True), ('net_margin', '净利率(%)', True),
        ('profit_growth', '净利润增长率(%)', True),
        ('revenue_growth', '营收增长率(%)', True),
    ]
    for key, label, _ in compare_fields:
        val = fin.get(key)
        if val is not None:
            comparison.append({
                'label': label,
                'value': round(val, 2) if isinstance(val, float) else val,
            })

    return {
        'stock': stock,
        'experts': experts,
        'comparison': comparison,
        'industry_name': industry_name,
    }
