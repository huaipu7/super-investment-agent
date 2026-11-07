"""Phase 3 交互增强离线验证（无需后端 / 网络）。

覆盖：
- report_diff.split_sections：preamble/章节拆分，`### ` 子标题不误切
- report_diff.render_section_diff / diff_reports：变更行/章节统计 + 高亮 HTML
- valuation.recompute_dcf_from_result：重算 fair_value 与手工 DCF 一致、
  WACC 覆写/回退估算、缺失数据降级、敏感性矩阵
- export.financial_xlsx / backtest_single_xlsx：xlsx bytes、sheet 名、缺失 None

运行: D:\\python\\python.exe scripts\\verify_phase3.py
退出码: 0 = 全部通过；1 = 存在失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.analysis.valuation import DCFModel, recompute_dcf_from_result

_PASS = 0
_FAIL = 0


def _ok(name: str, cond: bool, detail: str = ""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# ── 合成数据 ─────────────────────────────────────────────

def _synthetic_result() -> dict:
    return {
        "ticker": "TEST",
        "company_info": {"shares_outstanding": 100.0, "name": "测试"},
        "financial_analysis": {
            "fcf_history": [80.0, 90.0, 105.0, 118.0, 130.0],
            # 现价与自身 DCF 内在价值同量级：原值 8.0 会得到 ~69.5 的内在价值
            # （8.7 倍），触发 valuation.FAIR_VALUE_PRICE_BAND 降级，属夹具不自洽
            "current_price": 40.0,
            "raw_metrics": {"NET_DEBT": [{"period": "2024", "value": 50.0}]},
        },
        "valuation_analysis": {
            "dcf": {"wacc_used": 0.09, "fair_value": 40.0,
                    "terminal_growth": 0.03, "projection_years": 5},
            "estimates": {
                "risk_free_rate": {"value": 0.045, "source": "test"},
                "beta": {"value": 1.2, "source": "test"},
                "erp": {"value": 0.05, "source": "test"},
                "debt_ratio": {"value": 0.2, "source": "test"},
                "cost_of_debt": {"value": 0.04, "source": "test"},
            },
        },
    }


def _synthetic_fin_result() -> dict:
    return {
        "ticker": "TEST",
        "company_info": {"shares_outstanding": 100.0},
        "financial_analysis": {
            "raw_metrics": {
                "REVENUE": [{"period": "2022", "value": 100.0},
                            {"period": "2023", "value": 120.0}],
                "NET_INCOME": [{"period": "2022", "value": 10.0},
                               {"period": "2023", "value": 14.0}],
            },
            "ratios": {
                "ROE": [{"period": "2022", "value": 0.12},
                        {"period": "2023", "value": 0.14}],
                "GROSS_MARGIN": [{"period": "2022", "value": 0.4},
                                 {"period": "2023", "value": 0.42}],
            },
            "growth_rates": {
                "REVENUE_GROWTH": [{"period": "2022", "value": None},
                                   {"period": "2023", "value": 0.2}],
            },
            "dupont_breakdown": [{"period": "2023", "net_margin": 0.12,
                                  "asset_turnover": 0.6, "equity_multiplier": 2.0,
                                  "roe": 0.144}],
            "roic": [{"period": "2023", "nopat": 8.0, "invested_capital": 60.0,
                      "roic": 0.133}],
        },
        "valuation_analysis": {
            "dcf": {"fair_value": 8.0, "upside": 0.0, "wacc_used": 0.09,
                    "terminal_growth": 0.03, "projection_years": 5,
                    "fair_value_ev": 850.0,
                    "sensitivity": {"wacc_values": [0.07, 0.08, 0.09, 0.10, 0.11],
                                    "growth_values": [0.015, 0.0225, 0.03, 0.0375, 0.045],
                                    "matrix": [[8.5, 8.7, 8.9, 9.1, 9.3],
                                               [8.0, 8.2, 8.4, 8.6, 8.8],
                                               [7.5, 7.7, 7.9, 8.1, 8.3],
                                               [7.1, 7.3, 7.5, 7.7, 7.9],
                                               [6.7, 6.9, 7.1, 7.3, 7.5]]}},
            "relative_valuation": {"pe": {"company": 20.0, "industry_avg": 18.0,
                                          "premium": 0.111},
                                   "pb": {"company": 3.0, "industry_avg": 2.5,
                                          "premium": 0.2}},
        },
    }


# ── 断言组 ───────────────────────────────────────────────

def _test_split_sections():
    print("== report_diff.split_sections ==")
    from ui import report_diff as rd
    md = "# 标题\n> 生成时间\n## 一、摘要\n正文A\n### 子标题\n子内容\n## 二、财务\n正文B"
    preamble, secs = rd.split_sections(md)
    _ok("preamble 保留头部", "生成时间" in preamble)
    _ok("章节数=2", len(secs) == 2, f"n={len(secs)}")
    _ok("标题正确", [t for t, _ in secs] == ["一、摘要", "二、财务"])
    _ok("### 子标题未误切", secs[0][1].count("子标题") == 1 and secs[0][1].count("子内容") == 1)
    _ok("空 md 安全", rd.split_sections("")[1] == [] and rd.split_sections(None)[1] == [])


def _test_render_diff():
    print("== report_diff.render_section_diff ==")
    from ui import report_diff as rd
    h = rd.render_section_diff("DCF 内在价值 8.00", "DCF 内在价值 9.50")
    _ok("字符级高亮命中", "8" in h and "9" in h
        and "background:#fdecea" in h and "background:#e8f5e9" in h)
    _ok("无差异返回空", rd.render_section_diff("相同内容", "相同内容") == "")
    _ok("整段新增", rd.render_section_diff("", "新增一行") != "")
    _ok("HTML 转义", "<script>" not in rd.render_section_diff("<script>", "ok"))


def _test_diff_reports():
    print("== report_diff.diff_reports ==")
    from ui import report_diff as rd
    old = "# 报告\n\n## 一、摘要\nDCF 内在价值 8.00\n\n## 二、财务\nROE 12%\n"
    new = "# 报告\n\n## 一、摘要\nDCF 内在价值 9.50\n\n## 二、财务\nROE 12%\n\n## 三、新增章节\n新增内容\n"
    d = rd.diff_reports(old, new)
    st = d["stats"]
    _ok("章节总数=3", st["sections_total"] == 3, f"n={st['sections_total']}")
    _ok("变动章节=2", st["sections_changed"] == 2, f"n={st['sections_changed']}")
    _ok("新增行=2", st["added"] == 2, f"n={st['added']}")
    _ok("删除行=1", st["removed"] == 1, f"n={st['removed']}")
    by_title = {s["title"]: s for s in d["sections"]}
    _ok("新增章节标记", by_title["三、新增章节"]["added"] is True)
    _ok("摘要章节有 html", by_title["一、摘要"]["changed"] and by_title["一、摘要"]["html"] != "")
    _ok("财务章节未变无 html", by_title["二、财务"]["changed"] is False and by_title["二、财务"]["html"] == "")
    # 完全相同 → 零差异
    d2 = rd.diff_reports(old, old)
    _ok("相同报告零差异", d2["stats"]["added"] == 0 and d2["stats"]["removed"] == 0)


def _test_recompute_dcf():
    print("== recompute_dcf_from_result ==")
    res = _synthetic_result()
    dm = DCFModel()
    d = dm.compute_dcf([80.0, 90.0, 105.0, 118.0, 130.0], 0.09, terminal_growth=0.03)
    expect_fv = (d["fair_value_ev"] - 50.0) / 100.0

    out = recompute_dcf_from_result(res, wacc=0.09)
    _ok("available", out.get("available") is True)
    _ok("fair_value 与手工一致", abs(out["fair_value"] - expect_fv) < 0.05,
        f"got={out['fair_value']} expect={expect_fv:.2f}")
    expect_upside = (expect_fv - 40.0) / 40.0
    _ok("upside 计算", abs(out["upside"] - expect_upside) < 1e-5,
        f"got={out['upside']} expect={expect_upside:.6f}")
    _ok("敏感性矩阵 5×5", len(out["sensitivity"]["matrix"]) == 5)
    _ok("wacc_used 回传", abs(out["wacc_used"] - 0.09) < 1e-9)

    # wacc=None 时用估算重算（wd=0.2, kd=0.04, ke=0.045+1.2*0.05=0.105）
    out2 = recompute_dcf_from_result(res, wacc=None)
    ke = 0.045 + 1.2 * 0.05
    expect_wacc = 0.8 * ke + 0.2 * 0.04 * (1 - 0.21)
    _ok("wacc 从估算重算", abs(out2["wacc_used"] - expect_wacc) < 1e-9,
        f"got={out2['wacc_used']} expect={expect_wacc}")
    _ok("估算重算 fair_value 有限", out2["fair_value"] > 0)

    # 显式 beta 覆写
    out3 = recompute_dcf_from_result(res, wacc=None, beta=1.5)
    _ok("beta 覆写改变 wacc", abs(out3["wacc_used"] - expect_wacc) > 1e-6)

    # 缺失/坏数据降级
    _ok("None → 不可用", recompute_dcf_from_result(None)["available"] is False)
    bad = {"financial_analysis": {"fcf_history": [1.0]},
           "company_info": {"shares_outstanding": 100.0}}
    _ok("FCF 不足 → 不可用", recompute_dcf_from_result(bad)["available"] is False)
    neg = _synthetic_result()
    neg["financial_analysis"]["fcf_history"] = [80.0, 90.0, -5.0]
    _ok("FCF 负值 → 不可用", recompute_dcf_from_result(neg)["available"] is False)
    noshares = _synthetic_result()
    noshares["company_info"]["shares_outstanding"] = None
    _ok("缺股本 → 不可用", recompute_dcf_from_result(noshares)["available"] is False)


def _test_export_xlsx():
    print("== export.financial_xlsx / backtest ==")
    import io
    import zipfile

    from ui import export as ex
    res = _synthetic_fin_result()

    data = ex.financial_xlsx(res)
    _ok("xlsx 非空", data is not None and len(data) > 0)
    _ok("合法 zip (xlsx)", data is not None and zipfile.is_zipfile(io.BytesIO(data)))
    if data:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
        names = set(wb.sheetnames)
        _ok("含财务规模 sheet", "财务规模" in names, f"sheets={wb.sheetnames}")
        _ok("含 DCF敏感性 sheet", "DCF敏感性" in names)
        _ok("含杜邦分解 sheet", "杜邦分解" in names)
        ws = wb["财务规模"]
        rows = list(ws.iter_rows(values_only=True))
        _ok("财务规模带表头+期间列", len(rows) >= 2 and rows[0][0] == "期间",
            f"first row={rows[0][:3] if rows else '?'}")
        wb.close()

    _ok("空结果 → None", ex.financial_xlsx({}) is None)

    class _Mock:
        def __init__(self, d):
            self._d = d
        def model_dump(self, mode="json"):
            return self._d

    bt = _Mock({
        "metrics": {"total_return_pct": 12.5, "max_drawdown_pct": -8.0,
                    "sharpe_ratio": 1.2, "num_trades": 3},
        "trades": [{"entry_date": "2026-01-20", "exit_date": "2026-02-10",
                    "entry_price": 100.0, "exit_price": 110.0, "pnl_pct": 10.0}],
        "traces": [],
    })
    bd = ex.backtest_single_xlsx(_Mock(bt.model_dump("json") if hasattr(bt, "model_dump") else bt._d))
    _ok("回测导出非空", bd is not None and zipfile.is_zipfile(io.BytesIO(bd)))
    _ok("回测空对象 → None", ex.backtest_single_xlsx(_Mock({})) is None)

    br = _Mock({
        "runs": [{"period": 1, "symbol": "AAPL", "cutoff_date": "2025-06-30",
                  "metrics": {"total_return_pct": 5.0}}],
        "stats": {"n_periods": 1, "wins": 1, "losses": 0},
        "warnings": [],
    })
    bbd = ex.backtest_batch_xlsx(_Mock(br.model_dump("json") if hasattr(br, "model_dump") else br._d))
    _ok("批量导出非空", bbd is not None and zipfile.is_zipfile(io.BytesIO(bbd)))


def _test_integration():
    print("== research 页接入（纯导入 + 章节渲染） ==")
    from ui import report_diff as rd
    md = "## 一、A\nx\n## 二、B\ny"
    preamble, secs = rd.split_sections(md)
    _ok("research 页章节拆分", len(secs) == 2)
    # research 页内嵌的 TOC/diff/recalc 函数已导入成功（本脚本 import 了 research 模块）
    import ui.pages.research as rp
    _ok("TOC/diff/recalc 函数存在",
        callable(rp._render_md_with_toc) and callable(rp._render_report_diff)
        and callable(rp._render_dcf_recalc))


def main():
    print("=" * 60)
    print("verify_phase3.py — Phase 3 交互增强（目录导航 / DCF重算 / diff / Excel）")
    print("=" * 60)
    _test_split_sections()
    print()
    _test_render_diff()
    print()
    _test_diff_reports()
    print()
    _test_recompute_dcf()
    print()
    _test_export_xlsx()
    print()
    _test_integration()
    print()
    print(f"结果: {_PASS} 通过 / {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
