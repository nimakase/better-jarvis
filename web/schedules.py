"""定时任务管理 REST API —— 供前端「定时任务」面板用。

包住 core.scheduler 的 list / pause / resume / update，
让你用开关和改时间管理任务，无需对话。
"""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from core import scheduler
from core import schedule_presets

router = APIRouter()


@router.get("/api/schedules")
async def api_list_schedules():
    """列出所有定时任务（名称/描述/状态/cron/下次运行）。"""
    return JSONResponse(scheduler.list_schedules())


@router.get("/api/schedules/presets")
async def api_list_presets():
    """列出"可启动任务"预设目录；started 标记是否已创建。"""
    existing = {s["name"] for s in scheduler.list_schedules()}
    return JSONResponse(schedule_presets.list_presets(existing))


@router.post("/api/schedules/presets/{pid}/start")
async def api_start_preset(pid: str, cron: str = Body("", embed=True)):
    """按预设 + 所选 cron 一键启动一个定时任务。请求体：{"cron": "0 9 * * 1"}。"""
    ok, msg = schedule_presets.create_from_preset(pid, cron)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@router.post("/api/schedules/{name}/pause")
async def api_pause_schedule(name: str):
    ok, msg = scheduler.pause_schedule(name)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@router.post("/api/schedules/{name}/resume")
async def api_resume_schedule(name: str):
    ok, msg = scheduler.resume_schedule(name)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@router.post("/api/schedules/{name}/cron")
async def api_update_cron(name: str, cron: str = Body(..., embed=True)):
    """更新执行时间。请求体：{"cron": "0 8 * * *"}。"""
    ok, msg = scheduler.update_schedule(name, cron=cron)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@router.delete("/api/schedules/{name}")
async def api_delete_schedule(name: str):
    """删除定时任务（不可恢复）。创建仍走对话，这里只负责删。"""
    ok, msg = scheduler.delete_schedule(name)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)
