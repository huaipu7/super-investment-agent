"""统一报告生成入口 —— 一键生成 Markdown / HTML / PDF 三种格式。

用法:
    gen = ReportGenerator()
    paths = gen.generate(analysis_result)          # 深度研报（三种格式 + 图表）
    paths = gen.from_chat_report(md_text, "AAPL")  # 对话报告（转 HTML/PDF）
"""
from __future__ import annotations

import os
from datetime import datetime

from app.report.charts import ChartGenerator
from app.report.html import markdown_to_html, result_to_html
from app.report.markdown import result_to_markdown
from app.report.pdf import markdown_to_pdf, result_to_pdf
from app.utils.logger import logger


class ReportGenerator:
    def __init__(self, output_dir: str = "data/reports"):
        self.output_dir = output_dir
        self.chart_dir = os.path.join(output_dir, "charts")
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.chart_dir, exist_ok=True)

    def _base_name(self, ticker: str) -> str:
        return f"{ticker}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def generate(self, result: dict, fmt: str = "all") -> dict[str, str]:
        """从分析管线结果生成报告。

        Args:
            result: AnalysisOrchestrator.run() 输出
            fmt: "all" | "md" | "html" | "pdf"

        Returns:
            {"markdown"/"html"/"pdf": 文件路径, "charts": {key: path}}
        """
        ticker = result.get("ticker", "report")
        base = self._base_name(ticker)
        paths: dict[str, str] = {}

        logger.info("[report] 生成图表...")
        chart_gen = ChartGenerator(output_dir=self.chart_dir)
        charts = chart_gen.generate_all(result)
        paths["charts"] = charts
        logger.info("[report] 图表 %d 张", len(charts))

        if fmt in ("all", "md"):
            md_path = os.path.join(self.output_dir, f"{base}.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(result_to_markdown(result, charts=charts))
            paths["markdown"] = md_path
            logger.info("[report] Markdown: %s", md_path)

        if fmt in ("all", "html"):
            html_path = os.path.join(self.output_dir, f"{base}.html")
            with open(html_path, "w", encoding="utf-8") as f:
                # markdown 图片路径为 charts/xxx.png，相对 output_dir 解析
                f.write(result_to_html(result, charts=charts, base_dir=self.output_dir))
            paths["html"] = html_path
            logger.info("[report] HTML: %s", html_path)

        if fmt in ("all", "pdf"):
            pdf_path = os.path.join(self.output_dir, f"{base}.pdf")
            result_to_pdf(result, charts=charts, output_path=pdf_path)
            paths["pdf"] = pdf_path
            logger.info("[report] PDF: %s", pdf_path)

        return paths

    def from_chat_report(self, md_text: str, title: str = "投研报告",
                         fmt: str = "all") -> dict[str, str]:
        """把对话 Agent 的 Markdown 报告（如 Violetz expressor 输出）转为 HTML/PDF。

        Args:
            md_text: Markdown 报告文本
            title: 报告标题（用作 PDF 页脚与 HTML 标题）
            fmt: "all" | "html" | "pdf" | "md"

        Returns:
            {"markdown"/"html"/"pdf": 文件路径}
        """
        safe_title = "".join(c for c in title if c.isalnum() or c in "._- ") or "report"
        base = f"{safe_title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        paths: dict[str, str] = {}

        if fmt in ("all", "md"):
            md_path = os.path.join(self.output_dir, f"{base}.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md_text)
            paths["markdown"] = md_path

        if fmt in ("all", "html"):
            html_path = os.path.join(self.output_dir, f"{base}.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(markdown_to_html(md_text, base_dir=None, title=title))
            paths["html"] = html_path

        if fmt in ("all", "pdf"):
            pdf_path = os.path.join(self.output_dir, f"{base}.pdf")
            markdown_to_pdf(md_text, output_path=pdf_path, title=title)
            paths["pdf"] = pdf_path

        logger.info("[report] 对话报告 → %s", list(paths.keys()))
        return paths
