"""
飞书连接器

覆盖功能：
  - 获取日历事件列表
  - 创建日历事件
  - 获取最近消息（IM）
  - 发送消息给指定用户或群

使用前提：
  1. 在飞书开放平台创建应用，获取 App ID 和 App Secret
  2. 填入 .env 文件
  3. 给应用开通对应权限：
       calendar:calendar（日历读写）
       im:message（消息读写）
       im:message:send_as_bot（发消息）
"""

import httpx
import json
from datetime import datetime, timezone
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from core.controller import register_tool

# ── Token 管理 ────────────────────────────────────────────────────────────────

_access_token: Optional[str] = None
_token_expires_at: float = 0


async def _get_access_token() -> str:
    """获取租户 access token，自动续期。"""
    import time
    global _access_token, _token_expires_at

    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token

    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        raise ValueError("飞书 App ID / App Secret 未配置，请检查 .env 文件")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{config.FEISHU_BASE_URL}/auth/v3/tenant_access_token/internal",
            json={"app_id": config.FEISHU_APP_ID, "app_secret": config.FEISHU_APP_SECRET},
        )
        data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError(f"飞书 token 获取失败：{data}")

    _access_token = data["tenant_access_token"]
    _token_expires_at = time.time() + data.get("expire", 7200)
    return _access_token


async def _headers() -> dict:
    token = await _get_access_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── 日历 API ──────────────────────────────────────────────────────────────────

async def get_calendar(start_date: str, end_date: str, calendar_id: str = "primary") -> str:
    """
    获取指定日期范围内的日历事件。
    start_date / end_date 格式：YYYY-MM-DD
    """
    try:
        start_ts = int(datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc).timestamp())
        end_ts   = int(datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc).timestamp())

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{config.FEISHU_BASE_URL}/calendar/v4/calendars/{calendar_id}/events",
                headers=await _headers(),
                params={
                    "start_time": str(start_ts),
                    "end_time":   str(end_ts),
                    "page_size":  50,
                },
            )
        data = resp.json()

        if data.get("code") != 0:
            return f"获取日历失败：{data.get('msg')}"

        events = data.get("data", {}).get("items", [])
        if not events:
            return f"{start_date} 到 {end_date} 没有日程安排。"

        result = []
        for e in events:
            summary = e.get("summary", "（无标题）")
            start   = e.get("start_time", {}).get("date_time", e.get("start_time", {}).get("date", ""))
            end_t   = e.get("end_time",   {}).get("date_time", e.get("end_time",   {}).get("date", ""))
            result.append(f"- {summary}：{start} ~ {end_t}")

        return "\n".join(result)

    except Exception as e:
        return f"日历工具出错：{e}"


async def create_calendar_event(
    summary: str,
    start_datetime: str,
    end_datetime: str,
    description: str = "",
    calendar_id: str = "primary",
) -> str:
    """
    创建日历事件。
    start_datetime / end_datetime 格式：YYYY-MM-DDTHH:MM:SS+08:00
    """
    try:
        body = {
            "summary": summary,
            "description": description,
            "start_time": {"date_time": start_datetime, "timezone": "Asia/Shanghai"},
            "end_time":   {"date_time": end_datetime,   "timezone": "Asia/Shanghai"},
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{config.FEISHU_BASE_URL}/calendar/v4/calendars/{calendar_id}/events",
                headers=await _headers(),
                json=body,
            )
        data = resp.json()
        if data.get("code") != 0:
            return f"创建日历失败：{data.get('msg')}"
        event_id = data.get("data", {}).get("event", {}).get("event_id", "")
        return f"已创建日程「{summary}」，event_id={event_id}"

    except Exception as e:
        return f"创建日历出错：{e}"


# ── 消息 API ──────────────────────────────────────────────────────────────────

