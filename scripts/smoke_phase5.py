"""
Phase 5 冒烟测试：统一报告生成（PDF / HTML / Markdown + 图表，中文字体）。

验证：
  A. 深度研报：从 AnalysisResult（已有 data/phase5_full_aapl.json，或重跑管线）生成
     三格式报告；检查文件有效性、图表张数、图表 PNG 存在、HTML 图表 base64 内嵌。
  B. 对话报告：跑一次真实 Agent 分析（AAPL），把 expressor 输出的 Markdown 转 HTML/PDF。
  C. 中文字体：reportlab CJK 字体已注册（字体名含 CJK- 前缀）。

运行: PYTHONIOENCODING=utf-8 D:\\python\\python.exe scripts\\smoke_phase5.py
"""
import os
import sys
import json
import glob
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.report.generator import ReportGenerator
from app.report.fonts import reportlab_fonts

_results: list[dict] = []


def record(name: str, ok: bool, detail: str = ""):
    _results.append({"name": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


# ── C. 中文字体注册 ──────────────────────────────────────────

def check_fonts():
    fonts = reportlab_fonts()
    print("\n=== C. 中文字体 ===")
    print("  reportlab 注册字体:", fonts)
    cjk = [v for v in fonts.values() if v.startswith("CJK")]
    record("reportlab 中文字体注册", bool(cjk), f"{fonts}")
    if cjk:
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm
        names = {f.name for f in fm.fontManager.ttflist}
        record("matplotlib 中文字体加载", any(n in names for n in
               ("SimHei", "Microsoft YaHei", "SimSun")),
               f"sans-serif={plt.rcParams['font.sans-serif']}")


# ── A. 深度研报 ──────────────────────────────────────────────

def check_deep_report():
    print("\n=== A. 深度研报（AAPL） ===")
    json_path = Path("data/phase5_full_aapl.json")
    if json_path.exists():
        result = json.load(open(json_path, encoding="utf-8"))
        print(f"  使用已缓存分析结果: {json_path}（pipeline_time={result.get('pipeline_time')}s）")
    else:
        print("  无缓存结果，重跑分析管线（需数分钟）...")
        from app.analysis.pipeline import AnalysisOrchestrator
        result = AnalysisOrchestrator().run("AAPL")

    gen = ReportGenerator(output_dir="data/reports")
    paths = gen.generate(result, fmt="all")

    charts = paths.get("charts", {})
    record("图表生成", len(charts) >= 5, f"{len(charts)} 张: {list(charts.keys())[:6]}...")
    png_ok = all(os.path.exists(p) and os.path.getsize(p) > 1000 for p in charts.values())
    record("图表 PNG 有效", png_ok, f"{len(charts)} 个文件非空")

    # Markdown
    md_p = paths.get("markdown", "")
    md_ok = os.path.exists(md_p) and os.path.getsize(md_p) > 2000
    if md_ok:
        txt = open(md_p, encoding="utf-8").read()
        md_ok = "## 一、执行摘要" in txt and "本报告由系统" in txt
    record("Markdown 生成", md_ok, md_p and f"{os.path.getsize(md_p)} bytes")

    # HTML（自包含，图表 base64 内嵌）
    html_p = paths.get("html", "")
    if os.path.exists(html_p):
        h = open(html_p, encoding="utf-8").read()
        n_img = h.count("data:image/png;base64")
        html_ok = "<html" in h and n_img == len(charts) and "Microsoft YaHei" in h
        record("HTML 生成 + 图表内嵌", html_ok,
               f"{len(h.encode('utf-8'))} bytes, base64 图 {n_img}/{len(charts)}")
    else:
        record("HTML 生成 + 图表内嵌", False, "文件不存在")

    # PDF（%PDF 头 + CJK 字体 + 页数）
    pdf_p = paths.get("pdf", "")
    pdf_ok = False
    if os.path.exists(pdf_p) and os.path.getsize(pdf_p) > 50000:
        with open(pdf_p, "rb") as f:
            header = f.read(5)
        pdf_ok = header == b"%PDF-"
    record("PDF 生成（%PDF 头）", pdf_ok, f"{os.path.getsize(pdf_p)} bytes")
    if pdf_ok:
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(pdf_p)
            n = doc.page_count
            first = doc[0].get_text()
            record("PDF 可解析 + 中文文本", n >= 4 and "深度研究报告" in first,
                   f"{n} 页, 首页含中文标题")
        except ImportError:
            print("  （PyMuPDF 未装，跳过页面级校验）")

    return result


# ── B. 对话报告转 HTML/PDF ──────────────────────────────────

def check_chat_report():
    print("\n=== B. 对话报告（Agent 分析 AAPL）→ HTML/PDF ===")
    from app.agent import intent as agent_intent
    from app.agent import planner as agent_planner
    from app.agent import runner as agent_runner
    from app.agent import expressor as agent_expressor

    intent_result = agent_intent.parse("分析一下苹果公司")
    if not intent_result.get("tickers"):
        intent_result = {"intent": "analysis", "tickers": ["AAPL"],
                         "timeframe": "1y", "analysis_type": "comprehensive"}
    plan = agent_planner.build_plan(intent_result)
    exec_result = agent_runner.execute_plan(plan)
    report_md = agent_expressor.generate_report(exec_result, intent_result)

    record("对话 Agent 生成 Markdown 报告", len(report_md) > 200,
           f"{len(report_md)} 字符, 含数据说明")
    print("  报告片段:", report_md[:120].replace("\n", " / "))

    gen = ReportGenerator(output_dir="data/reports")
    paths = gen.from_chat_report(report_md, title=f"AAPL 对话分析")

    html_p = paths.get("html", "")
    pdf_p = paths.get("pdf", "")
    h_ok = os.path.exists(html_p) and "<html" in open(html_p, encoding="utf-8").read()
    record("对话报告 → HTML", h_ok, f"{os.path.getsize(html_p)} bytes" if h_ok else "")
    if os.path.exists(pdf_p):
        with open(pdf_p, "rb") as f:
            p_ok = f.read(5) == b"%PDF-"
        record("对话报告 → PDF", p_ok, f"{os.path.getsize(pdf_p)} bytes")
    else:
        record("对话报告 → PDF", False, "文件不存在")


# ── 主流程 ───────────────────────────────────────────────────

if __name__ == "__main__":
    check_fonts()
    check_deep_report()
    check_chat_report()

    passed = sum(1 for r in _results if r["ok"])
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"Phase 5 结果: {passed}/{total} 通过")
    for r in _results:
        if not r["ok"]:
            print(f"  FAIL: {r['name']} {r['detail']}")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)
