"""A2 验证：论点台账 API（CRUD / 草稿 / 证伪检查 / 路径安全）。

运行: D:\\python\\python.exe scripts/verify_thesis_api.py
用 FastAPI TestClient，临时库；不联网、不调 LLM、不写项目内 data/。
产物取自 data/artifacts 下真实的 JSON（只读）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="verify_thesis_api_"))

    class _TmpSettings:
        thesis_db = tmp / "thesis.db"

    real = sorted((ROOT / "data" / "artifacts").glob("*/*.json"))
    if not real:
        print("[FAIL] data/artifacts 为空，无法验证草稿与检查")
        sys.exit(1)
    artifact = real[-1]

    with mock.patch("app.thesis.db.get_settings", lambda: _TmpSettings()):
        from fastapi.testclient import TestClient

        from app.thesis import checks
        from api.main import app
        from app.thesis import db as tdb

        tdb.init_db()
        # 不进 lifespan：避免触碰真实 cache/watchlist/audit 等库
        client = TestClient(app)
        base = "/api/v1/thesis"

        # ── 1. 空列表 ──
        r = client.get(base)
        check("GET 空列表", r.status_code == 200 and r.json()["items"] == [])

        # ── 2. 产物列表 ──
        r = client.get(f"{base}/artifacts")
        arts = r.json().get("items", [])
        check("GET 产物列表", r.status_code == 200 and len(arts) >= 1, f"{len(arts)} 份")
        check("产物 ref 在 data/artifacts 下",
              all(a["ref"].startswith("data/artifacts/") for a in arts))

        # ── 3. 草稿 ──
        rel = artifact.relative_to(ROOT).as_posix()
        r = client.post(f"{base}/draft", json={"artifact_ref": rel})
        draft = r.json()
        check("POST 草稿 200", r.status_code == 200, str(r.status_code))
        check("草稿不替用户写论点", draft.get("statement") == "")
        check("草稿含证伪条件", len(draft.get("falsifiers") or []) >= 2,
              f"{len(draft.get('falsifiers') or [])} 条")
        check("草稿阈值标注需人工改",
              all("认错线" in f.get("note", "") for f in draft["falsifiers"]))
        check("草稿假设来自 DCF 真实参数",
              any("WACC" in a for a in draft.get("key_assumptions", [])),
              str(draft.get("key_assumptions")))

        # 草稿里的每个 metric 都必须能在产物台账里取到值，不许编路径
        ledger = checks.load_ledger(artifact)
        missing = [f["metric"] for f in draft["falsifiers"]
                   if checks.resolve_metric(ledger, f["metric"]) is None]
        check("草稿路径全部可在台账取到", not missing, str(missing))

        # 阈值取当前值 ⇒ 在同一份产物上判定必须未命中，草稿不能一出生就被证伪
        born_breached = [f["metric"] for f in draft["falsifiers"]
                         if checks.evaluate_falsifier(f, ledger).state == "breached"]
        check("草稿在源产物上不自我证伪", not born_breached, str(born_breached))

        # ── 4. 路径安全 ──
        for bad in ("data/eval/baseline.json", "../../requirements.txt",
                    "data/artifacts/does_not_exist.json"):
            r = client.post(f"{base}/draft", json={"artifact_ref": bad})
            check(f"拒绝越界/不存在的产物 {bad[:28]}", r.status_code in (400, 404),
                  str(r.status_code))

        # ── 5. 创建 / 读回 ──
        r = client.post(base, json={
            "statement": "测试论点：DCF 公平价值为正", "ticker": "aapl",
            "stance": "bullish", "horizon": "12 个月",
            "key_assumptions": ["需求不塌"],
            "falsifiers": draft["falsifiers"][:2],
            "conviction": 4, "artifact_ref": rel,
        })
        tid = r.json().get("id", "")
        check("POST 创建", r.status_code == 200 and len(tid) == 32, tid[:8])

        r = client.get(f"{base}/{tid}")
        t = r.json()
        check("GET 详情", r.status_code == 200 and t["statement"].startswith("测试论点"))
        check("详情含证据与复盘字段", t["evidence"] == [] and t["reviews"] == [])
        check("ticker 大写归一", t["ticker"] == "AAPL")

        # ── 6. 字段校验 ──
        for payload, label in (
            ({"statement": "  "}, "空 statement"),
            ({"statement": "x", "stance": "long"}, "非法 stance"),
            ({"statement": "x", "conviction": 9}, "非法 conviction"),
            ({"statement": "x", "falsifiers": [{"metric": "a", "op": "≈", "value": 1}]},
             "非法比较符"),
            ({"statement": "x", "falsifiers": [{"metric": "a", "op": ">", "value": "高"}]},
             "非数字阈值"),
        ):
            r = client.post(base, json=payload)
            check(f"拒绝{label}", r.status_code == 400, str(r.status_code))

        # ── 7. 更新 ──
        r = client.patch(f"{base}/{tid}", json={"status": "confirmed",
                                                "statement": "改后论点"})
        check("PATCH 更新", r.status_code == 200 and r.json()["thesis"]["statement"] == "改后论点")
        check("PATCH 空 body 拒绝", client.patch(f"{base}/{tid}", json={}).status_code == 400)
        check("PATCH 非法 status 拒绝",
              client.patch(f"{base}/{tid}", json={"status": "done"}).status_code == 400)
        check("PATCH 不存在的论点 404",
              client.patch(f"{base}/nope", json={"statement": "x"}).status_code == 404)

        # ── 8. 证据 / 复盘 ──
        r = client.post(f"{base}/{tid}/evidence",
                        json={"ref": "valuation_analysis.dcf.fair_value",
                              "kind": "fact", "direction": "supports", "note": "实测"})
        eid = r.json().get("id")
        check("POST 证据", r.status_code == 200 and isinstance(eid, int))
        check("详情含 1 条证据", len(client.get(f"{base}/{tid}").json()["evidence"]) == 1)
        check("DELETE 证据", client.delete(f"{base}/evidence/{eid}").status_code == 200)
        check("DELETE 不存在证据 404",
              client.delete(f"{base}/evidence/999999").status_code == 404)
        r = client.post(f"{base}/{tid}/review",
                        json={"verdict": "holds", "snapshot": {"x": 1.0}, "note": "复核"})
        check("POST 复盘", r.status_code == 200)
        check("复盘快照往返",
              client.get(f"{base}/{tid}").json()["reviews"][0]["snapshot"] == {"x": 1.0})
        check("非法 verdict 拒绝",
              client.post(f"{base}/{tid}/review", json={"verdict": "great"}).status_code == 400)

        # ── 9. 证伪检查 ──
        r = client.post(f"{base}/{tid}/check")
        res = r.json()
        check("POST check 200", r.status_code == 200)
        check("check 逐条返回", len(res.get("results") or []) == 2)
        check("check 结论合法",
              res.get("verdict") in ("falsified", "holding", "inconclusive"), res.get("verdict"))
        check("check 带回产物引用", res.get("artifact") == rel)
        check("check 每条的观测值来自台账",
              all(item["state"] != "unknown" for item in res["results"]),
              str([(i["state"], i["reason"]) for i in res["results"]]))

        # 最新期占位符必须解析成真实路径
        gm = next((f for f in draft["falsifiers"] if "[]" in f["metric"]), None)
        if gm:
            res_gm = checks.evaluate_falsifier(gm, ledger)
            check("[]. 解析到具体期数",
                  res_gm.resolved_path is not None and "[" in res_gm.resolved_path,
                  str(res_gm.resolved_path))

        # 无产物 → 全 unknown，不伪装成 holding
        tid2 = client.post(base, json={
            "statement": "没有产物的论点",
            "falsifiers": [{"metric": "valuation_analysis.dcf.fair_value",
                            "op": "<", "value": 1}],
        }).json()["id"]
        res2 = client.post(f"{base}/{tid2}/check").json()
        check("无 artifact_ref → artifact=None",
              res2["artifact"] is None and res2["artifact_reason"] == "no_artifact_ref")
        check("无台账 → inconclusive 而非 holding",
              res2["verdict"] == "inconclusive" and res2["counts"]["unknown"] == 1)
        check("无台账逐条标 unknown",
              all(i["state"] == "unknown" and i["reason"] == "no_ledger"
                  for i in res2["results"]))

        # 显式覆盖产物
        res3 = client.post(f"{base}/{tid2}/check",
                           json={"artifact_ref": rel}).json()
        check("check 可用 body 覆盖产物", res3["artifact"] == rel and res3["counts"]["unknown"] == 0)
        check("check 不存在论点 404",
              client.post(f"{base}/nope/check").status_code == 404)

        # ── 10. 带检查的列表：证伪的排最前 ──
        tid3 = client.post(base, json={
            "statement": "必然被证伪的论点", "ticker": "AAPL", "artifact_ref": rel,
            "falsifiers": [{"metric": "valuation_analysis.dcf.fair_value",
                            "op": ">", "value": 0.0}],
        }).json()["id"]
        items = client.get(base, params={"with_check": True}).json()["items"]
        check("with_check 返回每条的结论", all("check" in it for it in items))
        verdicts = [i["check"]["verdict"] for i in items]
        falsified = {i["id"] for i in items if i["check"]["verdict"] == "falsified"}
        check("新证伪的条目确实被证伪", tid3 in falsified)
        check("证伪条目全部排在最前",
              verdicts[:len(falsified)] == ["falsified"] * len(falsified), str(verdicts))
        check("非证伪条目排在后面",
              all(v != "falsified" for v in verdicts[len(falsified):]), str(verdicts))

        # ── 11. 过滤 ──
        check("按 ticker 过滤",
              len(client.get(base, params={"ticker": "aapl"}).json()["items"]) == 2)
        check("按 status 过滤",
              len(client.get(base, params={"status": "confirmed"}).json()["items"]) == 1)

        # ── 12. 删除 ──
        check("DELETE 论点", client.delete(f"{base}/{tid}").status_code == 200)
        check("删除后 404", client.get(f"{base}/{tid}").status_code == 404)
        check("重复删除 404", client.delete(f"{base}/{tid}").status_code == 404)
        check("删除后库里无残留",
              tdb.count_theses() == 2 and tdb.list_evidence(tid) == [])

        check("thesis.db 落盘", (tmp / "thesis.db").exists())

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
