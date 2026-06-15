"""
WebSocket 对话接口 + 带外动作分发。

会话隔离：每个 WebSocket 连接默认分配独立的 session_id（按连接隔离，
解决多标签共享历史的问题）；客户端也可在消息里带 "session_id" 以在
重连后延续同一会话。会话控制器由 app.state.ctx.sessions 管理。
"""

import json
import shutil
from pathlib import Path
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import config
from core import memory as mem
from core.tool_builder import list_skills_info
from connectors import vault

router = APIRouter()
DOWNLOAD_DIR = config.DOWNLOAD_DIR


@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()
    ctx = websocket.app.state.ctx
    conn_session_id = uuid.uuid4().hex   # 本连接默认会话（按连接隔离）
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            user_message = payload.get("message", "").strip()
            # 客户端可显式指定 session_id 以跨重连延续；否则用本连接的
            session_id = payload.get("session_id") or conn_session_id

            if not user_message:
                continue

            # 特殊命令
            if user_message == "/reset":
                ctx.sessions.reset(session_id)
                await websocket.send_text(json.dumps({"type": "system", "text": "会话已重置"}))
                continue

            if user_message == "/memory":
                items = mem.list_all()
                await websocket.send_text(json.dumps({"type": "system", "text": json.dumps(items, ensure_ascii=False, indent=2)}))
                continue

            if user_message == "/skills":
                skills = list_skills_info()
                await websocket.send_text(json.dumps({"type": "system", "text": json.dumps(skills, ensure_ascii=False, indent=2)}))
                continue

            controller = ctx.sessions.get(session_id)

            # 流式回复
            await websocket.send_text(json.dumps({"type": "start"}))

            full_response = ""
            async for chunk in controller.chat(user_message):
                full_response += chunk
                await websocket.send_text(json.dumps({"type": "chunk", "text": chunk}))

            await websocket.send_text(json.dumps({"type": "end"}))

            # 处理本轮工具产生的带外动作（代码审查 / 文件下载 / 证件揭示）
            await _dispatch_actions(websocket, controller.drain_actions())

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(json.dumps({"type": "error", "text": str(e)}))
        except Exception:
            pass
    finally:
        # 丢弃本连接默认会话，避免内存无界增长；客户端自带的 session_id 予以保留
        ctx.sessions.discard(conn_session_id)


async def _dispatch_actions(ws: WebSocket, actions: list):
    """处理本轮工具产生的带外动作（来自 controller.drain_actions()）。

    取代早期「扫描消息历史里的魔法 JSON」做法：动作由工具显式返回，
    类型明确、不进对话历史 / 云端。逐个分发，单个失败不影响其余。
    """
    for action in actions:
        try:
            await _dispatch_one(ws, action)
        except Exception:
            pass


async def _dispatch_one(ws: WebSocket, action):
    kind = action.type
    p = action.payload

    if kind == "code_review":
        # 把生成 / 修改的工具代码推给前端审查
        await ws.send_text(json.dumps({
            "type":       "code_review",
            "name":       p["name"],
            "code":       p["code"],
            "message":    p.get("message", ""),
            "validation": p.get("validation", {}),
        }))

    elif kind == "file_download":
        # 拷到下载目录并推下载卡片
        src = Path(p["file_path"])
        if src.exists():
            shutil.copy2(src, DOWNLOAD_DIR / p["filename"])
        await ws.send_text(json.dumps({
            "type":     "file_download",
            "filename": p["filename"],
            "url":      f"/api/download/{p['filename']}",
            "size":     p.get("size", 0),
        }))

    elif kind == "credential_reveal":
        # 在本机解密取出【真实】字段值，仅推往本机浏览器。
        # 真实值只走「加密库 → 本机 main.py → 浏览器」，绝不进入对话历史 / 云端。
        alias  = p["alias"]
        fields = p.get("fields") or None
        real   = vault.get_fields(alias, only=fields)  # 本机解密
        if not real:
            return
        real.pop("_raw_lines", None)
        meta = vault.get_meta(alias) or {}
        await ws.send_text(json.dumps({
            "type":       "credential_reveal",
            "alias":      alias,
            "type_label": vault.TYPE_LABELS.get(meta.get("cred_type"), meta.get("cred_type", "")),
            "values":     real,        # 真实值，仅发往本机浏览器
        }, ensure_ascii=False))
