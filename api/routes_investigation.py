"""自主深度调查 API —— 后台任务 + SSE 进度。

形状照抄 api/routes_analysis.py（那条链路已验证过）：
  _tasks 内存注册表 + daemon 线程干活 + 0.5s 轮询 SSE 推增量进度。

与 analysis 的差异：**按 run_id 而不是 ticker 建键**。同一标的的标准档与深度档
可以并存、也可以重复跑，用 ticker 建键会互相覆盖；另设 (ticker, depth) 次级索引
只为 409 防重入判定。

审计事件：investigation.start / .round / .complete / .failed / .budget_stop。
审计层只写不读（不喂给模型），符合分层设计。
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, PlainTextResponse

from app.agent.redteam import build_redteam
from app.audit.log import audit
from app.audit.middleware import get_request_id
from app.config import get_settings
from app.investigation.budget import Budget, Depth, StopReason
from app.investigation.loop import run_investigation
from app.investigation.report import build_report, report_markdown
from app.observability.metrics import metrics
from app.utils.logger import logger

router = APIRouter(prefix="/api/v1/investigation", tags=["investigation"])

# run_id -> {status, ticker, depth, progress[], result, run, error}
_tasks: dict[str, dict] = {}
# "TICKER|depth" -> run_id（仅用于 409 防重入）
_by_key: dict[str, str] = {}


def _sse_event(event: str, data: dict) -> str:
    """格式化 SSE 事件（本模块内复制，不跨模块 import，与 routes_analysis 同风格）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _key(ticker: str, depth: Depth) -> str:
    return f"{ticker}|{depth.value}"


def _coerce_depth(depth) -> Depth:
    """档位非法值一律落标准档（不因参数写错就报错）。"""
    return Depth.coerce(depth)


def _get_llm():
    try:
        from app.llm.client import get_llm_client
        return get_llm_client()
    except Exception as e:                      # noqa: BLE001
        logger.warning("调查无法获取 LLM 客户端，将走降级路径: %s", e)
        return None


def _run(run_id: str, ticker: str, depth: Depth, period: str, request_id: str) -> None:
    task = _tasks.get(run_id)
    if not task:
        return
    task["status"] = "running"

    def _progress(ev: dict) -> None:
        task["progress"].append(ev)
        # 每轮与收尾各落一条审计（可追溯），审计层不喂模型
        if ev.get("phase") in ("round", "evidence", "conclude"):
            audit("investigation.round",
                  {"ticker": ticker, "depth": depth.value,
                   "phase": ev.get("phase"), "round": ev.get("round"),
                   "tool": ev.get("tool"), "spent": ev.get("spent")},
                  request_id=request_id)

    try:
        settings = get_settings()
        if not settings.INVESTIGATION_ENABLED:
            raise RuntimeError(
                "自主深度调查已被配置关闭（INVESTIGATION_ENABLED=false）")
        budget = Budget.from_settings(depth, settings=settings)
        llm = _get_llm()
        run = run_investigation(ticker, depth=depth, period=period, budget=budget,
                                llm=llm, progress=_progress)
        # 红队只在深度档跑，标准档产物与 L4 数字不受影响。
        # 红队自己出错不阻断调查：它带 status=llm_unavailable 落档，不编反方论证。
        if depth is Depth.DEEP:
            run["redteam"] = build_redteam(run, llm=llm)
        report = build_report(run, llm=llm, prose=True)
        task["run"] = run
        task["result"] = report
        task["status"] = "completed"
        metrics.record("investigation_seconds", float(run["spent"].get("elapsed") or 0))
        audit("investigation.complete",
              {"ticker": ticker, "depth": depth.value,
               "stop_reason": run["stop_reason"], "spent": run["spent"],
               "status_counts": run.get("status_counts")},
              request_id=request_id)
        if run["stop_reason"] != StopReason.ALL_SETTLED.value:
            metrics.inc("investigation_budget_stops")
            audit("investigation.budget_stop",
                  {"ticker": ticker, "depth": depth.value,
                   "stop_reason": run["stop_reason"], "spent": run["spent"]},
                  request_id=request_id)
    except Exception as e:                      # noqa: BLE001
        logger.error("调查失败 %s: %s", ticker, e)
        task["error"] = str(e)
        task["status"] = "failed"
        task["progress"].append({
            "phase": "error", "round": 0, "label": "调查失败",
            "message": str(e), "pct": 100, "tool": None, "spent": {},
        })
        audit("investigation.failed",
              {"ticker": ticker, "depth": depth.value, "error": str(e)},
              request_id=request_id)


def _start(ticker: str, depth: Depth, period: str) -> dict:
    t = ticker.strip().upper()
    k = _key(t, depth)
    existing = _by_key.get(k)
    if existing and _tasks.get(existing, {}).get("status") in ("pending", "running"):
        raise HTTPException(status_code=409,
                            detail=f"该标的该档位调查正在进行中: {t} ({depth.value})")
    run_id = uuid.uuid4().hex
    rid = get_request_id()
    metrics.inc("investigation_requests")
    _tasks[run_id] = {"status": "pending", "ticker": t, "depth": depth.value,
                      "period": period, "progress": [], "result": None,
                      "run": None, "error": None}
    _by_key[k] = run_id
    audit("investigation.start",
          {"ticker": t, "depth": depth.value, "period": period, "run_id": run_id},
          request_id=rid)
    threading.Thread(target=_run, args=(run_id, t, depth, period, rid),
                     daemon=True).start()
    return {"ticker": t, "run_id": run_id, "depth": depth.value,
            "period": period, "status": "pending"}


