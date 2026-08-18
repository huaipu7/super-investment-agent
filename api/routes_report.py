"""报告 API —— 列表 / 下载 / 导出（Markdown → PDF）。

报告生成主体在 routes_analysis（深度研报）与 routes_chat（对话报告导出）中调用。
"""
from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.audit.log import audit
from app.audit.middleware import get_request_id
from app.report.pdf import markdown_to_pdf
from app.utils.logger import logger

router = APIRouter(prefix="/api/v1", tags=["report"])

REPORT_DIR = os.path.abspath("data/reports")
os.makedirs(REPORT_DIR, exist_ok=True)

_EXPORT_EXTS = {".md", ".html", ".pdf"}


@router.get("/reports/list")
def list_reports(kind: str = "all", ticker: str = ""):
    """列出已生成的报告文件（kind=all|md|html|pdf，可按 ticker 过滤，按修改时间倒序）"""
    files = []
    if not os.path.isdir(REPORT_DIR):
        return files
    base = ticker.strip().upper()
    for name in os.listdir(REPORT_DIR):
        path = os.path.join(REPORT_DIR, name)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in _EXPORT_EXTS:
            continue
        if kind != "all" and ext != "." + kind:
            continue
        if base:
            # 文件名形如 {ticker}_{时间戳}.md，兼容带交易所后缀（600519.SS_xxx）
            if not name.startswith(base):
                continue
            rest = name[len(base):]
            if not rest or rest[0] not in (".", "_"):
                continue
        files.append({
            "name": name,
            "size_kb": round(os.path.getsize(path) / 1024, 1),
            "modified": os.path.getmtime(path),
        })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return files


@router.get("/reports/download/{file_name:path}")
def download_report(file_name: str):
    """下载报告文件（防路径穿越）"""
    report_dir_norm = os.path.normpath(REPORT_DIR)
    file_path = os.path.normpath(os.path.join(REPORT_DIR, file_name))
    if not file_path.startswith(report_dir_norm):
        raise HTTPException(status_code=403, detail="非法路径")
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {file_name}")

    audit("report.download", {"file": file_name}, request_id=get_request_id())

    ext = os.path.splitext(file_path)[1].lower()
    media_type = {
        ".pdf": "application/pdf",
        ".html": "text/html; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
    }.get(ext, "application/octet-stream")
    return FileResponse(path=file_path, media_type=media_type, filename=os.path.basename(file_path))


class ExportRequest(BaseModel):
    text: str
    title: str = "投资分析报告"


@router.post("/report/export")
def export_pdf(req: ExportRequest):
    """将 Markdown（对话报告）转为 PDF 下载"""
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        markdown_to_pdf(req.text, output_path=tmp_path, title=req.title)
        with open(tmp_path, "rb") as f:
            content = f.read()
        os.unlink(tmp_path)
        audit("report.export_pdf", {"title": req.title, "bytes": len(content)},
              request_id=get_request_id())
        # 用 ASCII 文件名（中文标题进 header 会触发 latin-1 编码错误）
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="report.pdf"'},
        )
    except Exception as e:
        logger.error("导出 PDF 失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
