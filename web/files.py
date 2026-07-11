"""文件上传 / 下载接口。"""

import shutil

from fastapi import APIRouter, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse

import config
from core.safety import safe_filename

router = APIRouter()
UPLOAD_DIR = config.UPLOAD_DIR
DOWNLOAD_DIR = config.DOWNLOAD_DIR


@router.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    safe = safe_filename(file.filename)
    dest = UPLOAD_DIR / safe
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"path": str(dest), "filename": safe}


@router.get("/api/download/{filename}")
async def download_file(filename: str):
    path = DOWNLOAD_DIR / filename
    if not path.exists():
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    return FileResponse(path, filename=filename, media_type="application/octet-stream")
