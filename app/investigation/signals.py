"""确定性财务信号 —— 假设空间的唯一来源（反编造闸门 1）。

**全程无 LLM。** 从清洗后的真实三表按固定公式算出信号，每条带 `material`
标记。`material` 由本模块顶部的阈值表判定：不是模型选、不是运行时可调——
这正是"假设是真实数字的函数，而不是模型的想象"的实现方式。

**闸门 1**：没有 material 信号 → 下游 hypotheses 播种出零个成因族 → 零假设。
所以"没数据却能给出一堆泛泛假设"在结构上不可能发生。

数据形状（沿用 app/data/sources/base.py 的契约）：
    frames = {"income": df, "balance_sheet": df, "cash_flow": df}
    其中 df.index   = 报告期（Timestamp 或 "2023-12-31" 这类字符串）
         df.columns = 标准化科目代码

    注意方向：**行是期间、列是科目**（base.DataSource 的 docstring 明确写着
    "index=会计期间, columns=指标代码"）。科目名到标准代码的归一由各 source
    自己完成（yfinance_source._fetch_and_normalize 里就调了
    normalize_metric_name），所以到这里时列名已经是标准代码。
    lookup_series 仍对两种方向都容错，但以列优先。

**季度口径警告**：A股季报是累计口径（Q2=半年、Q4=全年），相邻期不可比。
因此 period != "annual" 时本模块**只产出比率类水平信号，不产出任何同比/环比
增量信号**——对累计序列做差会静默编造出一个不存在的趋势。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.constants import FINANCIAL_METRICS

# ── 科目归属（决定去哪个 statement 里找；找不到会回退全表搜）──────────
METRIC_STATEMENT: dict[str, str] = {
    "REVENUE": "income",
    "COST_OF_REVENUE": "income",
    "GROSS_PROFIT": "income",
    "OPERATING_EXPENSE": "income",
    "OPERATING_INCOME": "income",
    "NET_INCOME": "income",
    "EBITDA": "income",
    "INTEREST_EXPENSE": "income",
    "RESEARCH_DEV": "income",
    "TOTAL_ASSETS": "balance_sheet",
    "TOTAL_LIABILITIES": "balance_sheet",
    "TOTAL_EQUITY": "balance_sheet",
    "CURRENT_ASSETS": "balance_sheet",
    "CURRENT_LIABILITIES": "balance_sheet",
    "CASH_AND_EQUIVALENTS": "balance_sheet",
    "ACCOUNTS_RECEIVABLE": "balance_sheet",
    "INVENTORY": "balance_sheet",
    "FIXED_ASSETS": "balance_sheet",
    "GOODWILL": "balance_sheet",
    "OPERATING_CASH_FLOW": "cash_flow",
    "INVESTING_CASH_FLOW": "cash_flow",
    "FINANCING_CASH_FLOW": "cash_flow",
    "FREE_CASH_FLOW": "cash_flow",
    "CAPEX": "cash_flow",
    "STOCK_BUYBACK": "cash_flow",
}

METRIC_CODES: frozenset[str] = frozenset(METRIC_STATEMENT)
METRIC_LABELS: dict[str, str] = {
    k: FINANCIAL_METRICS.get(k, k) for k in METRIC_STATEMENT
}

# ── 阈值表（material 的判定标准；故意写成常量，不做成配置）──────────
# 理由：这是"什么算异常"的定义，一旦运行时可调，反编造闸门就可以被悄悄放宽。
# 形态：("abs", x) → |v| >= x；("gte", x) → v >= x；("outside", lo, hi) → v<lo 或 v>hi
#       ("rel", pct, floor_pp) → |Δ/上期| >= pct% 且 |Δ| >= floor_pp
_THRESHOLDS: dict[str, tuple] = {
    # 利润率类用**相对变化**：同一个 1.5pp，对 91% 毛利是 1.6%、对 20% 毛利是 7.5%，
    # 绝对 pp 阈值因此对利润率水平不敏感。地板项兜住利润率趋近 0 时相对值被放大。
    "gross_margin_delta": ("rel", 1.0, 0.5),       # |Δ/上期| >= 1.0%，且 |Δ| >= 0.5pp
    "net_margin_delta": ("rel", 1.0, 0.5),
    "operating_margin_delta": ("rel", 1.0, 0.5),
    "revenue_growth": ("abs", 5.0),                # 营收同比（%）
    "cogs_growth_minus_revenue": ("gte", 8.0),     # 成本增速 - 营收增速（pp）
    "inventory_growth_minus_revenue": ("gte", 10.0),   # 存货增速 - 营收增速（pp）
    "ar_growth_minus_revenue": ("gte", 10.0),      # 应收增速 - 营收增速（pp）
    "cash_conversion": ("outside", 0.7, 1.5),      # 经营现金流 / 净利润（x）
    "capex_to_revenue_delta": ("abs", 2.0),        # 资本开支占比同比（pp）
    "rd_to_revenue_delta": ("abs", 1.0),           # 研发投入占比同比（pp）
    "debt_to_equity_delta": ("abs", 0.2),          # 负债权益比同比（x）
    "interest_expense_growth": ("gte", 20.0),      # 利息支出同比（%）
}

# 年度口径才有的增量型信号（季度累计口径下不可计算，见模块 docstring）
_YOY_SIGNAL_CODES: frozenset[str] = frozenset({
    "gross_margin_delta", "net_margin_delta", "operating_margin_delta",
    "revenue_growth", "cogs_growth_minus_revenue",
    "inventory_growth_minus_revenue", "ar_growth_minus_revenue",
    "capex_to_revenue_delta", "rd_to_revenue_delta", "debt_to_equity_delta",
    "interest_expense_growth",
})


@dataclass
class Signal:
    """一条确定性信号。`value is None` 表示输入缺失、未能计算（不编造）。"""
    code: str
    label: str
    value: float | None = None
    baseline: float | None = None
    delta: float | None = None
    rel_value: float | None = None      # 相对变化（%），material 判定的输入之一
    unit: str = ""
    direction: str = "flat"
    periods: list[str] = field(default_factory=list)
    material: bool = False
    threshold: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code, "label": self.label,
            "value": _r(self.value), "baseline": _r(self.baseline),
            "delta": _r(self.delta), "rel_value": _r(self.rel_value),
            "unit": self.unit,
            "direction": self.direction, "periods": self.periods,
            "material": self.material, "threshold": self.threshold,
            "note": self.note,
        }


def _r(v: float | None) -> float | None:
    return None if v is None else round(float(v), 4)


# ── 取数 ──────────────────────────────────────────────────────

def _period_key(col):
    """报告期排序键：能解析成日期的按时间，否则按字符串（不混类型比较）。"""
    ts = pd.to_datetime(col, errors="coerce")
    if not pd.isna(ts):
        return (0, int(ts.value))
    return (1, str(col))


def lookup_series(frames: dict, code: str) -> pd.Series | None:
    """按科目代码取一条**按报告期升序**的数值序列；取不到返回 None（不编造）。

    优先按列取（真实契约：columns=科目代码）；列里没有再退化为按行取，
    以容忍未转置的数据源。两种情况返回的 Series 都以报告期为索引。
    """
    if not code or not isinstance(frames, dict):
        return None
    stmt = METRIC_STATEMENT.get(code)
    order = ([stmt] if stmt else []) + [
        k for k in ("income", "balance_sheet", "cash_flow") if k != stmt
    ]
    for key in order:
        df = frames.get(key)
        if df is None or getattr(df, "empty", True):
            continue
        try:
            if code in df.columns:                      # 契约方向
                row = df[code]
                if isinstance(row, pd.DataFrame):       # 同科目重复列 → 取第一条
                    row = row.iloc[:, 0]
            elif code in df.index:                      # 未转置 → 容错
                row = df.loc[code]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[0]
            else:
                continue
            row.index = [i for i in row.index]          # 统一为报告期索引
            s = pd.to_numeric(row, errors="coerce").dropna()
        except Exception:
            continue
        if s.empty:
            continue
        return s.reindex(sorted(s.index, key=_period_key))
    return None


def _ratio(a: pd.Series | None, b: pd.Series | None) -> pd.Series | None:
    """同期对齐后逐期相除；分母为 0 或结果非有限 → 该期弃。"""
    if a is None or b is None:
        return None
    common = a.index.intersection(b.index)
    if len(common) == 0:
        return None
    a2 = a.reindex(common)
    b2 = b.reindex(common).where(lambda x: x != 0)
    r = (a2 / b2).replace([float("inf"), float("-inf")], pd.NA).dropna()
    if r.empty:
        return None
    return r.reindex(sorted(r.index, key=_period_key))


def _growth(series: pd.Series | None) -> tuple[float | None, float | None, list[str]]:
    """末两期同比：(值, 上期基数, 期标签)。不足两期 → (None, None, labels)。"""
    if series is None or len(series) < 1:
        return None, None, []
    labels = [str(c) for c in series.index][-2:]
    if len(series) < 2:
        return None, None, labels
    prev = float(series.iloc[-2])
    cur = float(series.iloc[-1])
    if prev == 0:
        return None, None, labels
    return (cur - prev) / abs(prev) * 100.0, prev, labels


def _delta_pp(series: pd.Series | None) -> tuple[float | None, float | None, list[str]]:
    """末两期差值（百分点/倍数）：(现值, 上期值, 期标签)。不足两期 → (None, ...)。"""
    if series is None or len(series) < 2:
        return None, None, ([str(c) for c in series.index][-2:] if series is not None else [])
    labels = [str(c) for c in series.index][-2:]
    return float(series.iloc[-1]), float(series.iloc[-2]), labels


def _rel_change(value: float | None, baseline: float | None) -> float | None:
    """相对变化（%）：Δ / |上期| × 100。上期缺失或为 0 → None（不拿 0 当分母编数）。"""
    if value is None or baseline is None or baseline == 0:
        return None
    return (value - baseline) / abs(baseline) * 100.0


def _is_material(code: str, value: float | None,
                 rel_value: float | None = None) -> bool:
    if value is None:
        return False
    spec = _THRESHOLDS.get(code)
    if not spec:
        return False
    kind = spec[0]
    if kind == "abs":
        return abs(value) >= spec[1]
    if kind == "gte":
        return value >= spec[1]
    if kind == "outside":
        return value < spec[1] or value > spec[2]
    if kind == "rel":
        # 相对变化过线 **且** 绝对变化不低于噪声地板：后者防「利润率 0.05%、
        # 一个四舍五入级扰动就是 20% 相对变化」这类假信号。
        return rel_value is not None and abs(rel_value) >= spec[1] and abs(value) >= spec[2]
    return False


def _threshold_text(code: str) -> str:
    spec = _THRESHOLDS.get(code)
    if not spec:
        return ""
    if spec[0] == "abs":
        return f"|x| >= {spec[1]}"
    if spec[0] == "gte":
        return f"x >= {spec[1]}"
    if spec[0] == "outside":
        return f"x < {spec[1]} 或 x > {spec[2]}"
    if spec[0] == "rel":
        return f"|Δ/上期| >= {spec[1]}% 且 |Δ| >= {spec[2]}pp"
    return ""


def _dir(delta: float | None) -> str:
    if delta is None:
        return "flat"
    if delta > 1e-9:
        return "up"
    if delta < -1e-9:
        return "down"
    return "flat"


def _mk(code: str, label: str, unit: str, value, baseline, periods,
        *, delta=None, rel=None, note="") -> Signal:
    return Signal(
        code=code, label=label, value=value, baseline=baseline, delta=delta,
        rel_value=rel,
        unit=unit, direction=_dir(delta) if delta is not None else _dir(value),
        periods=periods, material=_is_material(code, value, rel),
        threshold=_threshold_text(code), note=note,
    )


# ── 主入口 ────────────────────────────────────────────────────

def extract_signals(frames: dict, *, period: str = "annual") -> list[Signal]:
    """算出全部信号。取不到的科目 → value=None（material 恒 False），不编造。

    返回顺序固定，便于报告与测试稳定比对。
    """
    annual = str(period or "annual").lower() == "annual"
    revenue = lookup_series(frames, "REVENUE")
    cogs = lookup_series(frames, "COST_OF_REVENUE")
    gross_profit = lookup_series(frames, "GROSS_PROFIT")
    op_income = lookup_series(frames, "OPERATING_INCOME")
    net_income = lookup_series(frames, "NET_INCOME")
    ocf = lookup_series(frames, "OPERATING_CASH_FLOW")
    capex = lookup_series(frames, "CAPEX")
    rd = lookup_series(frames, "RESEARCH_DEV")
    inventory = lookup_series(frames, "INVENTORY")
    ar = lookup_series(frames, "ACCOUNTS_RECEIVABLE")
    liabilities = lookup_series(frames, "TOTAL_LIABILITIES")
    equity = lookup_series(frames, "TOTAL_EQUITY")
    interest = lookup_series(frames, "INTEREST_EXPENSE")

    # 毛利率：优先 毛利/营收，回退 (营收-成本)/营收
    gross_margin = _ratio(gross_profit, revenue)
    if gross_margin is None:
        gross_margin = _ratio((revenue - cogs) if (revenue is not None and cogs is not None)
                              else None, revenue)
    net_margin = _ratio(net_income, revenue)
    op_margin = _ratio(op_income, revenue)
    if op_margin is None and revenue is not None and cogs is not None:
        op_margin = _ratio(revenue - cogs, revenue)
    cash_conv = _ratio(ocf, net_income)
    capex_ratio = _ratio(capex, revenue)
    rd_ratio = _ratio(rd, revenue)
    de = _ratio(liabilities, equity)

    out: list[Signal] = []

    if annual:
        v, b, ps = _delta_pp(gross_margin)
        d = None if (v is None or b is None) else (v - b) * 100.0
        out.append(_mk("gross_margin_delta", "毛利率同比变化", "pp", d, None, ps, delta=d,
                       rel=_rel_change(v, b),
                       note="" if v is not None else "毛利率不可计算（缺毛利/营收）"))
        v, b, ps = _delta_pp(net_margin)
        d = None if (v is None or b is None) else (v - b) * 100.0
        out.append(_mk("net_margin_delta", "净利率同比变化", "pp", d, None, ps, delta=d,
                       rel=_rel_change(v, b),
                       note="" if v is not None else "净利率不可计算（缺净利/营收）"))
        v, b, ps = _delta_pp(op_margin)
        d = None if (v is None or b is None) else (v - b) * 100.0
        out.append(_mk("operating_margin_delta", "营业利润率同比变化", "pp", d, None, ps,
                       delta=d, rel=_rel_change(v, b)))

        g, b, ps = _growth(revenue)
        out.append(_mk("revenue_growth", "营收同比", "%", g, b, ps, delta=g))

        g_r = g
        g_c, b_c, ps_c = _growth(cogs)
        m = None if (g_c is None or g_r is None) else g_c - g_r
        out.append(_mk("cogs_growth_minus_revenue", "成本增速减营收增速", "pp", m, g_r, ps_c,
                       delta=m))

        g_i, _, ps_i = _growth(inventory)
        m = None if (g_i is None or g_r is None) else g_i - g_r
        out.append(_mk("inventory_growth_minus_revenue", "存货增速减营收增速", "pp", m, g_r,
                       ps_i, delta=m))

        g_a, _, ps_a = _growth(ar)
        m = None if (g_a is None or g_r is None) else g_a - g_r
        out.append(_mk("ar_growth_minus_revenue", "应收增速减营收增速", "pp", m, g_r, ps_a,
                       delta=m))

        v, b, ps = _delta_pp(capex_ratio)
        d = None if (v is None or b is None) else (v - b) * 100.0
        out.append(_mk("capex_to_revenue_delta", "资本开支占比同比变化", "pp", d, b, ps,
                       delta=d))

        v, b, ps = _delta_pp(rd_ratio)
        d = None if (v is None or b is None) else (v - b) * 100.0
        out.append(_mk("rd_to_revenue_delta", "研发投入占比同比变化", "pp", d, b, ps, delta=d))

        v, b, ps = _delta_pp(de)
        d = None if (v is None or b is None) else (v - b)
        out.append(_mk("debt_to_equity_delta", "负债权益比同比变化", "x", d, b, ps, delta=d))

        g_i2, _, ps_i2 = _growth(interest)
        out.append(_mk("interest_expense_growth", "利息支出同比", "%", g_i2, None, ps_i2,
                       delta=g_i2))
    else:
        # 季度累计口径：只出水平信号，不出任何增量信号（见模块 docstring）
        for code, label in (("gross_margin_delta", "毛利率同比变化"),
                            ("net_margin_delta", "净利率同比变化"),
                            ("operating_margin_delta", "营业利润率同比变化"),
                            ("revenue_growth", "营收同比"),
                            ("cogs_growth_minus_revenue", "成本增速减营收增速"),
                            ("inventory_growth_minus_revenue", "存货增速减营收增速"),
                            ("ar_growth_minus_revenue", "应收增速减营收增速"),
                            ("capex_to_revenue_delta", "资本开支占比同比变化"),
                            ("rd_to_revenue_delta", "研发投入占比同比变化"),
                            ("debt_to_equity_delta", "负债权益比同比变化"),
                            ("interest_expense_growth", "利息支出同比")):
            out.append(Signal(code=code, label=label,
                              note="季度累计口径：需同期对同期，本模块不产出增量信号"))

    # 水平信号：两种口径都算（分子分母同期，累计也无害）
    lv, _, ps = _delta_pp(cash_conv)
    v = float(cash_conv.iloc[-1]) if cash_conv is not None and len(cash_conv) else None
    out.append(_mk("cash_conversion", "经营现金流/净利润", "x", v, None, ps, delta=lv,
                   note="" if v is not None else "经营现金流或净利润缺失"))

    return out


def material_signals(signals: list[Signal]) -> list[Signal]:
    """只留 material 的——下游播种成因族的唯一输入。"""
    return [s for s in signals if s.material and s.value is not None]


def signals_to_dicts(signals: list[Signal]) -> list[dict]:
    return [s.to_dict() for s in signals]


def frame_periods(frames: dict) -> list[str]:
    """全部 frame 出现过的报告期（升序），供报告标注数据覆盖范围。

    真实契约里报告期在 index；若某 frame 是未转置的（index 全是科目代码），
    则改用它的 columns。
    """
    seen: set = set()
    for df in (frames or {}).values():
        if df is None or getattr(df, "empty", True):
            continue
        try:
            labels = list(df.index)
            if labels and all(str(x).strip().upper() in METRIC_CODES for x in labels):
                labels = list(df.columns)
            for c in labels:
                seen.add(str(c))
        except Exception:
            continue
    return sorted(seen, key=_period_key)


def has_any_statement_data(frames: dict) -> bool:
    """三表里是否有任何一行可用数据——决定"零假设"是不是因为真没数据。"""
    for df in (frames or {}).values():
        if df is None or getattr(df, "empty", True):
            continue
        if len(df.index) > 0 and df.shape[1] > 0:
            return True
    return False
