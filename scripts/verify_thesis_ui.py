"""A3 验证：论点台账看板页（无头渲染 + 导航链 + 详情/未检查态）。

Part A —— AppTest 渲染「🧾 论点台账」：列表含证伪红标；详情页能渲染
          「取不到值」警示，而不是把它当未触发。
Part B —— 导航链完整性：MODULES 每项都有分支，无裸 else 静默错路由。
Part C —— api_client 具备台账全部接口。
Part D —— 纯函数：空证伪表 dtype、徽标映射、原因中文覆盖。
Part E —— 活体（可选，后端未起则跳过）。

运行: D:\\python\\python.exe scripts/verify_thesis_ui.py
（Part A–D 全离线：API 用桩替换，不发真实请求）
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name: str, ok, detail: str = ""):
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


THESIS_HOLDING = {
    "id": "t_hold", "ticker": "AAPL", "statement": "服务业务毛利撑住估值",
    "stance": "bullish", "horizon": "12 个月", "key_assumptions": ["服务收入增速 10%"],
    "falsifiers": [{"metric": "valuation_analysis.dcf.fair_value", "op": "<",
                    "value": 1200.0, "note": "认错线"}],
    "status": "active", "conviction": 4, "artifact_ref": "data/artifacts/AAPL/x.json",
    "created_at": "2026-11-06 10:00:00", "updated_at": "2026-11-06 10:00:00",
    "evidence": [{"id": 1, "thesis_id": "t_hold", "ref": "valuation_analysis.dcf.fair_value",
                  "kind": "fact", "direction": "supports", "note": "实测 366.85",
                  "added_at": "2026-11-06 10:01:00"}],
    "reviews": [{"id": 1, "thesis_id": "t_hold", "verdict": "holds",
                 "snapshot": {}, "note": "复核一次", "reviewed_at": "2026-11-06 10:02:00"}],
}

THESIS_BREACHED = {
    "id": "t_breach", "ticker": "600519.SS", "statement": "提价周期未结束",
    "stance": "bullish", "horizon": "18 个月", "key_assumptions": [],
    "falsifiers": [{"metric": "valuation_analysis.dcf.fair_value", "op": ">",
                    "value": 0.0, "note": ""}],
    "status": "active", "conviction": 3, "artifact_ref": "",
    "created_at": "2026-11-06 09:00:00", "updated_at": "2026-11-06 09:00:00",
}

THESIS_UNKNOWN = {
    "id": "t_unknown", "ticker": "0700.HK", "statement": "游戏流水企稳",
    "stance": "neutral", "horizon": "", "key_assumptions": [],
    "falsifiers": [{"metric": "nope.path", "op": ">", "value": 0.0, "note": ""}],
    "status": "active", "conviction": 2, "artifact_ref": "",
    "created_at": "2026-11-06 08:00:00", "updated_at": "2026-11-06 08:00:00",
}

HOLDING_CHECK = {
    "thesis_id": "t_hold", "verdict": "holding", "reason": None,
    "counts": {"total": 1, "breached": 0, "holding": 1, "unknown": 0},
    "results": [{"metric": "valuation_analysis.dcf.fair_value", "op": "<",
                 "threshold": 1200.0, "state": "holding", "observed": 366.85,
                 "unit": "currency", "as_of": None, "provenance": "computed",
                 "reason": None, "resolved_path": "valuation_analysis.dcf.fair_value"}],
    "artifact": "data/artifacts/AAPL/x.json",
    "checked_at": "2026-11-06T10:05:00",
}

BREACH_CHECK = {
    "thesis_id": "t_breach", "verdict": "falsified", "reason": None,
    "counts": {"total": 1, "breached": 1, "holding": 0, "unknown": 0},
    "results": [{"metric": "valuation_analysis.dcf.fair_value", "op": ">",
                 "threshold": 0.0, "state": "breached", "observed": 366.85,
                 "unit": "currency", "as_of": None, "provenance": "computed",
                 "reason": None, "resolved_path": "valuation_analysis.dcf.fair_value"}],
    "artifact": "data/artifacts/AAPL/x.json",
    "checked_at": "2026-11-06T10:05:30",
}

UNKNOWN_CHECK = {
    "thesis_id": "t_unknown", "verdict": "inconclusive", "reason": "unknown_falsifier",
    "counts": {"total": 1, "breached": 0, "holding": 0, "unknown": 1},
    "results": [{"metric": "nope.path", "op": ">", "threshold": 0.0,
                 "state": "unknown", "observed": None, "unit": None, "as_of": None,
                 "provenance": None, "reason": "path_not_in_ledger",
                 "resolved_path": None}],
    "artifact": None, "artifact_reason": "no_artifact_ref",
    "checked_at": "2026-11-06T10:06:00",
}


def _patch_api():
    """把台账接口换成桩：离线渲染，不发真实请求。"""
    from ui import api_client as api

    api.thesis_list = lambda **kw: [
        {**THESIS_BREACHED, "check": BREACH_CHECK},
        {**THESIS_UNKNOWN, "check": UNKNOWN_CHECK},
        {**THESIS_HOLDING, "check": HOLDING_CHECK},
    ]
    api.thesis_artifacts = lambda ticker="": [
        {"ticker": "AAPL", "name": "20261105T002222.json",
         "ref": "data/artifacts/AAPL/20261105T002222.json",
         "modified": "2026-11-05 00:22", "size_kb": 812.0},
    ]
    _by_id = {"t_hold": THESIS_HOLDING, "t_unknown": THESIS_UNKNOWN,
              "t_breach": THESIS_BREACHED}
    api.thesis_get = lambda tid: dict(_by_id[tid])


# ── Part A：AppTest 渲染 ───────────────────────────────

def check_render():
    print("\n=== Part A: 看板「🧾 论点台账」渲染 ===")
    try:
        from streamlit.testing.v1 import AppTest
    except Exception as e:                          # noqa: BLE001
        check("streamlit AppTest 可用", False, f"import 失败：{e}")
        return

    _patch_api()
    from ui.pages.thesis import _list
    _list.clear()

    at = AppTest.from_file(str(ROOT / "ui" / "dashboard.py"), default_timeout=240)
    try:
        at.run(timeout=240)
        labels = [str(o) for o in (at.radio[0].options or [])] if at.radio else []
        if "🧾 论点台账" not in labels:
            check("台账模块出现在导航里", False, str(labels))
            return
        check("台账模块出现在导航里", True, f"共 {len(labels)} 个模块")
        at.radio[0].set_value("🧾 论点台账")
        at.run(timeout=240)
    except Exception as e:                          # noqa: BLE001
        check("台账页渲染无异常", False, f"exception={e}")
        return
    if at.exception:
        check("台账页渲染无异常", False, f"exception={at.exception}")
        return
    check("台账页渲染无异常", True, "AppTest 运行无 exception")

    texts = [str(getattr(el, "value", "") or "")
             for attr in ("markdown", "caption", "info", "warning", "error",
                          "success", "subheader")
             for el in (getattr(at, attr, []) or [])]
    blob = "\n".join(texts)
    check("列表渲染出三条论点",
          all(s in blob for s in ("服务业务毛利撑住估值", "提价周期未结束", "游戏流水企稳")))
    check("证伪条目被红标并置顶",
          "证伪条件已被真实事实触发" in blob and blob.index("提价周期未结束")
          < blob.index("游戏流水企稳") < blob.index("服务业务毛利撑住估值"))
    check("列表同时给出三种结论",
          "证伪条件未触发" in blob and "无法判定" in blob and "证伪条件已触发" in blob)
    check("页面写明诚实边界", "查不到" in blob and "没事" in blob)
    check("新建页提供产物草稿入口", "生成草稿" in blob)
    check("表单字段齐全",
          len(at.text_input or []) >= 3 and len(at.text_area or []) >= 2
          and len(at.slider or []) >= 1,
          f"input={len(at.text_input or [])} area={len(at.text_area or [])} "
          f"slider={len(at.slider or [])}")

    # 详情：取不到值必须是警示，不能显示成「未触发」
    at.session_state["thesis_sel"] = "t_hold"
    try:
        at.run(timeout=240)
    except Exception as e:                          # noqa: BLE001
        check("详情页渲染无异常", False, f"exception={e}")
        return
    if at.exception:
        check("详情页渲染无异常", False, f"exception={at.exception}")
        return
    detail = "\n".join(str(getattr(el, "value", "") or "")
                       for attr in ("markdown", "caption", "info", "warning", "error")
                       for el in (getattr(at, attr, []) or []))
    check("详情页渲染无异常", True)
    check("详情展示论点全文与假设",
          "服务业务毛利撑住估值" in detail and "服务收入增速 10%" in detail)
    check("详情展示证据与复盘",
          "实测 366.85" in detail and "复核一次" in detail)

    # 点「运行证伪检查」：桩返回 unknown，页面必须警示「取不到值 ≠ 未触发」
    from ui.pages.thesis import _render_detail  # noqa: F401  仅确认可导入
    from ui import api_client as api
    api.thesis_check = lambda tid, artifact_ref=None: UNKNOWN_CHECK
    idx = next((i for i, b in enumerate(at.button)
                if "运行证伪检查" in str(getattr(b, "label", ""))), None)
    if idx is None:
        check("详情有「运行证伪检查」按钮", False)
        return
    at.button[idx].click()
    at.run(timeout=240)
    if at.exception:
        check("运行证伪检查后渲染无异常", False, f"exception={at.exception}")
        return
    after = "\n".join(str(getattr(el, "value", "") or "")
                      for attr in ("markdown", "caption", "info", "warning", "error")
                      for el in (getattr(at, attr, []) or []))
    check("运行证伪检查后渲染无异常", True)
    res = at.session_state["chk_res_t_hold"]
    check("点击后结果进 session_state（复用不重算）",
          isinstance(res, dict) and res.get("verdict") == "inconclusive", str(res)[:60])
    check("未知态显示为「无法判定」而不是未触发", "无法判定" in after)
    check("未知态给出「取不到值」警示", "取不到值" in after)


# ── Part B：导航链完整性 ───────────────────────────────

def check_nav():
    print("\n=== Part B: 导航链完整性（防裸 else 静默错路由）===")
    src = io.open(ROOT / "ui" / "dashboard.py", encoding="utf-8").read()
    from ui.dashboard import MODULES
    branches = src.count("nav == MODULES[")
    check("MODULES 每一项都有分支", branches == len(MODULES),
          f"模块 {len(MODULES)} 个，分支 {branches} 个")
    check("台账在 MODULES 里", "🧾 论点台账" in MODULES, str(MODULES[-1]))
    check("没有裸 else 兜底页面", "未注册的模块" in src)
    check("MODULES 无重复", len(set(MODULES)) == len(MODULES))


# ── Part C：api_client 接口 ────────────────────────────

def check_wiring():
    print("\n=== Part C: api_client 台账接口 ===")
    from ui import api_client as api
    need = ["thesis_list", "thesis_get", "thesis_create", "thesis_update",
            "thesis_delete", "thesis_check", "thesis_artifacts", "thesis_draft",
            "thesis_add_evidence", "thesis_delete_evidence", "thesis_add_review"]
    missing = [n for n in need if not callable(getattr(api, n, None))]
    check("api_client 具备台账全部接口", not missing, str(missing))


# ── Part D：纯函数 ────────────────────────────────────

def check_pure():
    print("\n=== Part D: 纯函数与文案覆盖 ===")
    import pandas as pd

    from ui.pages.thesis import (
        _badge, _empty_falsifiers, _rows_to_falsifiers,
        REASON_LABELS, STATE_LABELS, VERDICT_LABELS,
    )

    rows = pd.DataFrame([
        {"metric": "valuation_analysis.dcf.fair_value", "op": "<",
         "value": 1200.0, "note": "认错线"},
        {"metric": "  ", "op": ">", "value": 1.0, "note": "空指标应丢弃"},
        {"metric": "financial_analysis.ratios.GROSS_MARGIN[].value", "op": None,
         "value": 0.5, "note": None},
    ])
    parsed = _rows_to_falsifiers(rows)
    check("编辑器行解析：丢弃空指标", len(parsed) == 2, str(parsed))
    check("编辑器行解析：保留阈值与备注",
          parsed[0] == {"metric": "valuation_analysis.dcf.fair_value", "op": "<",
                        "value": 1200.0, "note": "认错线"}, str(parsed[0]))
    check("编辑器行解析：缺 op 不静默丢掉该行",
          parsed[1]["op"] == "<" and parsed[1]["note"] == ""
          and parsed[1]["value"] == 0.5
          and "[]" in parsed[1]["metric"], str(parsed[1]))
    check("空表解析为空列表", _rows_to_falsifiers(_empty_falsifiers()) == [])

    # pandas 会把空单元变成 NaN，str(NaN) == "nan" —— 不能当值用
    nan_rows = pd.DataFrame([
        {"metric": "valuation_analysis.dcf.fair_value", "op": float("nan"),
         "value": float("nan"), "note": float("nan")},
    ])
    nan_parsed = _rows_to_falsifiers(nan_rows)
    check("NaN 单元不变成 \"nan\" 字符串",
          nan_parsed[0]["op"] == "<" and nan_parsed[0]["note"] == ""
          and nan_parsed[0]["value"] is None, str(nan_parsed))

    df = _empty_falsifiers()
    check("空证伪表 dtype 正确（NumberColumn 可识别）",
          str(df["value"].dtype) == "float64" and list(df.columns) ==
          ["metric", "op", "value", "note"], str(df.dtypes.to_dict()))
    check("徽标覆盖三态",
          _badge({"verdict": "falsified"}).startswith("🔴")
          and _badge({"verdict": "holding"}).startswith("🟢")
          and _badge({"verdict": "inconclusive"}).startswith("⚪")
          and _badge(None).startswith("⚫"))
    check("判定文案齐全", set(VERDICT_LABELS) == {"falsified", "holding", "inconclusive"})
    check("逐条状态文案齐全", set(STATE_LABELS) == {"breached", "holding", "unknown"})

    from app.thesis.checks import UNKNOWN
    # checks.py 里 unknown 的所有 reason 都必须有中文解释，否则用户看到的是英文码
    from app.thesis import checks as ck
    src = io.open(ROOT / "app" / "thesis" / "checks.py", encoding="utf-8").read()
    reasons = {s for s in ("invalid_threshold", "no_ledger", "path_not_in_ledger",
                           "ledger_unknown", "non_finite") if f'"{s}"' in src}
    check("checks 的 unknown 原因全部被 UI 解释",
          reasons <= set(REASON_LABELS), str(reasons - set(REASON_LABELS)))
    check("UNKNOWN 常量确实为 unknown", UNKNOWN == "unknown")


# ── Part E：活体（可选）───────────────────────────────

def check_live():
    print("\n=== Part E: 活体 curl（可选；未启动则跳过，不计入结果）===")
    url = "http://127.0.0.1:8000/api/v1/thesis/nonexistent"
    try:
        p = subprocess.run(["curl", "-s", "--max-time", "20", "-o", "/dev/null",
                            "-w", "%{http_code}", url],
                           capture_output=True, timeout=30)
        code = (p.stdout or b"").decode().strip()
    except Exception as e:                          # noqa: BLE001
        print(f"[SKIP] 活体：后端未启动或网络不可达（{type(e).__name__}），如实跳过")
        return
    if not code or code == "000":
        print("[SKIP] 活体：后端未启动（curl HTTP 000 / 空），如实跳过")
        return
    check("活体：台账路由已挂载（未知 id 返回 404）", code == "404", f"HTTP {code}")


def main():
    check_render()
    check_nav()
    check_wiring()
    check_pure()
    check_live()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