@router.post("/run/{ticker}")
def start_investigation(ticker: str, depth: str = "standard", period: str = "annual"):
    """启动自主深度调查（后台线程）。depth=standard|deep。"""
    return _start(ticker, _coerce_depth(depth), period)


@router.post("/run-sync/{ticker}")
def start_investigation_sync(ticker: str, depth: str = "standard",
                             period: str = "annual"):
    """同步跑完并返回报告（供脚本/验收用；耗时可达分钟级）。"""
    started = _start(ticker, _coerce_depth(depth), period)
    run_id = started["run_id"]
    task = _tasks[run_id]
    # 同步路径直接等线程结束（daemon 线程 + 轮询状态，避免再写一份逻辑）
    import time
    deadline = time.monotonic() + Budget.from_settings(
        _coerce_depth(depth)).deadline_seconds + 180
    while task["status"] in ("pending", "running"):
        if time.monotonic() > deadline:
            raise HTTPException(status_code=504, detail="调查超时未完成")
        time.sleep(0.5)
    if task["status"] == "failed":
        raise HTTPException(status_code=500, detail=task.get("error", "调查失败"))
    return task["result"]


@router.get("/status/{run_id}")
def get_status(run_id: str):
    task = _tasks.get(run_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"未找到调查任务: {run_id}")
    run = task.get("run") or {}
    return {
        "run_id": run_id, "ticker": task["ticker"], "depth": task["depth"],
        "status": task["status"], "error": task.get("error"),
        "spent": (run.get("spent") if run else None),
        "limits": (run.get("limits") if run else
                   Budget.from_settings(_coerce_depth(task["depth"])).as_dict()),
        "stop_reason": run.get("stop_reason"),
        "hypotheses": [{"id": h["id"], "family_label": h.get("family_label"),
                        "statement": h.get("statement"), "status": h.get("status"),
                        "support_count": h.get("support_count", 0),
                        "evidence_count": len(h.get("evidence") or [])}
                       for h in (run.get("hypotheses") or [])],
        "latest_progress": (task["progress"][-1] if task["progress"] else None),
    }


@router.get("/stream/{run_id}")
async def investigation_stream(run_id: str, timeout: int = 900):
    """SSE 进度流：0.5s 轮询任务进度，逐条推送 progress，终态后 status+done。

    timeout 秒内未到终态则超时结束（默认 900s，大于深度档最长 deadline）。
    """
    task = _tasks.get(run_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"未找到调查任务: {run_id}")

    async def gen():
        sent = 0
        start = asyncio.get_event_loop().time()
        deadline = start + max(timeout, 30)
        try:
            while True:
                if asyncio.get_event_loop().time() > deadline:
                    yield _sse_event("error", {"message": "SSE 进度流超时"})
                    yield _sse_event("done", {})
                    return
                t = _tasks.get(run_id)
                if t is None:
                    yield _sse_event("error", {"message": "任务已丢失"})
                    yield _sse_event("done", {})
                    return
                progress = t.get("progress") or []
                while sent < len(progress):
                    yield _sse_event("progress", progress[sent])
                    sent += 1
                status = t.get("status")
                if status == "failed":
                    yield _sse_event("error", {"message": t.get("error") or "调查失败"})
                    yield _sse_event("status", {"status": status, "run_id": run_id})
                    yield _sse_event("done", {})
                    return
                if status == "completed":
                    yield _sse_event("status", {"status": status, "run_id": run_id})
                    yield _sse_event("done", {})
                    return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        except Exception as e:                  # noqa: BLE001
            yield _sse_event("error", {"message": str(e)})
            yield _sse_event("done", {})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"})


@router.get("/result/{run_id}")
def get_result(run_id: str):
    task = _tasks.get(run_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"未找到调查任务: {run_id}")
    if task["status"] in ("pending", "running"):
        raise HTTPException(status_code=202, detail="调查尚未完成")
    if task["status"] == "failed":
        raise HTTPException(status_code=500, detail=task.get("error", "调查失败"))
    return task["result"]


@router.get("/result/by-ticker/{ticker}")
def get_result_by_ticker(ticker: str, depth: str = "standard"):
    """取该标的该档位最近一次完成的调查结果（供 UI 直接取用）。"""
    t = ticker.strip().upper()
    d = _coerce_depth(depth)
    for run_id in reversed(list(_tasks)):
        task = _tasks[run_id]
        if (task.get("ticker") == t and task.get("depth") == d.value
                and task.get("status") == "completed"):
            return task["result"]
    raise HTTPException(status_code=404,
                        detail=f"未找到已完成调查: {t} ({d.value})")


@router.get("/report/{run_id}")
def get_report_markdown(run_id: str):
    """导出确定性 Markdown 报告（缺失数值渲染为 —，离线可复核）。"""
    task = _tasks.get(run_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"未找到调查任务: {run_id}")
    if task["status"] != "completed":
        raise HTTPException(status_code=409, detail="调查尚未完成，无法导出报告")
    return PlainTextResponse(report_markdown(task["result"]),
                             media_type="text/markdown; charset=utf-8")