async def get_feishu_messages(chat_id: str, limit: int = 20) -> str:
    """获取指定会话的最近消息。"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{config.FEISHU_BASE_URL}/im/v1/messages",
                headers=await _headers(),
                params={"container_id_type": "chat", "container_id": chat_id, "page_size": limit},
            )
        data = resp.json()
        if data.get("code") != 0:
            return f"获取消息失败：{data.get('msg')}"

        items = data.get("data", {}).get("items", [])
        if not items:
            return "没有消息。"

        result = []
        for msg in reversed(items):
            sender = msg.get("sender", {}).get("id", "未知")
            body   = json.loads(msg.get("body", {}).get("content", "{}"))
            text   = body.get("text", "[非文字消息]")
            result.append(f"{sender}: {text}")
        return "\n".join(result)

    except Exception as e:
        return f"获取消息出错：{e}"


async def send_feishu_message(receive_id: str, text: str, receive_id_type: str = "open_id") -> str:
    """
    发送文字消息。
    receive_id_type: open_id | user_id | email | chat_id
    """
    try:
        body = {
            "receive_id": receive_id,
            "msg_type":   "text",
            "content":    json.dumps({"text": text}),
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{config.FEISHU_BASE_URL}/im/v1/messages",
                headers=await _headers(),
                params={"receive_id_type": receive_id_type},
                json=body,
            )
        data = resp.json()
        if data.get("code") != 0:
            return f"发送消息失败：{data.get('msg')}"
        return f"消息已发送给 {receive_id}"

    except Exception as e:
        return f"发送消息出错：{e}"


# ── 工具定义 & 注册 ───────────────────────────────────────────────────────────

FEISHU_TOOL_DEFS = [
    {
        "name": "get_calendar",
        "description": "获取飞书日历中指定日期范围内的事件列表。询问日程、安排、空档时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date":  {"type": "string", "description": "开始日期，格式 YYYY-MM-DD"},
                "end_date":    {"type": "string", "description": "结束日期，格式 YYYY-MM-DD"},
                "calendar_id": {"type": "string", "description": "日历 ID，默认 'primary'"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "在飞书日历中创建新事件。用户要新增日程时使用。执行前须告知用户将创建的内容。",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary":        {"type": "string", "description": "事件标题"},
                "start_datetime": {"type": "string", "description": "开始时间，格式 YYYY-MM-DDTHH:MM:SS+08:00"},
                "end_datetime":   {"type": "string", "description": "结束时间，格式 YYYY-MM-DDTHH:MM:SS+08:00"},
                "description":    {"type": "string", "description": "事件描述（可选）"},
                "calendar_id":    {"type": "string", "description": "日历 ID，默认 'primary'"},
            },
            "required": ["summary", "start_datetime", "end_datetime"],
        },
    },
    {
        "name": "get_feishu_messages",
        "description": "获取飞书指定会话的最近消息记录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "飞书会话 ID"},
                "limit":   {"type": "integer", "description": "最多获取多少条，默认 20"},
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "send_feishu_message",
        "description": "通过飞书发送文字消息给指定用户或群。不可逆操作，执行前必须告知用户并确认。",
        "input_schema": {
            "type": "object",
            "properties": {
                "receive_id":      {"type": "string", "description": "接收方 ID"},
                "text":            {"type": "string", "description": "消息内容"},
                "receive_id_type": {"type": "string", "description": "ID 类型：open_id / user_id / email / chat_id"},
            },
            "required": ["receive_id", "text"],
        },
    },
]

async def _handle_get_calendar(start_date: str, end_date: str, calendar_id: str = "primary") -> str:
    return await get_calendar(start_date, end_date, calendar_id)

async def _handle_create_calendar_event(summary: str, start_datetime: str, end_datetime: str, description: str = "", calendar_id: str = "primary") -> str:
    return await create_calendar_event(summary, start_datetime, end_datetime, description, calendar_id)

async def _handle_get_feishu_messages(chat_id: str, limit: int = 20) -> str:
    return await get_feishu_messages(chat_id, limit)

async def _handle_send_feishu_message(receive_id: str, text: str, receive_id_type: str = "open_id") -> str:
    return await send_feishu_message(receive_id, text, receive_id_type)

FEISHU_HANDLERS = {
    "get_calendar":          _handle_get_calendar,
    "create_calendar_event": _handle_create_calendar_event,
    "get_feishu_messages":   _handle_get_feishu_messages,
    "send_feishu_message":   _handle_send_feishu_message,
}


def register_feishu_tools():
    """在应用启动时调用，把飞书工具注册进主控。"""
    for defn in FEISHU_TOOL_DEFS:
        register_tool(defn, FEISHU_HANDLERS[defn["name"]])
