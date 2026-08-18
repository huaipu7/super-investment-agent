"""HTML 报告生成 —— 将 Markdown 转为自包含 HTML（图表以 base64 内嵌，可直接打开）。"""
from __future__ import annotations

import base64
import os
import re

import markdown

_CSS = """
body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; color: #1f2937;
       max-width: 900px; margin: 0 auto; padding: 32px 24px; line-height: 1.7; }
h1 { color: #1e3a5f; border-bottom: 3px solid #2563eb; padding-bottom: 8px; }
h2 { color: #1e3a5f; border-left: 4px solid #2563eb; padding-left: 10px; margin-top: 28px; }
h3 { color: #334155; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }
th { background: #1e3a5f; color: #fff; padding: 8px 10px; text-align: left; }
td { border: 1px solid #d1d5db; padding: 6px 10px; }
tr:nth-child(even) td { background: #f3f4f6; }
blockquote { border-left: 4px solid #d1d5db; margin: 8px 0; padding: 4px 12px;
             color: #6b7280; background: #f9fafb; }
img { max-width: 100%; height: auto; margin: 8px 0; border: 1px solid #e5e7eb;
      border-radius: 6px; }
ul, ol { padding-left: 22px; }
hr { border: none; border-top: 1px solid #e5e7eb; margin: 24px 0; }
"""

_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _inline_images(md: str, base_dir: str | None) -> str:
    """把 markdown 图片引用替换为 base64 data URI（文件相对于 base_dir）。"""
    if not base_dir:
        return md

    def _replace(m: re.Match) -> str:
        alt, rel = m.group(1), m.group(2)
        if rel.startswith(("http", "data:")):
            return m.group(0)
        path = os.path.join(base_dir, rel)
        if not os.path.exists(path):
            return m.group(0)
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return f"![{alt}](data:image/png;base64,{b64})"
        except Exception:
            return m.group(0)

    return _IMG_RE.sub(_replace, md)


def markdown_to_html(md: str, base_dir: str | None = None, title: str = "投研报告") -> str:
    """将 Markdown 转为自包含 HTML。

    Args:
        md: Markdown 文本
        base_dir: 图表 PNG 所在目录（用于内嵌 base64）；None 则保留原始路径引用
        title: 页面标题

    Returns:
        str: 完整 HTML 文档
    """
    md_inlined = _inline_images(md, base_dir)
    body = markdown.markdown(md_inlined, extensions=["tables", "fenced_code", "sane_lists"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{_CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""


def result_to_html(result: dict, charts: dict | None = None, base_dir: str | None = None) -> str:
    """从分析管线结果直接生成 HTML 报告（含图表）。"""
    from app.report.markdown import result_to_markdown

    md = result_to_markdown(result, charts=charts)
    ticker = result.get("ticker", "")
    name = result.get("company_info", {}).get("name", ticker)
    return markdown_to_html(md, base_dir=base_dir, title=f"深度研究报告 {name}（{ticker}）")
