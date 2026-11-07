"""章节完整性门：核心章节没拿到数据时，必须说出来。

现状的反例：0700.HK 的财务三表整段为空（`financial_analysis.periods=[]`、
`ratios={}`），DCF 因此不适用，但同一份产物里 `trade_plan` 照给「看多 / 半仓」。
读者看到的是一个有结论的报告，看不到「支撑结论的两章其实是空的」。

这一层只做判定，不改任何数值：对四个核心章节检查「最小可用数据」是否到位，
结果写进产物的 `completeness`，由 UI 在报告顶部提示。

输出只有布尔与字符串，不产生数值叶子，因此台账 `fact_total` 与基线不受影响。
"""
from __future__ import annotations

# 章节 key → 中文名（UI 提示用）
SECTION_LABELS = {
    "company_info": "公司信息",
    "financial_analysis": "财务分析",
    "valuation_analysis": "估值分析",
    "price_history": "行情序列",
}


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _check_company_info(r: dict) -> str | None:
    name = _as_dict(r.get("company_info")).get("name")
    if not isinstance(name, str) or not name.strip():
        return "缺少公司名称"
    return None


def _check_financial_analysis(r: dict) -> str | None:
    fin = _as_dict(r.get("financial_analysis"))
    periods = fin.get("periods")
    ratios = fin.get("ratios")
    if not isinstance(periods, list) or not periods:
        return "三表期数缺失（periods 为空），财务比率无法计算"
    if not isinstance(ratios, dict) or not ratios:
        return "财务比率缺失（ratios 为空）"
    return None


def _check_valuation_analysis(r: dict) -> str | None:
    va = _as_dict(r.get("valuation_analysis"))
    dcf = _as_dict(va.get("dcf"))
    if not dcf:
        return "DCF 未生成"
    if dcf.get("fair_value") is None:
        reason = dcf.get("reason")
        return f"DCF 已降级：{reason}" if reason else "DCF 未给出每股内在价值"
    return None


def _check_price_history(r: dict) -> str | None:
    ph = _as_dict(r.get("price_history"))
    if ph.get("available") is not True:
        reason = ph.get("reason")
        return f"行情不可用：{reason}" if reason else "行情不可用"
    close = ph.get("close")
    if not isinstance(close, list) or not close:
        return "行情序列缺少收盘价"
    return None


_CHECKS = (
    ("company_info", _check_company_info),
    ("financial_analysis", _check_financial_analysis),
    ("valuation_analysis", _check_valuation_analysis),
    ("price_history", _check_price_history),
)


def check_completeness(result: dict) -> dict:
    """核心章节完整性判定。

    Returns:
        {"complete": bool,
         "gaps": [{"section": "financial_analysis", "reason": "三表期数缺失…"}]}
    """
    if not isinstance(result, dict):
        return {"complete": False,
                "gaps": [{"section": "result", "reason": "产物不是对象"}]}
    gaps = []
    for section, check in _CHECKS:
        reason = check(result)
        if reason:
            gaps.append({"section": section, "reason": reason})
    return {"complete": not gaps, "gaps": gaps}
