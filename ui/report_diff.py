"""研报章节拆分 + 两期报告 diff（纯函数，离线可测）。

- split_sections: 把研报 md 拆成 preamble + [(章节标题, 章节正文)]（按 `## ` 顶层章节）。
- render_section_diff: 单个章节两版本正文 → 行级/字符级高亮 HTML。
- diff_reports: 两期完整 md → 章节级 diff + 全局统计。

遵循数据真实性：diff 只高亮文本差异，不重算任何数值。
"""
from __future__ import annotations

import difflib
import html

_ESC = html.escape


def split_sections(md: str) -> tuple[str, list[tuple[str, str]]]:
    """将研报 md 拆成 (preamble, [(章节标题, 章节正文), ...])。

    顶层章节以 `## ` 开头（标题形如 `## 一、执行摘要`）；附录内的 `### `
    子标题不会误切（仅匹配 `## ` 行首）。preamble 为第一个章节前的头部。
    """
    preamble: list[str] = []
    sections: list[tuple[str, str]] = []
    cur_title: str | None = None
    cur_body: list[str] = []

    def _finalize():
        """收尾当前章节：去掉末尾空行，避免章节间空行混入正文。"""
        nonlocal cur_title, cur_body
        while cur_body and not cur_body[-1].strip():
            cur_body.pop()
        if cur_title is not None or cur_body:
            sections.append((cur_title or "", "\n".join(cur_body)))
        cur_title, cur_body = None, []

    for ln in (md or "").splitlines():
        if ln.startswith("## "):
            _finalize()
            cur_title = ln[3:].strip()
            cur_body = []
        elif cur_title is None:
            preamble.append(ln)
        else:
            cur_body.append(ln)
    _finalize()
    while preamble and not preamble[-1].strip():
        preamble.pop()
    return "\n".join(preamble), sections


def _char_diff(old: str, new: str) -> tuple[str, str]:
    """同一行内字符级高亮（− 移除 / + 新增）。返回 (old_html, new_html)。"""
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    old_parts: list[str] = []
    new_parts: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        a, b = _ESC(old[i1:i2]), _ESC(new[j1:j2])
        if tag == "equal":
            old_parts.append(a)
            new_parts.append(b)
        elif tag in ("delete", "replace"):
            old_parts.append(f'<span style="background:#fdecea;color:#b71c1c">{a}</span>')
        if tag in ("insert", "replace"):
            new_parts.append(f'<span style="background:#e8f5e9;color:#1b5e20">{b}</span>')
    return "".join(old_parts), "".join(new_parts)


def _line_stats(a: str, b: str) -> tuple[int, int]:
    """返回 (n_removed, n_added) 行数（仅差异行）。"""
    sm = difflib.SequenceMatcher(None, a.splitlines(), b.splitlines(), autojunk=False)
    n_removed = n_added = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            n_removed += i2 - i1
        if tag in ("insert", "replace"):
            n_added += j2 - j1
    return n_removed, n_added


def render_section_diff(old_body: str, new_body: str) -> str:
    """两版本章节正文 → 高亮 HTML（行级 + 字符级）。无差异返回空串。"""
    old_lines = old_body.splitlines()
    new_lines = new_body.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        # 上下文：变动块前的相邻相等行
        if i1 > 0:
            out.append(f'<div style="color:#888">{_ESC(old_lines[i1 - 1])}</div>')
        n_del, n_ins = i2 - i1, j2 - j1
        paired = min(n_del, n_ins)
        for k in range(paired):
            oh, nh = _char_diff(old_lines[i1 + k], new_lines[j1 + k])
            out.append(f'<div style="background:#fdecea;color:#b71c1c">− {oh}</div>')
            out.append(f'<div style="background:#e8f5e9;color:#1b5e20">+ {nh}</div>')
        for k in range(paired, n_del):
            out.append(f'<div style="background:#fdecea;color:#b71c1c">− {_ESC(old_lines[i1 + k])}</div>')
        for k in range(paired, n_ins):
            out.append(f'<div style="background:#e8f5e9;color:#1b5e20">+ {_ESC(new_lines[j1 + k])}</div>')
    return "\n".join(out)


def diff_reports(old_md: str, new_md: str) -> dict:
    """两期完整研报 → 章节级 diff 结果。

    Returns:
        {
            "sections": [{"title", "added", "removed", "changed",
                          "n_added", "n_removed", "html"}, ...],
            "stats": {"sections_total", "sections_changed", "added", "removed"},
        }
    """
    _, old_secs = split_sections(old_md)
    _, new_secs = split_sections(new_md)
    old_map = dict(old_secs)
    new_map = dict(new_secs)
    titles = list(dict.fromkeys([t for t, _ in old_secs] + [t for t, _ in new_secs]))

    sections = []
    stats = {"sections_total": 0, "sections_changed": 0, "added": 0, "removed": 0}
    for t in titles:
        ob = old_map.get(t, "")
        nb = new_map.get(t, "")
        n_removed, n_added = _line_stats(ob, nb)
        stats["sections_total"] += 1
        stats["added"] += n_added
        stats["removed"] += n_removed
        changed = ob != nb
        if changed:
            stats["sections_changed"] += 1
        sections.append({
            "title": t,
            "added": t not in old_map,
            "removed": t not in new_map,
            "changed": changed,
            "n_added": n_added,
            "n_removed": n_removed,
            "html": render_section_diff(ob, nb) if changed else "",
        })
    return {"sections": sections, "stats": stats}
