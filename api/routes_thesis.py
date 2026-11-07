"""论点台账 API —— /api/v1/thesis/*

个人自用：无鉴权、无多租户。存的是「为什么买 / 什么情况下认错」，
以及每次拿真实产物去检查这些条件的结果。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.thesis import checks
from app.thesis import db as tdb
from app.thesis.draft import build_draft

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = (ROOT / "data" / "artifacts").resolve()

router = APIRouter(prefix="/api/v1/thesis", tags=["thesis"])

_VERDICT_RANK = {"falsified": 0, "inconclusive": 1, "holding": 2, None: 3}


# ── 模型 ──────────────────────────────────────────────────────

class ThesisCreate(BaseModel):
    statement: str
    ticker: str = ""
    stance: str = "neutral"
    horizon: str = ""
    key_assumptions: list[str] = Field(default_factory=list)
    falsifiers: list[dict] = Field(default_factory=list)
    conviction: int = 3
    artifact_ref: str = ""


class ThesisUpdate(BaseModel):
    ticker: str | None = None
    statement: str | None = None
    stance: str | None = None
    horizon: str | None = None
    key_assumptions: list[str] | None = None
    falsifiers: list[dict] | None = None
    status: str | None = None
    conviction: int | None = None
    artifact_ref: str | None = None


class EvidenceRequest(BaseModel):
    ref: str
    kind: str = "fact"
    direction: str = "supports"
    note: str = ""


class ReviewRequest(BaseModel):
    verdict: str
    snapshot: dict | None = None
    note: str = ""


class CheckRequest(BaseModel):
    artifact_ref: str | None = None


class DraftRequest(BaseModel):
    artifact_ref: str


# ── 产物路径 ──────────────────────────────────────────────────

def _resolve_artifact(ref: str) -> Path:
    """只允许读 data/artifacts 下的 .json，挡住路径穿越与任意文件读取。"""
    raw = Path(ref)
    path = (raw if raw.is_absolute() else ROOT / raw).resolve()
    if ARTIFACT_DIR not in path.parents:
        raise HTTPException(status_code=400,
                            detail="artifact_ref 必须指向 data/artifacts 下的产物文件")
    if path.suffix.lower() != ".json" or not path.is_file():
        raise HTTPException(status_code=404, detail=f"产物不存在：{ref}")
    return path


def _artifact_ref_of(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _list_artifacts(ticker: str = "", limit: int = 100) -> list[dict]:
    if not ARTIFACT_DIR.is_dir():
        return []
    base = ticker.strip().upper().replace(".", "_")
    items = []
    for p in ARTIFACT_DIR.glob("*/*.json"):
        if base and p.parent.name != base:
            continue
        st = p.stat()
        items.append({
            "ticker": p.parent.name,
            "name": p.name,
            "ref": _artifact_ref_of(p),
            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "size_kb": round(st.st_size / 1024, 1),
            "_mtime": st.st_mtime,
        })
    items.sort(key=lambda x: x["_mtime"], reverse=True)
    for it in items:
        it.pop("_mtime")
    return items[:max(1, int(limit))]


# ── 静态路由（必须先于 /{thesis_id} 声明）─────────────────────

@router.get("/artifacts")
def list_artifacts(ticker: str = "", limit: int = 100):
    """可选产物列表，供草稿与证伪检查选择依据。"""
    return {"items": _list_artifacts(ticker, limit)}


@router.post("/draft")
def create_draft(req: DraftRequest):
    """从产物生成草稿：抄真实数字，论点留空由人写。"""
    path = _resolve_artifact(req.artifact_ref)
    try:
        return build_draft(path, root=ROOT)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"产物无法解析：{e}")


# ── 论点 CRUD ─────────────────────────────────────────────────

@router.get("")
def list_theses(ticker: str = "", status: str = "", limit: int = 100,
                with_check: bool = False):
    """列出论点。with_check=true 时逐条跑证伪检查，证伪的排最前。"""
    try:
        items = tdb.list_theses(ticker=ticker or None, status=status or None,
                                limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not with_check:
        return {"items": items}

    ledger_cache: dict[str, object] = {}
    for it in items:
        it["check"] = _run_check(it, ledger_cache)
    # 稳定排序：只按结论分组，组内保持 DB 的 updated_at 倒序
    items.sort(key=lambda x: _VERDICT_RANK.get((x.get("check") or {}).get("verdict"), 4))
    return {"items": items}


@router.post("")
def create_thesis(req: ThesisCreate):
    try:
        tid = tdb.create_thesis(
            statement=req.statement, ticker=req.ticker, stance=req.stance,
            horizon=req.horizon, key_assumptions=req.key_assumptions,
            falsifiers=req.falsifiers, conviction=req.conviction,
            artifact_ref=req.artifact_ref,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": tid}


@router.get("/{thesis_id}")
def get_thesis(thesis_id: str):
    t = tdb.get_thesis(thesis_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"论点不存在：{thesis_id}")
    t["evidence"] = tdb.list_evidence(thesis_id)
    t["reviews"] = tdb.list_reviews(thesis_id)
    return t


@router.patch("/{thesis_id}")
def update_thesis(thesis_id: str, req: ThesisUpdate):
    fields = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    if tdb.get_thesis(thesis_id) is None:
        raise HTTPException(status_code=404, detail=f"论点不存在：{thesis_id}")
    try:
        db_changed = tdb.update_thesis(thesis_id, **fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not db_changed:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    return {"ok": True, "thesis": tdb.get_thesis(thesis_id)}


@router.delete("/{thesis_id}")
def delete_thesis(thesis_id: str):
    if not tdb.delete_thesis(thesis_id):
        raise HTTPException(status_code=404, detail=f"论点不存在：{thesis_id}")
    return {"ok": True}


# ── 证据 / 复盘 ───────────────────────────────────────────────

@router.post("/{thesis_id}/evidence")
def add_evidence(thesis_id: str, req: EvidenceRequest):
    try:
        eid = tdb.add_evidence(thesis_id, req.ref, kind=req.kind,
                               direction=req.direction, note=req.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": eid}


@router.delete("/evidence/{evidence_id}")
def delete_evidence(evidence_id: int):
    if not tdb.delete_evidence(evidence_id):
        raise HTTPException(status_code=404, detail=f"证据不存在：{evidence_id}")
    return {"ok": True}


@router.post("/{thesis_id}/review")
def add_review(thesis_id: str, req: ReviewRequest):
    try:
        rid = tdb.add_review(thesis_id, req.verdict, snapshot=req.snapshot,
                             note=req.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": rid}


# ── 证伪检查 ──────────────────────────────────────────────────

def _run_check(thesis: dict, cache: dict[str, object]) -> dict:
    """跑一次证伪检查。同一请求内按产物路径复用台账，避免重复解析大 JSON。"""
    ref = (thesis.get("artifact_ref") or "").strip()
    if ref:
        if ref not in cache:
            try:
                cache[ref] = checks.load_ledger(_resolve_artifact(ref))
            except HTTPException:
                cache[ref] = None
        result = checks.evaluate(thesis, cache[ref])
        result["artifact"] = ref
        if cache[ref] is None:
            result["artifact_reason"] = "unreadable"
    else:
        result = checks.evaluate(thesis, None)
        result["artifact"] = None
        result["artifact_reason"] = "no_artifact_ref"
    result["checked_at"] = datetime.now().isoformat(timespec="seconds")
    return result


@router.post("/{thesis_id}/check")
def check_thesis(thesis_id: str, req: CheckRequest | None = None):
    """拿产物台账对照该论点的证伪条件；不调 LLM、不改库。"""
    t = tdb.get_thesis(thesis_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"论点不存在：{thesis_id}")
    override = (req.artifact_ref if req else None) or ""
    if override:
        t["artifact_ref"] = _artifact_ref_of(_resolve_artifact(override))
    return _run_check(t, {})
