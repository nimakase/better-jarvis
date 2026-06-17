"""对话历史 REST API + 健康检查。

替代原 web/memory.py：记忆库功能已下线，这里改为提供持久化对话历史的回放/清空，
供前端在加载、刷新、换设备时还原此前的对话。
"""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from core import history
from core import profile

router = APIRouter()


# ── 用户档案 / core memory（常驻长期记忆，设置面板增删查改）──────────────────
@router.get("/api/profile")
async def api_profile():
    return JSONResponse(profile.list_facts())


@router.post("/api/profile")
async def api_profile_add(text: str = Body(..., embed=True)):
    return profile.add_fact(text)


@router.put("/api/profile/{fact_id}")
async def api_profile_update(fact_id: int, text: str = Body(..., embed=True)):
    return profile.update_fact(fact_id, text)


@router.delete("/api/profile/{fact_id}")
async def api_profile_delete(fact_id: int):
    return profile.delete_fact(fact_id)


@router.get("/api/history")
async def api_history(conversation: str = history.DEFAULT_CONVERSATION, limit: int = 500):
    """返回某会话按时间正序的消息列表，供前端回放。"""
    return JSONResponse(history.get_messages(conversation, limit=limit))


@router.delete("/api/history")
async def api_clear_history(conversation: str = history.DEFAULT_CONVERSATION):
    history.clear(conversation)
    return {"ok": True}


@router.get("/api/conversations")
async def api_conversations():
    return JSONResponse(history.list_conversations())


@router.get("/api/health")
async def health():
    return {"status": "ok"}
