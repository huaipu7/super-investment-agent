"""深度研究分析 API —— 后台任务 + 报告生成（迁移自 Research-Agent backend/main.py）。

任务状态流转: pending → running → completed / failed。
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.analysis.pipeline import AnalysisOrchestrator
from app.analysis.cache import cached_status, get_cached_result, set_cached_result
from app.config import BASE_DIR
from app.analysis.valuation import recompute_dcf_from_result
from app.audit.log import audit
from app.audit.middleware import get_request_id
from app.observability.metrics import metrics
from app.report.generator import ReportGenerator
from app.trust.ledger import content_hash  # 接受 dict，audit.log 的同名函数只收字符串
from app.utils.logger import logger

router = APIRouter(prefix="/api/v1", tags=["analysis"])

REPORT_DIR = "data/reports"

# 内存任务缓存（ticker -> {status, result, error, progress}）
_tasks: dict[str, dict] = {}


def _sse_event(event: str, data: dict) -> str:
    """格式化 SSE 事件（本模块内复制，不跨模块 import routes_chat）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _run_analysis(ticker: str, request_id: str = ""):
    task = _tasks.get(ticker)
    if not task:
        return
    task["status"] = "running"
    try:
        orchestrator = AnalysisOrchestrator()
        result = orchestrator.run(ticker, progress_cb=task["progress"].append)
        # 报告反馈的关联键：内容哈希作 report_id（同一份报告多次打开仍算同一份），
        # request_id 指回本次分析请求。旧缓存产物没有这两个字段，UI 据此不显示反馈组件。
        report_id = f"report:{ticker}:{content_hash(result)}"
        result["report_id"] = report_id
        result["request_id"] = request_id
        task["report_id"] = report_id
        task["result"] = result
        task["status"] = "completed"
        set_cached_result(ticker, result)  # 持久化，进程重启后仍可命中
        pt = result.get("pipeline_time", 0)
        metrics.record("analysis_pipeline_seconds", pt)
        logger.info("分析完成", extra={"ticker": ticker, "pipeline_seconds": round(pt, 3)})
        audit("analysis.complete",
              {"ticker": ticker, "pipeline_time": pt},
              request_id=request_id)
    except Exception as e:
        logger.error("分析失败 %s: %s", ticker, e)
        task["error"] = str(e)
        task["progress"].append({
            "step": "error", "label": "分析失败", "message": str(e), "pct": 100,
        })
        task["status"] = "failed"
        audit("analysis.failed", {"ticker": ticker, "error": str(e)}, request_id=request_id)


@router.post("/analysis/run/{ticker}")
def run_analysis(ticker: str):
    """启动分析管线（后台线程执行）"""
    t = ticker.strip().upper()
    metrics.inc("analysis_requests")
    if t in _tasks and _tasks[t]["status"] == "running":
        raise HTTPException(status_code=409, detail=f"分析正在进行中: {t}")
    rid = get_request_id()
    _tasks[t] = {"status": "pending", "result": None, "error": None, "progress": []}
    audit("analysis.start", {"ticker": t}, request_id=rid)
    thread = threading.Thread(target=_run_analysis, args=(t, rid), daemon=True)
    thread.start()
    return {"ticker": t, "status": "pending"}


@router.get("/analysis/status/{ticker}")
def get_analysis_status(ticker: str):
    t = ticker.strip().upper()
    task = _tasks.get(t)
    if not task:
        # 进程重启后任务丢失：命中持久化缓存则视为 completed
        if cached_status(t):
            return {"ticker": t, "status": "completed", "cached": True,
                    "progress": [], "error": None}
        return {"ticker": t, "status": "pending", "progress": []}
    return {
        "ticker": t, "status": task["status"], "error": task.get("error"),
        "progress": task.get("progress", []),
    }


@router.get("/analysis/stream/{ticker}")
async def analysis_stream(ticker: str, timeout: int = 900):
    """SSE 进度流：0.5s 轮询 task.progress 增量推送 [1/7]→done，终态后结束。

    事件序列: progress(每步) → status(终态) → done；异常先 error 再 done。
    timeout 秒内未到终态则超时结束（默认 900s=15min）。
    """
    t = ticker.strip().upper()

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
                task = _tasks.get(t)
                if task is None:
                    await asyncio.sleep(0.5)
                    continue
                progress = task.get("progress") or []
                while sent < len(progress):
                    ev = progress[sent]
                    sent += 1
                    yield _sse_event("progress", ev)
                status = task.get("status")
                if status == "failed":
                    yield _sse_event("error", {"message": task.get("error", "分析失败")})
                    yield _sse_event("status", {"status": status, "ticker": t})
                    yield _sse_event("done", {})
                    return
                if status == "completed":
                    yield _sse_event("status", {"status": status, "ticker": t})
                    yield _sse_event("done", {})
                    return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        except Exception as e:
            yield _sse_event("error", {"message": str(e)})
            yield _sse_event("done", {})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


