"""
WebSocket 对话接口 + 带外动作分发。

会话隔离：每个 WebSocket 连接默认分配独立的 session_id（按连接隔离，
解决多标签共享历史的问题）；客户端也可在消息里带 "session_id" 以在
重连后延续同一会话。会话控制器由 app.state.ctx.sessions 管理。
"""

import json
import logging
import shutil
from pathlib import Path
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import config
from core import history
from core.tool_builder import list_skills_info
from connectors import vault

router = APIRouter()
DOWNLOAD_DIR = config.DOWNLOAD_DIR

# 2026-08-08：此前这条通道对话处理异常只会发给前端一条 error 消息，日志里
# 一个字都不落——同样的异常在飞书通道（lark_bridge.py）早就有 logger.exception
# 记录，网页这条漏了。用户反馈"贾维斯有时候静默失败，想回头查日志"，先把这个
# 最基本的缺口补上：出异常必须落进 logs/jarvis.log（明文、带完整 traceback），
# 不然"回头查"无从查起。
logger = logging.getLogger("jarvis.web")

# 单一主对话：transcript 落到固定会话，跨标签/设备都能回放同一段历史。
# 工作记忆(controller.messages)仍按连接隔离；历史层只负责"给人看"的持久记录。
CONVERSATION_ID = history.DEFAULT_CONVERSATION


def _seed_controller_from_history(controller) -> None:
    """新建的会话控制器若内存为空，用持久化的文本历史给模型补上下文，
    让换设备/重连后模型也能延续，而不仅仅是界面能回看。"""
    if controller.messages:
        return
    for m in history.get_messages(CONVERSATION_ID):
        if m["kind"] == "text" and m["role"] in ("user", "assistant"):
            controller.messages.append({"role": m["role"], "content": m["content"]})


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
                history.clear(CONVERSATION_ID)
                await websocket.send_text(json.dumps({"type": "system", "text": "会话已重置"}))
                continue

            if user_message == "/skills":
                skills = list_skills_info()
                await websocket.send_text(json.dumps({"type": "system", "text": json.dumps(skills, ensure_ascii=False, indent=2)}))
                continue

            controller = ctx.sessions.get(session_id)
            controller.channel = "web"   # 渠道感知（㉒）：本轮从网页来
            _seed_controller_from_history(controller)

            # 记录用户这轮输入（持久化，供回看 / 跨设备）
            history.append("user", user_message, conversation_id=CONVERSATION_ID)

            # 流式回复：controller 现在产出结构化事件（text / tool）两条通道
            await websocket.send_text(json.dumps({"type": "start"}))

            full_response = ""
            async for ev in controller.chat(user_message):
                if ev["type"] == "text":
                    full_response += ev["text"]
                    await websocket.send_text(json.dumps({"type": "chunk", "text": ev["text"]}))
                elif ev["type"] == "tool":
                    # 工具进度走独立事件，前端低调渲染、且不进对话正文/历史
                    await websocket.send_text(json.dumps({"type": "tool_status", "name": ev["name"]}))

            await websocket.send_text(json.dumps({"type": "end"}))

            # 持久化助手回复（仅纯文本正文；工具进度与证件揭示不入库）
            if full_response.strip():
                history.append("assistant", full_response, conversation_id=CONVERSATION_ID)

            # 处理本轮工具产生的带外动作（代码审查 / 文件下载 / 证件揭示）
            await _dispatch_actions(websocket, controller.drain_actions())

    except WebSocketDisconnect:
        pass
    except Exception as e:
        # 落一条带完整 traceback 的日志（logs/jarvis.log），不然出了问题只能
        # 靠前端那一行 error 文字猜——那句话往往只有异常的 str()，看不出根因。
        logger.exception("[web] 对话处理异常（session=%s）", session_id)
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
        card = {
            "filename": p["filename"],
            "url":      f"/api/download/{p['filename']}",
            "size":     p.get("size", 0),
        }
        await ws.send_text(json.dumps({"type": "file_download", **card}))
        # 文件卡片可安全持久化（不含敏感值），回放时仍可见
        history.append("assistant", card, kind="file", conversation_id=CONVERSATION_ID)

    elif kind == "interactive":
        # 贾维斯自己设计的可交互回复（按钮/表单）。网页侧原样下发，前端渲染成
        # 可点按钮：点击 = 把该选项 intent 当作用户输入发送（与飞书回调等价）。
        await ws.send_text(json.dumps({
            "type":    "interactive",
            "mode":    p.get("mode", "options"),
            "text":    p.get("text", ""),
            "options": p.get("options", []),
            "fields":  p.get("fields", []),
            "submit_label":  p.get("submit_label", "提交"),
            "submit_intent": p.get("submit_intent", "提交表单"),
        }, ensure_ascii=False))

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
