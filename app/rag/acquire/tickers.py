"""股票代码归一化。

新语料入库与查询入口都先过这里，避免 600519 / 600519.SS / SH600519 在
ticker 等值过滤下互相看不见。**不迁移存量 2041 条切片**——迁移收益低于风险。

认不出来就返回空串，绝不猜：猜错会让一篇年报挂到别的公司名下。
"""
from __future__ import annotations

import re

_A_SHARE_SUFFIX = {"SS": ".SS", "SH": ".SS", "SZ": ".SZ", "BJ": ".BJ"}

# 美股：AAA / BRK.B / BRK-B；不接受纯数字
_US_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}(?:[.\-][A-Z]{1,2})?$")

_PREFIXED_CN_RE = re.compile(r"^(SH|SZ|BJ)(\d{6})$")


def _a_share_suffix(digits: str) -> str:
    """6 位 A 股代码按板块判交易所：沪 6/9、深 0/3、北交 4/8。"""
    if digits[0] in "69":
        return ".SS"
    if digits[0] in "03":
        return ".SZ"
    if digits[0] in "48":
        return ".BJ"
    return ""


def canonical_ticker(raw: str | None) -> str:
    """归一为 600519.SS / 000001.SZ / 00700.HK / AAPL 形式，未知返回 ""。"""
    if raw is None:
        return ""
    s = str(raw).strip().upper().replace(" ", "")
    if not s:
        return ""

    m = _PREFIXED_CN_RE.match(s)
    if m:
        return f"{m.group(2)}{_A_SHARE_SUFFIX[m.group(1)]}"

    if "." in s:
        base, _, suffix = s.rpartition(".")
        suffix = suffix.strip()
        if suffix in _A_SHARE_SUFFIX:
            return f"{base}{_A_SHARE_SUFFIX[suffix]}" if base.isdigit() and len(base) == 6 else ""
        if suffix == "HK":
            return f"{base.zfill(5)}.HK" if base.isdigit() and 1 <= len(base) <= 5 else ""
        # 美股后缀（BRK.B / RDS.A）：原样保留，其余一律不认
        if base.isalpha() and suffix.isalpha() and len(suffix) <= 2:
            return s
        return ""

    if s.isdigit():
        if len(s) == 6:
            return f"{s}{_a_share_suffix(s)}"
        if 1 <= len(s) <= 5:
            return f"{s.zfill(5)}.HK"
        return ""

    if _US_RE.match(s):
        return s
    return ""