@router.get("/analysis/result/{ticker}")
def get_analysis_result(ticker: str, force: bool = False):
    t = ticker.strip().upper()
    task = _tasks.get(t)
    if not task:
        # 进程重启后任务丢失：命中持久化缓存直接返回（force=True 时忽略缓存）
        if not force:
            cached = get_cached_result(t)
            if cached is not None:
                _tasks[t] = {"status": "completed", "result": cached,
                             "error": None, "progress": []}
                return cached
        raise HTTPException(status_code=404, detail=f"未找到分析任务: {t}")
    if task["status"] in ("running", "pending"):
        raise HTTPException(status_code=202, detail="分析尚未完成")
    if task["status"] == "failed":
        # 失败但存在历史缓存 → 返回缓存兜底
        cached = get_cached_result(t)
        if cached is not None:
            task["result"] = cached
            task["status"] = "completed"
            return cached
        raise HTTPException(status_code=500, detail=task.get("error", "分析失败"))
    return task["result"]


@router.post("/analysis/run-sync/{ticker}")
def run_analysis_sync(ticker: str):
    """同步执行分析管线（等待完成，返回完整结果）"""
    t = ticker.strip().upper()
    metrics.inc("analysis_requests")
    rid = get_request_id()
    audit("analysis.start", {"ticker": t, "mode": "sync"}, request_id=rid)
    try:
        orchestrator = AnalysisOrchestrator()
        result = orchestrator.run(t)
        _tasks[t] = {"status": "completed", "result": result, "error": None, "progress": []}
        set_cached_result(t, result)
        audit("analysis.complete",
              {"ticker": t, "mode": "sync", "pipeline_time": result.get("pipeline_time", 0)},
              request_id=rid)
        return result
    except Exception as e:
        _tasks[t] = {"status": "failed", "result": None, "error": str(e), "progress": []}
        audit("analysis.failed", {"ticker": t, "mode": "sync", "error": str(e)},
              request_id=rid)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analysis/report/{ticker}")
def generate_analysis_report(ticker: str, fmt: str = "all"):
    """为已完成分析生成报告（md/html/pdf，fmt=all|md|html|pdf）"""
    t = ticker.strip().upper()
    task = _tasks.get(t)
    if not task or task["status"] != "completed":
        raise HTTPException(status_code=400, detail="请先完成分析：POST /api/v1/analysis/run/{ticker}")
    try:
        gen = ReportGenerator(output_dir=REPORT_DIR)
        paths = gen.generate(task["result"], fmt=fmt)
        audit("report.export", {"ticker": t, "fmt": fmt}, request_id=get_request_id())
        return {
            "ticker": t,
            "report": {k: v for k, v in paths.items() if k != "charts"},
            "charts_count": len(paths.get("charts", {})),
        }
    except Exception as e:
        logger.error("报告生成失败 %s: %s", t, e)
        raise HTTPException(status_code=500, detail=str(e))


class DcfRecomputeRequest(BaseModel):
    """看板估值参数滑块 → 重算 DCF 的请求体（全部可选，None 回退分析时基准值）。"""
    ticker: str
    wacc: float | None = None
    terminal_growth: float = 0.03
    projection_years: int = 5
    beta: float | None = None
    risk_free_rate: float | None = None
    erp: float | None = None
    current_price: float | None = None


@router.post("/analysis/recompute-dcf")
def recompute_dcf(req: DcfRecomputeRequest):
    """基于已缓存分析结果重算 DCF（确定性，无 LLM，秒回）。

    从缓存提取 fcf_history/股本/净负债/当前价，按给定 wacc/永续g/预测期重算
    每股内在价值与 upside，并附敏感性矩阵。参数缺失回退分析时估算值。
    """
    t = req.ticker.strip().upper()
    result = get_cached_result(t)
    if result is None:
        raise HTTPException(status_code=404, detail=f"未找到分析结果: {t}")
    try:
        out = recompute_dcf_from_result(
            result,
            wacc=req.wacc,
            terminal_growth=req.terminal_growth,
            projection_years=req.projection_years,
            beta=req.beta,
            risk_free_rate=req.risk_free_rate,
            erp=req.erp,
            current_price=req.current_price,
        )
    except Exception as e:
        logger.error("DCF 重算失败 %s: %s", t, e)
        raise HTTPException(status_code=500, detail=str(e))
    if not out.get("available"):
        raise HTTPException(status_code=400, detail=out.get("reason", "DCF 重算失败"))
    out["ticker"] = t
    audit("analysis.recompute_dcf", {"ticker": t, "wacc": req.wacc,
                                     "terminal_growth": req.terminal_growth},
          request_id=get_request_id())
    return out


