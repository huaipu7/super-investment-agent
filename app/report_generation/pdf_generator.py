"""PDF 报告生成器支持模块（迁移自 Research-Agent report_generation/pdf_generator.py）

仅迁移估值分析器（app/analysis/valuation.py）懒加载依赖的 get_risk_free_rate。
"""

# 无风险利率（从 yfinance ^TNX 获取实时 10 年期国债收益率，回退 2.5%）
_RFR_CACHE: float | None = None


def get_risk_free_rate() -> float:
    global _RFR_CACHE
    if _RFR_CACHE is not None:
        return _RFR_CACHE
    try:
        import yfinance as yf
        tnx = yf.Ticker("^TNX")
        info = tnx.history(period="1d")
        if not info.empty:
            rate = float(info["Close"].iloc[-1]) / 100.0
            if 0.0 < rate < 0.15:
                _RFR_CACHE = rate
                return rate
    except Exception:
        pass
    _RFR_CACHE = 0.025
    return 0.025
