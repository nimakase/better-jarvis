"""
贾维斯 - 入口

启动：python main.py
然后浏览器打开 http://localhost:8000
"""

import asyncio
import json
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from core.controller import controller
from core import memory as mem
from core.tool_builder import (
    register_meta_tools, load_all_active_skills,
    activate_skill, deactivate_skill,
    list_skills_info, read_skill_code
)
from connectors.feishu import register_feishu_tools
from connectors.document import register_document_tools
from core.scheduler import get_scheduler, load_all_active_schedules

# 注册工具（顺序：先内置元工具，再外部连接器）
register_meta_tools()
register_feishu_tools()
register_document_tools()

# 启动时加载所有已激活的自建技能
load_all_active_skills()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：加载定时任务并启动调度器
    load_all_active_schedules()
    scheduler = get_scheduler()
    scheduler.start()
    yield
    # 关闭：停止调度器
    scheduler.shutdown(wait=False)


app = FastAPI(title="贾维斯", lifespan=lifespan)

# 上传文件临时目录
UPLOAD_DIR = Path(tempfile.gettempdir()) / "jarvis_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


# ── 前端 ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "frontend" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ── WebSocket 对话接口 ─────────────────────────────────────────────────────────

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            user_message = payload.get("message", "").strip()

            if not user_message:
                continue

            # 特殊命令
            if user_message == "/reset":
                controller.reset_session()
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

            # 流式回复
            await websocket.send_text(json.dumps({"type": "start"}))

            full_response = ""
            async for chunk in controller.chat(user_message):
                full_response += chunk
                await websocket.send_text(json.dumps({"type": "chunk", "text": chunk}))

            await websocket.send_text(json.dumps({"type": "end"}))

            # 检查回复中是否有 __skill_action__（工具生成了代码审查请求）
            # 扫描最近几条 assistant/tool messages
            _check_and_send_skill_actions(websocket, controller.messages)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(json.dumps({"type": "error", "text": str(e)}))
        except Exception:
            pass


def _check_and_send_skill_actions(ws: WebSocket, messages: list):
    """
    扫描最近的 tool 结果消息，如果有 __skill_action__ 就发 code_review 事件。
    用 asyncio.create_task 异步发送，不阻塞主循环。
    """
    for msg in reversed(messages[-6:]):
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("role") == "tool":
                    _try_emit_skill_action(ws, block.get("content", ""))
        elif isinstance(content, str):
            _try_emit_skill_action(ws, content)


def _try_emit_skill_action(ws: WebSocket, text: str):
    if not isinstance(text, str) or "__skill_action__" not in text:
        return
    try:
        # text 可能混有其他内容，找 JSON 部分
        start = text.find("{")
        end   = text.rfind("}") + 1
        data  = json.loads(text[start:end])
        if data.get("__skill_action__") == "code_review":
            asyncio.ensure_future(
                ws.send_text(json.dumps({
                    "type":       "code_review",
                    "name":       data["name"],
                    "code":       data["code"],
                    "message":    data.get("message", ""),
                    "validation": data.get("validation", {}),
                }))
            )
    except Exception:
        pass


# ── 技能管理 API ──────────────────────────────────────────────────────────────

@app.post("/api/tools/{name}/activate")
async def api_activate_tool(name: str):
    ok, msg = activate_skill(name)
    return {"ok": ok, "message": msg}


@app.post("/api/tools/{name}/deactivate")
async def api_deactivate_tool(name: str):
    ok, msg = deactivate_skill(name)
    return {"ok": ok, "message": msg}


@app.get("/api/tools")
async def api_list_tools():
    return JSONResponse(list_skills_info())


@app.get("/api/tools/{name}/code")
async def api_get_tool_code(name: str):
    code = read_skill_code(name)
    if code is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"name": name, "code": code}


# ── 文件上传接口 ──────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    dest = UPLOAD_DIR / file.filename
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"path": str(dest), "filename": file.filename}


# ── REST API ──────────────────────────────────────────────────────────────────

@app.get("/api/memory")
async def api_memory():
    return JSONResponse(mem.list_all())


@app.delete("/api/memory/{key}")
async def api_delete_memory(key: str):
    mem.delete(key)
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ── 启动 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
