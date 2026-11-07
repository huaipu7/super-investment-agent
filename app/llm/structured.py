"""LLM 结构化输出解析 —— 统一 JSON 提取与校验。

原先 SWOT / 竞品 / 地缘 / 分部 / 新闻 / 股东大会 各自实现了一份
「剥 ```json 围栏 → json.loads → 失败静默返回 default」的解析器，问题有二：
  1. 同一段逻辑重复 6 份；
  2. 失败只留一行 "返回非 JSON"，看不出是哪个字段错了 —— 静默降级。

本模块提供 parse_llm_json()：稳健提取 → 可选 pydantic 校验 → 字段级告警。
容器结构错误（不是 dict / 缺必要键）会被明确报出；叶子值能救则救（记日志），
不可救则回退 default，绝不假装成功。
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from app.utils.logger import logger

__all__ = ["extract_json", "parse_llm_json", "failure_stats", "reset_failures"]

# 解析失败计数：仅写日志看不出「今天模型坏了几次」。按 context 分类累计，
# 供 metrics 端点与评测的 llm_error_rate 护栏指标读取。
_FAILURES: dict[str, int] = {}


def failure_stats() -> dict[str, int]:
    return dict(_FAILURES)


def reset_failures() -> None:
    _FAILURES.clear()


def _count_failure(context: str) -> None:
    _FAILURES[context] = _FAILURES.get(context, 0) + 1


def extract_json(text: str) -> Any:
    """从 LLM 自由文本中提取首个合法 JSON 值（对象/数组）。

    依次尝试：整段直接解析 → 剥 markdown 围栏 → 从各候选起点做括号配平截取。
    失败抛 json.JSONDecodeError。
    """
    if not text or not text.strip():
        raise json.JSONDecodeError("空响应", text or "", 0)

    t = _strip_fence(text.strip())

    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass

    for start in _candidate_starts(t):
        snippet = _balanced_slice(t, start)
        if snippet is None:
            continue
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            continue

    raise json.JSONDecodeError("未能在响应中定位合法 JSON", t[:200], 0)


def parse_llm_json(
    text: str,
    model: type[BaseModel] | TypeAdapter | Any | None = None,
    default: Any = None,
    context: str = "LLM",
) -> Any:
    """提取 LLM 响应中的 JSON，（可选）按 pydantic 模型校验后返回。

    Args:
        text:     LLM 原始文本响应
        model:    pydantic BaseModel 子类，或任意可交给 TypeAdapter 的类型
                  （如 list[Competitor]）。None 表示只提取不校验。
        default:  提取/校验失败时的返回值（原调用方的兜底语义）
        context:  日志前缀，用于定位是哪个分析器出的问题

    Returns:
        校验通过 → 归一化后的 dict/list；失败 → default（并记录字段级原因）
    """
    if text is None or not str(text).strip():
        logger.warning("%s: 收到空响应，使用默认值", context)
        _count_failure(context)
        return default

    try:
        data = extract_json(text)
    except json.JSONDecodeError as e:
        logger.warning("%s: 未能从响应中提取 JSON (%s)。原文前 200 字: %s",
                       context, e.msg, str(text).strip()[:200])
        _count_failure(context)
        return default

    if model is None:
        return data

    try:
        adapter = model if isinstance(model, TypeAdapter) else TypeAdapter(model)
        return adapter.dump_python(adapter.validate_python(data))
    except ValidationError as e:
        logger.warning("%s: JSON 结构校验未通过，回退默认值。字段错误: %s",
                       context, _format_errors(e))
        _count_failure(context)
        return default


# ── 内部工具 ──────────────────────────────────────────────────

def _strip_fence(text: str) -> str:
    """剥掉 markdown 代码围栏；无围栏则原样返回。"""
    if "```" not in text:
        return text
    parts = text.split("```")
    if len(parts) < 3:
        return text
    inner = parts[1]
    stripped = inner.lstrip()
    if stripped[:4].lower() == "json":
        inner = stripped[4:]
    return inner.strip()


def _candidate_starts(text: str) -> list[int]:
    """所有可能的 JSON 起点（{ 或 [ 的位置），按出现顺序。"""
    return [i for i, ch in enumerate(text) if ch in "{["]


def _balanced_slice(text: str, start: int) -> str | None:
    """从 start 起截取首个括号配平的片段；括号不配平返回 None。

    正确处理字符串字面量内的括号与反斜杠转义。
    """
    stack: list[str] = []
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
            if not stack:
                return text[start:i + 1]
    return None


def _format_errors(e: ValidationError, limit: int = 5) -> str:
    """把 pydantic 校验错误压成一行，带字段路径。"""
    parts: list[str] = []
    for err in e.errors()[:limit]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        parts.append(f"{loc}: {err.get('msg', '')}")
    extra = len(e.errors()) - limit
    if extra > 0:
        parts.append(f"...另有 {extra} 处")
    return "; ".join(parts)
