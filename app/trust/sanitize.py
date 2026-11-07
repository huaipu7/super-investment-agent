"""产物落地前的数值净化边界。

产物里出现 NaN / Inf 有三重危害：
  1. `json.dumps` 默认 `allow_nan=True`，会把 `NaN` 原样写进文件，产出的是
     **非法 JSON**（`JSON.parse` 直接抛错，浏览器与后端解析器都吃不下）；
  2. 台账只对数值叶子出事实，NaN 也是 float，于是会生成一条 value 非有限的
     事实，L0 的「value 有限」断言必挂；
  3. 下游展示层拿到 NaN 会静默变成空值或 0，看不出是「没有数据」还是「数据坏了」。

这里只做一件事：**把非有限值换成 None**，并把「哪条路径坏了、坏成什么」记录
到 `data_quality.non_finite`。不改任何有限数值（包括 float32 噪声——那是显示层
格式化的问题，不在数据层四舍五入）。

三条设计约束：
  1. 只在确实发现问题时才写 `data_quality`：干净产物的 `content_hash` / `run_id`
     必须与净化前逐字节一致，否则基线会无故漂移。
  2. `data_quality` 里只有字符串，不产生数值叶子，所以台账 `fact_total` 不变。
  3. 净化为空即返回原对象，不做无意义的深拷贝。
"""
from __future__ import annotations

import math

try:  # numpy 是既有依赖；缺失时退化为只处理 Python 原生数值
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None


def _to_native(value: object) -> object:
    """numpy 标量 → Python 原生。

    `np.float32` 不是 `float` 的子类，`json.dumps` 序列化它直接抛 TypeError，
    现有代码靠 `default=str` 兜底会把它悄悄写成字符串 `"1.5"`——数值变文本，
    台账与下游计算都认不出来。所以在净化阶段先归一。
    只处理标量（`np.generic`），不动 ndarray。
    """
    if _np is not None and isinstance(value, _np.generic):
        return value.item()
    return value


def _non_finite_kind(value: object) -> str | None:
    """是数值且非有限 → 返回 'nan'/'inf'/'-inf'；否则 None。"""
    if isinstance(value, bool) or isinstance(value, int):
        return None
    if not isinstance(value, float):
        return None
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return None


def _walk(node: object, path: str, issues: list[dict]) -> object:
    node = _to_native(node)
    kind = _non_finite_kind(node)
    if kind is not None:
        issues.append({"path": path or "<root>", "kind": kind})
        return None
    if isinstance(node, dict):
        return {k: _walk(v, f"{path}.{k}" if path else str(k), issues)
                for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, f"{path}[{i}]", issues) for i, v in enumerate(node)]
    if isinstance(node, tuple):
        return tuple(_walk(v, f"{path}[{i}]", issues) for i, v in enumerate(node))
    return node


def sanitize_numbers(obj: object, *, path: str = "") -> tuple[object, list[dict]]:
    """递归把非有限数值换成 None，返回 (净化后对象, 问题清单)。

    问题清单形如 `[{"path": "financial_analysis.current_price", "kind": "nan"}]`，
    路径与台账 fact.path 同一套写法，便于对照。
    """
    issues: list[dict] = []
    return _walk(obj, path, issues), issues


def sanitize_result(result: dict) -> dict:
    """管线产物净化入口。干净产物原样返回，保证 content_hash 不变。"""
    if not isinstance(result, dict):
        return result
    clean, issues = sanitize_numbers(result)
    if not issues:
        return result
    clean["data_quality"] = {"non_finite": issues}
    return clean
