"""调查 UI 验证：看板页渲染 + 导航链完整性 + 档位上限可见 + api_client 接口。

Part A —— AppTest 无头渲染「🧭 自主深度调查」页：导航 → 断言无异常、
         表单（代码/档位/报表口径/开始按钮）与诚实边界说明都在。
Part B —— 导航链完整性：MODULES 每一项都必须有对应分支（防止裸 else 静默错路由）。
Part C —— 档位标签把真实上限写出来（barrier 对用户可见）。
Part D —— 状态展示表覆盖四种假设状态；api_client 具备调查接口。
Part E —— 活体 curl（可选，后端未起则跳过，不计入结果）。

运行: D:\\python\\python.exe scripts\\verify_investigation_ui.py
（Part A–D 全离线；Part E 需后端已启动: D:\\python\\python.exe api/main.py）
"""
from __future__ import annotations

import io
import json
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


# ── Part A：AppTest 渲染 ───────────────────────────────

def check_render():
    print("\n=== Part A: 看板「🧭 自主深度调查」渲染 ===")
    try:
        from streamlit.testing.v1 import AppTest
    except Exception as e:                          # noqa: BLE001
        check("streamlit AppTest 可用", False, f"import 失败：{e}")
        return

    at = AppTest.from_file(str(ROOT / "ui" / "dashboard.py"), default_timeout=240)
    try:
        at.run(timeout=240)
        labels = [str(o) for o in (at.radio[0].options or [])] if at.radio else []
        if "🧭 自主深度调查" not in labels:
            check("新模块出现在导航里", False, str(labels))
            return
        check("新模块出现在导航里", True, f"共 {len(labels)} 个模块")
        at.radio[0].set_value("🧭 自主深度调查")
        at.run(timeout=240)
    except Exception as e:                          # noqa: BLE001
        check("调查页渲染无异常", False, f"exception={e}")
        return
    if at.exception:
        check("调查页渲染无异常", False, f"exception={at.exception}")
        return
    check("调查页渲染无异常", True, "AppTest 运行无 exception")

    texts = []
    for attr in ("markdown", "caption", "info", "warning", "subheader", "title"):
        for el in getattr(at, attr, []) or []:
            texts.append(str(getattr(el, "value", "") or ""))
    blob = "\n".join(texts)
    check("页面写明诚实边界（结果是假设、证据不足就如实说）",
          "假设" in blob and "证据不足" in blob)
    check("页面说明有硬预算", "硬预算" in blob or "上限" in blob)
    # 档位上限写在 radio 的选项文字里，不在 markdown 元素里——去 radio 选项上找
    depth_opts = [str(o) for r in (at.radio or []) for o in (r.options or [])]
    check("档位标签里写出了真实上限（barrier 可见）",
          any(("轮" in o and "次工具" in o and "秒" in o) for o in depth_opts),
          str([o for o in depth_opts if "轮" in o]))
    check("表单齐全（代码/档位/口径/开始）",
          len(getattr(at, "text_input", []) or []) >= 1
          and len(getattr(at, "radio", []) or []) >= 1
          and len(getattr(at, "selectbox", []) or []) >= 1
          and len(getattr(at, "button", []) or []) >= 1,
          f"input={len(at.text_input or [])} radio={len(at.radio or [])} "
          f"select={len(at.selectbox or [])} button={len(at.button or [])}")


# ── Part B：导航链完整性 ───────────────────────────────

def check_nav():
    print("\n=== Part B: 导航链完整性（防裸 else 静默错路由）===")
    src = io.open(ROOT / "ui" / "dashboard.py", encoding="utf-8").read()
    n_modules = src.count('", "') + 1 if "MODULES = [" in src else 0
    # 更可靠：真正 import 一次 MODULES
    from ui.dashboard import MODULES
    branches = src.count("nav == MODULES[")
    check("MODULES 每一项都有分支",
          branches == len(MODULES),
          f"模块 {len(MODULES)} 个，分支 {branches} 个")
    check("没有裸 else 兜底页面（改为显式报错）",
          "else:\n    # 刻意不写兜底页面" in src or "未注册的模块" in src)
    check("新模块在 MODULES 里",
          any("自主深度调查" in m for m in MODULES), str(MODULES[-1]))
    check("MODULES 数量与索引一致（无重复项）",
          len(set(MODULES)) == len(MODULES), str(len(MODULES)))


# ── Part C：档位上限可见 ───────────────────────────────

def check_depth_labels():
    print("\n=== Part C: 档位标签写出真实上限 ===")
    from app.config import get_settings
    from ui.pages.investigation import _depth_labels
    s = get_settings()
    labels, back = _depth_labels()
    check("两个档位都在，且可反查 key",
          set(back.values()) == {"standard", "deep"}, str(sorted(back.values())))
    std = labels["standard"]
    deep = labels["deep"]
    check("标准档标签含真实轮次/工具/LLM/秒上限",
          all(str(getattr(s, f"INVESTIGATION_STANDARD_{k}")) in std
              for k in ("MAX_ROUNDS", "MAX_TOOL_CALLS", "MAX_LLM_CALLS",
                        "DEADLINE_SECONDS")), std)
    check("深度档标签含真实上限", all(
        str(getattr(s, f"INVESTIGATION_DEEP_{k}")) in deep
        for k in ("MAX_ROUNDS", "MAX_TOOL_CALLS", "MAX_LLM_CALLS",
                  "DEADLINE_SECONDS")), deep)


# ── Part D：状态展示 + api_client ──────────────────────

def check_wiring():
    print("\n=== Part D: 状态展示与 api_client 接口 ===")
    from app.investigation.hypotheses import (
        STATUS_INSUFFICIENT, STATUS_PARTIAL, STATUS_UNVERIFIED, STATUS_VERIFIED,
    )
    from app.investigation.report import STATUS_DISPLAY, STOP_DISPLAY
    check("四种假设状态都有展示标签与配色",
          {STATUS_VERIFIED, STATUS_PARTIAL, STATUS_UNVERIFIED, STATUS_INSUFFICIENT}
          <= set(STATUS_DISPLAY)
          and all(v.get("label") and v.get("color") for v in STATUS_DISPLAY.values()),
          str({k: v["label"] for k, v in STATUS_DISPLAY.items()}))
    check("停止原因都有中文说明", all(STOP_DISPLAY.values()) and len(STOP_DISPLAY) >= 9,
          f"{len(STOP_DISPLAY)} 条")

    from ui import api_client as api
    need = ["investigation_run", "investigation_run_sync", "investigation_status",
            "investigation_result", "investigation_result_by_ticker",
            "investigation_report_md", "investigation_stream"]
    missing = [n for n in need if not callable(getattr(api, n, None))]
    check("api_client 具备调查全部接口", not missing, str(missing))

    # 幂等渲染：报告渲染函数对空报告也不该崩
    from ui.pages.investigation import _fmt
    check("缺失数值在 UI 渲染为 —（不填 0）",
          _fmt(None) == "—" and _fmt(float("nan")) == "—" and _fmt(1.5) == "1.5",
          f"{_fmt(None)} {_fmt(float('nan'))} {_fmt(1.5)}")


# ── Part E：活体（可选）───────────────────────────────

def check_live():
    print("\n=== Part E: 活体 curl（可选；未启动则跳过，不计入结果）===")
    url = "http://127.0.0.1:8000/api/v1/investigation/status/nonexistent"
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
    check("活体：调查路由已挂载（未知 run_id 返回 404）", code == "404", f"HTTP {code}")


def main():
    check_render()
    check_nav()
    check_depth_labels()
    check_wiring()
    check_live()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
