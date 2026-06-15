"""自建技能管理 API。"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.tool_builder import (
    activate_skill, deactivate_skill, list_skills_info, read_skill_code,
)

router = APIRouter()


@router.post("/api/tools/{name}/activate")
async def api_activate_tool(name: str):
    ok, msg = activate_skill(name)
    return {"ok": ok, "message": msg}


@router.post("/api/tools/{name}/deactivate")
async def api_deactivate_tool(name: str):
    ok, msg = deactivate_skill(name)
    return {"ok": ok, "message": msg}


@router.get("/api/tools")
async def api_list_tools():
    return JSONResponse(list_skills_info())


@router.get("/api/tools/{name}/code")
async def api_get_tool_code(name: str):
    code = read_skill_code(name)
    if code is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"name": name, "code": code}