# ── 增量刷新 & 事实级对比 ────────────────────────────────────

ARTIFACT_DIR = (BASE_DIR / "data" / "artifacts").resolve()


def _artifact_path(ref: str) -> Path:
    """只允许 data/artifacts 下的 .json，挡住路径穿越。"""
    raw = Path(ref)
    p = (raw if raw.is_absolute() else BASE_DIR / raw).resolve()
    if ARTIFACT_DIR not in p.parents:
        raise HTTPException(status_code=400,
                            detail="artifact_ref 必须指向 data/artifacts 下的产物")
    if p.suffix.lower() != ".json" or not p.is_file():
        raise HTTPException(status_code=404, detail=f"产物不存在：{ref}")
    return p


def _artifact_ref(p: Path) -> str:
    try:
        return p.relative_to(BASE_DIR).as_posix()
    except ValueError:
        return p.as_posix()


def _latest_artifact(ticker: str) -> Path | None:
    d = ARTIFACT_DIR / ticker.upper().replace(".", "_")
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
    return files[0] if files else None


class RefreshRequest(BaseModel):
    base_ref: str | None = None


class DiffRequest(BaseModel):
    old_ref: str | None = None
    new_ref: str | None = None       # 省略时用当前缓存结果


@router.post("/analysis/refresh/{ticker}")
def refresh_analysis(ticker: str, req: RefreshRequest = RefreshRequest()):
    """行情增量刷新：只重取行情与由现价决定的结论，不调 LLM。"""
    from app.analysis.refresh import refresh_price, summarize_refresh
    from app.trust.factdiff import diff_facts

    t = ticker.strip().upper()
    if req.base_ref:
        base_path = _artifact_path(req.base_ref)
        base = json.loads(base_path.read_text(encoding="utf-8"))
        base_at = datetime.fromtimestamp(
            base_path.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
        base_ref = _artifact_ref(base_path)
    else:
        cached = get_cached_result(t)
        if cached is not None:
            base, base_ref, base_at = cached, "", ""
        else:
            base_path = _latest_artifact(t)
            if base_path is None:
                raise HTTPException(status_code=404,
                                    detail=f"未找到 {t} 的分析结果，先跑一次全量分析")
            base = json.loads(base_path.read_text(encoding="utf-8"))
            base_at = datetime.fromtimestamp(
                base_path.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
            base_ref = _artifact_ref(base_path)

    try:
        result = refresh_price(base, base_ref=base_ref, base_generated_at=base_at)
    except Exception as e:
        logger.error("行情刷新失败 %s: %s", t, e)
        raise HTTPException(status_code=400, detail=f"刷新失败: {e}")

    result["report_id"] = f"report:{t}:{content_hash(result)}"
    result["request_id"] = get_request_id()

    out_dir = ARTIFACT_DIR / t.replace(".", "_")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{datetime.now():%Y%m%dT%H%M%S}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                   default=str, allow_nan=False),
                        encoding="utf-8")
    set_cached_result(t, result)

    diff = diff_facts(base, result)
    audit("analysis.refresh_price", {"ticker": t, "artifact": _artifact_ref(out_path)},
          request_id=get_request_id())
    return {
        "ticker": t,
        "artifact_ref": _artifact_ref(out_path),
        "refresh": summarize_refresh(result),
        "diff": diff["summary"],
        "completeness": result.get("completeness"),
    }


@router.post("/analysis/diff")
def diff_analysis(req: DiffRequest):
    """事实级跨 run 对比：按「路径 + 期间」对齐两份产物。"""
    from app.trust.factdiff import diff_facts

    if not req.old_ref:
        raise HTTPException(status_code=400, detail="缺少 old_ref")
    old_path = _artifact_path(req.old_ref)
    old = json.loads(old_path.read_text(encoding="utf-8"))

    if req.new_ref:
        new_path = _artifact_path(req.new_ref)
        new = json.loads(new_path.read_text(encoding="utf-8"))
        new_ref = _artifact_ref(new_path)
    else:
        ticker = old.get("ticker") or ""
        cached = get_cached_result(ticker) if ticker else None
        if cached is None:
            raise HTTPException(status_code=404, detail="没有当前结果可对比")
        new, new_ref = cached, "current"

    out = diff_facts(old, new)
    out["old"]["ref"] = _artifact_ref(old_path)
    out["new"]["ref"] = new_ref
    return out
