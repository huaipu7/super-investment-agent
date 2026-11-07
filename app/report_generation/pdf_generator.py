"""PDF 报告生成器支持模块（迁移自 Research-Agent report_generation/pdf_generator.py）

保留这一层只为兼容旧调用点；取数逻辑统一在 `app.analysis.estimates`，
不再有第二份「yfinance ^TNX + 裸 2.5%」的实现。
"""

_RFR_CACHE: float | None = None


def get_risk_free_rate() -> float:
    global _RFR_CACHE
    if _RFR_CACHE is None:
        from app.analysis.estimates import FALLBACK_RF, get_risk_free_rate as _estimate
        _RFR_CACHE = _estimate(market="us").get("value") or FALLBACK_RF
    return _RFR_CACHE
