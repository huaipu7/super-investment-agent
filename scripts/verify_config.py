"""Phase A2 验证：配置分层（.env → .env.{ENV} → .env.local）。

运行: D:\\python\\python.exe scripts\\verify_config.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.config as cfg  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _fresh_settings(tmp: Path) -> "cfg.Settings":
    """用临时目录重建 Settings（清受测变量 + 清缓存 + 重载 env 链）。"""
    for k in ("LLM_MODEL", "SERVER_PORT", "LLM_API_KEY"):
        os.environ.pop(k, None)  # 真实 .env 已在 import 时注入，须清掉才能测分层
    cfg._load_env_files()
    cfg.get_settings.cache_clear()
    return cfg.Settings()


def main():
    prev_env = os.environ.get("ENV")
    try:
        # ── 场景 1：ENV=staging，三层都存在 → .env.local 最高，staging 中层生效 ──
        tmp = Path(tempfile.mkdtemp(prefix="verify_cfg_"))
        (tmp / ".env").write_text("LLM_MODEL=from_base\nSERVER_PORT=8000\n", encoding="utf-8")
        (tmp / ".env.staging").write_text("LLM_MODEL=from_staging\nSERVER_PORT=9000\n",
                                          encoding="utf-8")
        (tmp / ".env.local").write_text("LLM_MODEL=from_local\n", encoding="utf-8")

        os.environ["ENV"] = "staging"
        with mock.patch.object(cfg, "BASE_DIR", tmp):
            s = _fresh_settings(tmp)
        check("staging: ENV 读取", s.ENV == "staging", s.ENV)
        check("staging: .env.local 最高优先级", s.LLM_MODEL == "from_local", s.LLM_MODEL)
        check("staging: .env.{ENV} 覆盖 .env", s.SERVER_PORT == 9000, str(s.SERVER_PORT))

        # ── 场景 2：ENV=prod，无 .env.prod 且无 .env.local → 回退 .env ──
        (tmp / ".env.local").unlink()
        os.environ["ENV"] = "prod"
        with mock.patch.object(cfg, "BASE_DIR", tmp):
            s = _fresh_settings(tmp)
        check("prod: ENV=prod 无文件不崩", s.ENV == "prod")
        check("prod: 回退 .env 值", s.LLM_MODEL == "from_base", s.LLM_MODEL)

        # ── 场景 3：无任何 .env（空目录）→ 用默认值 ──
        tmp2 = Path(tempfile.mkdtemp(prefix="verify_cfg_empty_"))
        os.environ["ENV"] = "dev"
        with mock.patch.object(cfg, "BASE_DIR", tmp2):
            s = _fresh_settings(tmp2)
        check("空目录: 走默认值", s.LLM_MODEL == "deepseek-v4-flash", s.LLM_MODEL)
        check("默认 ENV=dev", s.ENV == "dev")

        # ── 场景 4：新配置字段与路径属性 ──
        with mock.patch.object(cfg, "BASE_DIR", tmp):
            s = _fresh_settings(tmp)
            check("AUDIT_DB_PATH 字段", s.AUDIT_DB_PATH == "data/audit.db",
                  s.AUDIT_DB_PATH)
            check("audit_db 相对路径属性",
                  s.audit_db == tmp / "data/audit.db", str(s.audit_db))
            check("COMPLIANCE_DB_PATH 字段", s.COMPLIANCE_DB_PATH == "data/compliance.db",
                  s.COMPLIANCE_DB_PATH)

        # ── 场景 5：自主深度调查预算（硬天花板，默认即上限）──
        os.environ["ENV"] = "dev"
        with mock.patch.object(cfg, "BASE_DIR", tmp2):
            s = _fresh_settings(tmp2)
            check("调查：ENABLED 默认开", s.INVESTIGATION_ENABLED is True)
            check("调查：标准档轮次上限", s.INVESTIGATION_STANDARD_MAX_ROUNDS == 6,
                  str(s.INVESTIGATION_STANDARD_MAX_ROUNDS))
            check("调查：标准档工具调用上限", s.INVESTIGATION_STANDARD_MAX_TOOL_CALLS == 14,
                  str(s.INVESTIGATION_STANDARD_MAX_TOOL_CALLS))
            check("调查：标准档 LLM 调用上限", s.INVESTIGATION_STANDARD_MAX_LLM_CALLS == 10,
                  str(s.INVESTIGATION_STANDARD_MAX_LLM_CALLS))
            check("调查：标准档 deadline", s.INVESTIGATION_STANDARD_DEADLINE_SECONDS == 300,
                  str(s.INVESTIGATION_STANDARD_DEADLINE_SECONDS))
            check("调查：深度档轮次上限", s.INVESTIGATION_DEEP_MAX_ROUNDS == 12,
                  str(s.INVESTIGATION_DEEP_MAX_ROUNDS))
            check("调查：深度档 LLM 调用上限", s.INVESTIGATION_DEEP_MAX_LLM_CALLS == 20,
                  str(s.INVESTIGATION_DEEP_MAX_LLM_CALLS))
            check("调查：深度档 deadline 小于 SSE 兜底 900",
                  s.INVESTIGATION_DEEP_DEADLINE_SECONDS < 900,
                  str(s.INVESTIGATION_DEEP_DEADLINE_SECONDS))
            check("调查：深档单假设证据上限", s.INVESTIGATION_DEEP_MAX_EVIDENCE_PER_HYPOTHESIS == 6,
                  str(s.INVESTIGATION_DEEP_MAX_EVIDENCE_PER_HYPOTHESIS))
            check("调查：深档 peer 上限", s.INVESTIGATION_DEEP_MAX_PEERS == 5,
                  str(s.INVESTIGATION_DEEP_MAX_PEERS))
            check("调查：无进展窗口", s.INVESTIGATION_NO_PROGRESS_WINDOW == 3,
                  str(s.INVESTIGATION_NO_PROGRESS_WINDOW))
            check("调查：最小轮次秒数", s.INVESTIGATION_MIN_ROUND_SECONDS == 60,
                  str(s.INVESTIGATION_MIN_ROUND_SECONDS))
            check("调查：深档上限严格大于标准档",
                  s.INVESTIGATION_DEEP_MAX_ROUNDS > s.INVESTIGATION_STANDARD_MAX_ROUNDS
                  and s.INVESTIGATION_DEEP_MAX_TOOL_CALLS > s.INVESTIGATION_STANDARD_MAX_TOOL_CALLS
                  and s.INVESTIGATION_DEEP_MAX_LLM_CALLS > s.INVESTIGATION_STANDARD_MAX_LLM_CALLS,
                  "轮次/工具/LLM 三档均递增")

        # ── 场景 5b：环境变量可覆盖 ──
        os.environ["INVESTIGATION_STANDARD_MAX_ROUNDS"] = "3"
        try:
            with mock.patch.object(cfg, "BASE_DIR", tmp2):
                s = _fresh_settings(tmp2)
            check("调查：配置可被 env 覆盖", s.INVESTIGATION_STANDARD_MAX_ROUNDS == 3,
                  str(s.INVESTIGATION_STANDARD_MAX_ROUNDS))
        finally:
            os.environ.pop("INVESTIGATION_STANDARD_MAX_ROUNDS", None)

        # ── 场景 6：investigation 是已注册的任务名 ──
        from app.llm.router import KNOWN_TASKS
        check("KNOWN_TASKS 含 investigation", "investigation" in KNOWN_TASKS,
              str(KNOWN_TASKS))
    finally:
        if prev_env is None:
            os.environ.pop("ENV", None)
        else:
            os.environ["ENV"] = prev_env

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
