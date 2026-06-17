"""
报告中心 API —— 给 /reports 中心页用。

  GET  /api/reports                 归档列表 + 可生成的类型
  POST /api/reports/{type}/generate 生成某类型报告并归档
  GET  /api/reports/{id}/download   下载某份报告 PDF
"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, FileResponse

from core import reports

router = APIRouter()


@router.get("/api/reports")
async def list_reports():
    return JSONResponse({"reports": reports.list_reports(), "types": reports.list_types()})


@router.post("/api/reports/{type_id}/generate")
async def generate_report(type_id: str):
    res = await reports.generate(type_id)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.get("/api/reports/{report_id}/view")
async def view_report(report_id: str):
    """在线查看：以 inline 方式返回 PDF，点开即看、不强制下载。"""
    r = reports.get_report(report_id)
    if not r or not r.get("path"):
        return JSONResponse({"error": "报告不存在"}, status_code=404)
    p = Path(r["path"])
    if not p.exists():
        return JSONResponse({"error": "文件已丢失"}, status_code=404)
    return FileResponse(
        str(p), media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{p.name}"'},
    )


@router.get("/api/reports/{report_id}/download")
async def download_report(report_id: str):
    r = reports.get_report(report_id)
    if not r or not r.get("path"):
        return JSONResponse({"error": "报告不存在"}, status_code=404)
    p = Path(r["path"])
    if not p.exists():
        return JSONResponse({"error": "文件已丢失"}, status_code=404)
    return FileResponse(str(p), filename=p.name, media_type="application/pdf")
