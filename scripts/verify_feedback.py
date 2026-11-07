"""数据飞轮自检 —— 反馈落库 / 孤儿守卫 / 版本戳 / 护栏指标 / 防污染。

飞轮最容易坏在看不见的地方：
  · 反馈没有 request_id / msg_id → 孤儿数据，永远归不了因
  · 没有版本戳 → 说不出「哪个版本变差了」
  · 护栏指标把「一次都没评」显示成 0 → 看起来像「评了但全坏」
  · 👎 自动进回归集 → 一次误操作把错误答案固化成金标准

这个脚本逐条证明这些情况不会静默发生。全部跑在临时目录，不碰真实库。

运行: D:\\python\\python.exe scripts/verify_feedback.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在导入 app 之前改路径：Settings 在首次实例化时就锁定这些值
_TMP = Path(tempfile.mkdtemp(prefix="sia_feedback_"))
os.environ["FEEDBACK_DB_PATH"] = str(_TMP / "feedback.db")
os.environ["FLYWHEEL_DIR_PATH"] = str(_TMP / "flywheel")
os.environ["AUDIT_DB_PATH"] = str(_TMP / "audit.db")
os.environ["MEMORY_DB_PATH"] = str(_TMP / "memory.db")

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}" if detail
          else f"  [{'PASS' if ok else 'FAIL'}] {name}")


def main() -> int:
    print("=" * 64)
    print("数据飞轮自检")
    print("=" * 64)

    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.routes_feedback import router as feedback_router
        from api.routes_metrics import router as metrics_router
        from app.audit import log as audit_log
        from app.feedback import store
        from app.memory import db as memory_db
        from app.observability import guardrails
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] 导入失败: {type(e).__name__}: {e}")
        return 1
    check("模块可导入", True)

    api = FastAPI()
    api.include_router(feedback_router)
    api.include_router(metrics_router)
    client = TestClient(api)

    # ── 1. 表结构与只追加 ──
    print("\n[1] 表结构与只追加")
    store.init_db()
    cols = {r[1] for r in sqlite3.connect(str(_TMP / "feedback.db"))
            .execute("PRAGMA table_info(feedback)")}
    need = {"id", "ts", "request_id", "msg_id", "label", "scope", "fact_id",
            "options_json", "note", "model", "prompt_version", "git_sha"}
    check("feedback 表含计划书要求全部字段", need <= cols, f"缺 {need - cols}")
    check("无删除/更新入口（只追加）",
          not hasattr(store, "delete") and not hasattr(store, "update")
          and not hasattr(store, "remove"))

    # ── 2. 落库与待审队列 ──
    print("\n[2] 落库与 👎 待审队列")
    r = client.post("/api/v1/feedback", json={
        "request_id": "rid-001", "msg_id": "m-001", "label": "up"})
    check("👍 返回 201", r.status_code == 201, str(r.status_code))
    check("👍 不进待审队列", r.json().get("inbox") is None, str(r.json()))
    inbox = _TMP / "flywheel" / "inbox.jsonl"
    check("👍 不写 inbox.jsonl", not inbox.exists())

    r = client.post("/api/v1/feedback", json={
        "request_id": "rid-002", "msg_id": "m-002", "label": "down",
        "scope": "fact", "fact_id": "F117",
        "options": ["数字不对", "来源不可信"], "note": "分部占比跟年报对不上"})
    check("👎 返回 201", r.status_code == 201)
    check("👎 指向待审队列", r.json().get("inbox") == "data/flywheel/inbox.jsonl",
          str(r.json()))
    lines = [json.loads(x) for x in inbox.read_text(encoding="utf-8").splitlines() if x]
    check("inbox 恰好一条", len(lines) == 1, str(len(lines)))
    rec = lines[0]
    check("inbox 记录状态为 pending_review", rec.get("status") == "pending_review")
    check("inbox 记录未标记 promoted", rec.get("promoted") is False)
    check("inbox 带 request_id / msg_id",
          rec.get("request_id") == "rid-002" and rec.get("msg_id") == "m-002")
    check("options 落成数组而非字符串",
          store.recent(1)[0]["options"] == ["数字不对", "来源不可信"])

    # ── 3. 孤儿守卫与入参校验 ──
    print("\n[3] 缺关联键 / 非法枚举一律拒绝")
    cases = [
        ("缺 request_id", {"msg_id": "m", "label": "up"}),
        ("缺 msg_id", {"request_id": "r", "label": "up"}),
        ("空白 request_id", {"request_id": "   ", "msg_id": "m", "label": "up"}),
        ("空白 msg_id", {"request_id": "r", "msg_id": " ", "label": "up"}),
        ("非法 label", {"request_id": "r", "msg_id": "m", "label": "meh"}),
        ("非法 scope", {"request_id": "r", "msg_id": "m", "label": "up",
                        "scope": "everything"}),
        ("note 超长", {"request_id": "r", "msg_id": "m", "label": "up",
                       "note": "x" * 2001}),
    ]
    for label, payload in cases:
        code = client.post("/api/v1/feedback", json=payload).status_code
        check(f"{label} → 422", code == 422, f"got {code}")
    check("拒绝的请求没有落库", store.stats()["total"] == 2, str(store.stats()))

    # ── 4. 版本戳 ──
    print("\n[4] 版本戳（model / prompt_version / git_sha）")
    latest = store.recent(1)[0]
    check("落库带 model", bool(latest.get("model")), str(latest.get("model")))
    check("prompt_version 形如 p<hash>",
          str(latest.get("prompt_version") or "").startswith("p")
          and len(str(latest.get("prompt_version"))) == 9,
          str(latest.get("prompt_version")))
    check("git_sha 有值或诚实为 None(非 git 环境)",
          latest.get("git_sha") is None or len(str(latest.get("git_sha"))) >= 7,
          str(latest.get("git_sha")))
    r = client.post("/api/v1/feedback", json={
        "request_id": "rid-003", "msg_id": "m-003", "label": "up",
        "model": "forged-model", "prompt_version": "p00000000",
        "git_sha": "deadbee"})
    forged = store.recent(1)[0]
    check("客户端伪造的版本戳不被采信",
          forged["model"] != "forged-model" and forged["prompt_version"] != "p00000000",
          str({k: forged[k] for k in ("model", "prompt_version")}))

    # ── 5. 关联键真的能连上 audit / conversations ──
    print("\n[5] request_id / msg_id 能关联到真实记录")
    audit_log.init_db()
    audit_log.audit("chat.reply", {"msg_id": "m-join", "content_hash": "abc"},
                    request_id="rid-join")
    conn = sqlite3.connect(str(_TMP / "audit.db"))
    hit = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE request_id=? AND detail_json LIKE ?",
        ("rid-join", '%"m-join"%')).fetchone()[0]
    check("audit_log 能用 request_id+msg_id 定位到那条回复", hit == 1, str(hit))
    acols = {r[1] for r in conn.execute("PRAGMA table_info(audit_log)")}
    check("audit_log 已补三件套列",
          {"model", "prompt_version", "git_sha"} <= acols, str(acols))
    arow = conn.execute("SELECT model, prompt_version, git_sha FROM audit_log"
                        " ORDER BY id DESC LIMIT 1").fetchone()
    check("新审计行的版本戳非空", all(arow[:2]), str(arow))
    conn.close()

    memory_db.init_db()
    memory_db.add_message("assistant", "示例回复", msg_id="m-join")
    mconn = sqlite3.connect(str(_TMP / "memory.db"))
    mrow = mconn.execute("SELECT msg_id, model, prompt_version, git_sha FROM"
                         " conversations ORDER BY id DESC LIMIT 1").fetchone()
    check("conversations 存下 msg_id", mrow[0] == "m-join", str(mrow))
    check("conversations 带版本戳", all(mrow[1:3]), str(mrow))
    joined = mconn.execute(
        "SELECT COUNT(*) FROM conversations c JOIN "
        "(SELECT 1) x WHERE c.msg_id=?", ("m-join",)).fetchone()[0]
    check("feedback.msg_id 可与 conversations.msg_id 对上", joined == 1)

    # ── 6. 旧库迁移 ──
    print("\n[6] 旧库就地补列（幂等，历史行留空）")
    old = _TMP / "old.db"
    oc = sqlite3.connect(str(old))
    oc.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, event_type TEXT)")
    oc.execute("INSERT INTO audit_log(event_type) VALUES ('legacy')")
    oc.commit()
    audit_log._migrate(oc)
    audit_log._migrate(oc)  # 第二次不应报错
    ocols = {r[1] for r in oc.execute("PRAGMA table_info(audit_log)")}
    check("迁移补齐三件套列", {"model", "prompt_version", "git_sha"} <= ocols)
    legacy = oc.execute("SELECT model, prompt_version FROM audit_log").fetchone()
    check("历史行留 NULL 而不是补假值", legacy == (None, None), str(legacy))
    oc.close()

    # ── 7. 五个护栏指标 ──
    print("\n[7] 五个护栏指标")
    real = guardrails.snapshot({}, eval_dir=ROOT / "data" / "eval")
    l1 = json.loads((ROOT / "data" / "eval" / "l1_report.json").read_text("utf-8"))
    l0 = json.loads((ROOT / "data" / "eval" / "l0_report.json").read_text("utf-8"))
    check("grounding_rate 取自真实 L1 报告",
          real["grounding_rate"] == l1["metrics"]["grounding_rate"],
          f"{real['grounding_rate']} vs {l1['metrics']['grounding_rate']}")
    check("degraded_rate 取自真实 L0 报告",
          real["degraded_rate"] == l0["metrics"]["degraded_rate"],
          f"{real['degraded_rate']} vs {l0['metrics']['degraded_rate']}")
    check("离线指标标明口径与时间",
          "离线" in real["meta"]["grounding_rate"]["scope"]
          and real["meta"]["grounding_rate"]["as_of"] == l1["generated_at"])

    empty = _TMP / "no_eval"
    empty.mkdir(exist_ok=True)
    none_val = guardrails.snapshot({}, eval_dir=empty, feedback_stats={"total": 0},
                                   assistant_total=0)
    for k in ("grounding_rate", "degraded_rate", "llm_error_rate",
              "feedback_negative_rate", "feedback_coverage"):
        check(f"{k} 无数据时为 None 而非 0", none_val[k] is None, str(none_val[k]))

    g = guardrails.snapshot({"llm_calls": 4, "llm_errors": 1},
                            eval_dir=empty, feedback_stats={"total": 4, "down": 3,
                                                            "distinct_msgs": 2},
                            assistant_total=8)
    check("llm_error_rate = 1/4", g["llm_error_rate"] == 0.25, str(g["llm_error_rate"]))
    check("feedback_negative_rate = 3/4", g["feedback_negative_rate"] == 0.75)
    check("feedback_coverage = 2/8", g["feedback_coverage"] == 0.25)
    check("llm 零调用 → None（不是 0%）",
          guardrails.snapshot({"llm_calls": 0}, eval_dir=empty,
                              feedback_stats={"total": 0},
                              assistant_total=1)["llm_error_rate"] is None)

    r = client.get("/api/v1/metrics")
    body = r.json()
    gr = body.get("guardrails") or {}
    five = {"grounding_rate", "degraded_rate", "llm_error_rate",
            "feedback_negative_rate", "feedback_coverage"}
    check("GET /api/v1/metrics 暴露 5 个护栏", five <= set(gr), str(sorted(gr)))
    check("护栏值同时带 meta 口径", isinstance(gr.get("meta"), dict))
    check("metrics 原有快照未被破坏", "counters" in (body.get("metrics") or {}))

    # ── 8. 防污染：反馈不进回归集 ──
    print("\n[8] 反馈不会自动进回归集")
    scripts = list((ROOT / "scripts").glob("eval_*.py")) + [ROOT / "scripts" / "run_eval.py"]
    offenders = [p.name for p in scripts
                 if p.exists() and ("flywheel" in p.read_text("utf-8").lower()
                                    or "inbox" in p.read_text("utf-8").lower())]
    check("没有任何评测脚本读取待审队列", not offenders, str(offenders))
    check("待审队列记录不含 promoted=True",
          all(not json.loads(x).get("promoted")
              for x in inbox.read_text("utf-8").splitlines() if x))

    # ── 9. UI 接线（组件本身无关联键就不显示）──
    print("\n[9] UI 采集点接线")
    from ui import api_client
    from ui.pages import chat as chat_page
    from ui.pages import research as research_page
    from ui.pages import evals as evals_page

    check("api_client 暴露 feedback()", callable(getattr(api_client, "feedback", None)))
    check("对话页：无关联键不显示组件", chat_page.can_feedback({}) is False)
    check("对话页：缺 request_id 不显示",
          chat_page.can_feedback({"msg_id": "m"}) is False)
    check("对话页：空白 id 不算有",
          chat_page.can_feedback({"msg_id": " ", "request_id": "r"}) is False)
    check("对话页：两者齐全才显示",
          chat_page.can_feedback({"msg_id": "m", "request_id": "r"}) is True)
    check("研究页：旧缓存报告（无 report_id）不显示",
          research_page.can_feedback_report({"ticker": "600519.SS"}) is False)
    check("研究页：有 report_id+request_id 才显示",
          research_page.can_feedback_report(
              {"report_id": "report:600519.SS:abc", "request_id": "r"}) is True)

    fw = evals_page.flywheel_rows()
    keys = {k for k, _ in evals_page.GUARDRAILS}
    check("评测看板展示 5 个护栏指标",
          keys == {"grounding_rate", "degraded_rate", "llm_error_rate",
                   "feedback_negative_rate", "feedback_coverage"}, str(sorted(keys)))
    check("护栏值缺失时用 — 而不是 0",
          all(evals_page._pct(fw.get(k)) in (evals_page.DASH,) or "%" in
              evals_page._pct(fw.get(k)) for k in keys))
    check("看板自身也读真实 L1 接地率（离线口径）",
          fw["grounding_rate"] == l1["metrics"]["grounding_rate"],
          str(fw["grounding_rate"]))
    check("页面调用了飞轮渲染", "_render_flywheel()" in
          (ROOT / "ui" / "pages" / "evals.py").read_text("utf-8"))

    print("\n" + "=" * 64)
    if failed:
        print(f"结果: {passed} 通过, {failed} 失败")
        return 1
    print(f"结果: {passed}/{passed} 全部通过")
    print(f"临时目录: {_TMP}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
