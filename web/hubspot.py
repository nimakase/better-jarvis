"""
HubSpot 本地登录调试 API —— 给情报台的「HubSpot 连接」面板用。

  GET  /api/hubspot/status  当前登录状态
  POST /api/hubspot/login   在本机弹有头 Chrome，手动登录一次
  POST /api/hubspot/check   无头检测会话是否有效
  POST /api/hubspot/close   关闭登录窗口
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from prospecting.login_manager import LoginManager

router = APIRouter()

_MSG = {
    "ok": "HubSpot 登录有效",
    "login_in_progress": "登录中 —— 请在本机弹出的 Chrome 窗口完成登录",
    "expired": "登录已过期，请点「重新登录」",
    "failed": "登录失败或超时，请重试",
    "busy": "正在处理任务，稍后再试",
    "unknown": "未登录 / 状态未知",
    "closing": "已请求关闭登录窗口",
}


def _resp(status: str):
    return JSONResponse({"status": status, "message": _MSG.get(status, status)})


@router.get("/api/hubspot/status")
async def hubspot_status():
    return _resp(LoginManager.get().get_status())


@router.post("/api/hubspot/login")
async def hubspot_login():
    return _resp(LoginManager.get().start_login())


@router.post("/api/hubspot/check")
async def hubspot_check():
    return _resp(LoginManager.get().check_session())


@router.post("/api/hubspot/close")
async def hubspot_close():
    return _resp(LoginManager.get().stop_login())
