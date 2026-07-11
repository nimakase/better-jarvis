"""内置日历 REST API（阶段 1）。

抽屉日历面板用：list / create / update / delete / agenda。
agenda 返回统一时间轴（表内事件展开 + 派生条目），list 返回原始表行（供编辑视图）。
"""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from core import calendar as cal

router = APIRouter()


@router.get("/api/calendar")
async def api_list(kind: str = ""):
    """原始事件表行（不展开 rrule），供月视图/编辑。"""
    return JSONResponse(cal.list_events(kind or None))


@router.get("/api/calendar/agenda")
async def api_agenda(start: str = "", end: str = "", days: int = 7):
    """统一时间轴：表内事件展开 + 派生条目，按时间排序。"""
    return JSONResponse(cal.agenda(start or None, end or None, days=days))


@router.post("/api/calendar")
async def api_create(payload: dict = Body(...)):
    return cal.create_event(
        payload.get("title", ""),
        payload.get("start", ""),
        end=payload.get("end"),
        all_day=bool(payload.get("all_day", False)),
        kind=payload.get("kind", "event"),
        rrule=payload.get("rrule"),
        notify=payload.get("notify"),
        meta=payload.get("meta"),
    )


@router.put("/api/calendar/{event_id}")
async def api_update(event_id: str, payload: dict = Body(...)):
    return cal.update_event(event_id, **payload)


@router.delete("/api/calendar/{event_id}")
async def api_delete(event_id: str):
    return cal.delete_event(event_id)
