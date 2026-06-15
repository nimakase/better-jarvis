"""记忆库 REST API 与健康检查。"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core import memory as mem

router = APIRouter()


@router.get("/api/memory")
async def api_memory():
    return JSONResponse(mem.list_all())


@router.delete("/api/memory/{key}")
async def api_delete_memory(key: str):
    mem.delete(key)
    return {"ok": True}


@router.get("/api/health")
async def health():
    return {"status": "ok"}
