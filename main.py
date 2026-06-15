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

import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
import uvicorn

from core.controller import controller
from core import memory as mem
from core.safety import safe_filename
from core.tool_builder import (
    register_meta_tools, load_all_active_skills,
    activate_skill, deactivate_skill,
    list_skills_info, read_skill_code
)
from connectors.feishu import register_feishu_tools
from connectors.document import register_document_tools
from connectors.credentials import register_credential_tools, ingest_image, scan_image, scan_images
from connectors import vault
from core.scheduler import get_scheduler, load_all_active_schedules

# 注册工具（顺序：先内置元工具，再外部连接器）
register_meta_tools()
register_feishu_tools()
register_document_tools()
register_credential_tools()

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

# 上传 / 下载 临时目录
UPLOAD_DIR   = Path(tempfile.gettempdir()) / "jarvis_uploads"
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "jarvis_downloads"
UPLOAD_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)


# ── 前端 ──────────────────────────────────────────────────────────────────────

FRONTEND_DIR = Path(__file__).parent / "frontend"


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = FRONTEND_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ── PWA 静态资源（manifest / service worker / 图标，根作用域）──────────────────

@app.get("/manifest.webmanifest")
async def pwa_manifest():
    return FileResponse(
        FRONTEND_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
async def pwa_service_worker():
    # Service Worker 必须从根路径返回，才能接管整个站点；不缓存 SW 本身
    return FileResponse(
        FRONTEND_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/icon-192.png")
async def pwa_icon_192():
    return FileResponse(FRONTEND_DIR / "icon-192.png", media_type="image/png")


@app.get("/icon-512.png")
async def pwa_icon_512():
    return FileResponse(FRONTEND_DIR / "icon-512.png", media_type="image/png")


@app.get("/icon-maskable-512.png")
async def pwa_icon_maskable():
    return FileResponse(FRONTEND_DIR / "icon-maskable-512.png", media_type="image/png")


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

            # 检查 tool 结果中的特殊动作
            _check_and_send_skill_actions(websocket, controller.messages)
            _check_and_send_file_actions(websocket, controller.messages)
            _check_and_send_credential_actions(websocket, controller.messages)

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


def _check_and_send_file_actions(ws: WebSocket, messages: list):
    for msg in reversed(messages[-6:]):
        content = msg.get("content", "")
        if isinstance(content, str):
            _try_emit_file_action(ws, content)


def _try_emit_file_action(ws: WebSocket, text: str):
    if not isinstance(text, str) or "__file_action__" not in text:
        return
    try:
        start = text.find("{")
        end   = text.rfind("}") + 1
        data  = json.loads(text[start:end])
        if data.get("__file_action__") == "download":
            src = Path(data["file_path"])
            if src.exists():
                dest = DOWNLOAD_DIR / data["filename"]
                shutil.copy2(src, dest)
            asyncio.ensure_future(
                ws.send_text(json.dumps({
                    "type":     "file_download",
                    "filename": data["filename"],
                    "url":      f"/api/download/{data['filename']}",
                    "size":     data.get("size", 0),
                }))
            )
    except Exception:
        pass


def _check_and_send_credential_actions(ws: WebSocket, messages: list):
    """
    扫描最近的 tool 结果，发现 __credential_reveal__ 标记则：
      在本机解密取出【真实】字段值，经 WebSocket 直接推到浏览器显示。
    真实值只走「加密库 → 本机 main.py → 用户浏览器」，绝不进入对话历史 / 云端。
    """
    for msg in reversed(messages[-6:]):
        content = msg.get("content", "")
        if isinstance(content, str):
            _try_emit_credential_reveal(ws, content)


def _try_emit_credential_reveal(ws: WebSocket, text: str):
    if not isinstance(text, str) or "__credential_reveal__" not in text:
        return
    try:
        start = text.find("{")
        end   = text.rfind("}") + 1
        data  = json.loads(text[start:end])
        if not data.get("__credential_reveal__"):
            return
        alias  = data["alias"]
        fields = data.get("fields") or None
        real   = vault.get_fields(alias, only=fields)  # 本机解密
        if not real:
            return
        real.pop("_raw_lines", None)
        meta = vault.get_meta(alias) or {}
        asyncio.ensure_future(ws.send_text(json.dumps({
            "type":       "credential_reveal",
            "alias":      alias,
            "type_label": vault.TYPE_LABELS.get(meta.get("cred_type"), meta.get("cred_type", "")),
            "values":     real,        # 真实值，仅发往本机浏览器
        }, ensure_ascii=False)))
    except Exception:
        pass


# ── 证件保险箱 API（全部本机处理，不经云端模型）─────────────────────────────

@app.get("/api/credentials")
async def api_list_credentials():
    return JSONResponse(vault.list_summary())


@app.post("/api/credentials/scan")
async def api_scan_credential(
    cred_type: str = Form("auto"),
    files: list[UploadFile] = File(...),
):
    """本机 OCR 识别一到多张证件照（如正反面），合并后返回预填字段供确认。
    照片会被加密【暂存】并返回 scan_token；用户在确认时决定是否归档保存，
    放弃的暂存稍后自动清理。临时明文图片用完即删。"""
    vault.purge_stale_staged()
    token = uuid.uuid4().hex
    saved = []
    for i, file in enumerate(files):
        data = await file.read()
        try:
            vault.save_image(f"__stage__{token}", data, file.content_type or "image/jpeg")
        except Exception:
            pass
        dest = UPLOAD_DIR / f"cred_{i}_{safe_filename(file.filename)}"
        dest.write_bytes(data)
        saved.append(dest)
    try:
        result = scan_images([str(p) for p in saved], cred_type)
    except Exception as e:
        result = {"ok": False, "message": f"识别失败：{e}"}
    finally:
        for p in saved:
            try:
                p.unlink()  # 删除临时明文图（加密副本已暂存）
            except Exception:
                pass
    if isinstance(result, dict):
        result["scan_token"] = token
    return JSONResponse(result)


@app.post("/api/credentials/{alias}/reveal")
async def api_reveal_credential(alias: str, body: dict = None):
    """供保险箱面板直接揭示（本机，不经模型）。body 可含 {"fields": ["card_number"]}。"""
    only = (body or {}).get("fields") or None
    real = vault.get_fields(alias, only=only)
    if real is None:
        return JSONResponse({"error": "不存在"}, status_code=404)
    real.pop("_raw_lines", None)
    return JSONResponse({"alias": alias, "values": real})


@app.post("/api/credentials/manual")
async def api_manual_credential(body: dict):
    """手动录入/更新一个证件（在网页表单填写，不经云端模型）。"""
    alias = (body.get("alias") or "").strip()
    if not alias:
        return JSONResponse({"ok": False, "message": "缺少代号"}, status_code=400)
    cred_type = body.get("cred_type", "other")
    fields = body.get("fields", {}) or {}
    expires_at = body.get("expires_at") or None
    note = body.get("note", "")
    vault.save(alias, cred_type, fields, expires_at=expires_at, note=note)

    # 处理暂存的照片：归档或丢弃
    token = body.get("scan_token")
    img_msg = ""
    if token:
        if body.get("keep_images"):
            n = vault.commit_staged(token, alias)
            if n:
                img_msg = f"，并保存 {n} 张照片"
        else:
            vault.discard_staged(token)
    return JSONResponse({"ok": True, "alias": alias, "message": f"已加密保存「{alias}」{img_msg}"})


@app.get("/api/credentials/{alias}/images")
async def api_credential_images(alias: str):
    """列出某证件的照片元数据（id/mime），不含图片内容。"""
    return JSONResponse(vault.list_images(alias))


@app.get("/api/credentials/{alias}/image/{image_id}")
async def api_credential_image(alias: str, image_id: int):
    """返回解密后的图片字节（仅本机显示）。"""
    res = vault.read_image(image_id)
    if res is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    data, mime = res
    return Response(content=data, media_type=mime)


@app.delete("/api/credentials/{alias}/image/{image_id}")
async def api_delete_credential_image(alias: str, image_id: int):
    ok = vault.delete_image(image_id)
    return {"ok": ok}


@app.delete("/api/credentials/{alias}")
async def api_delete_credential(alias: str):
    ok = vault.delete(alias)
    return {"ok": ok}


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
    safe = safe_filename(file.filename)
    dest = UPLOAD_DIR / safe
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"path": str(dest), "filename": safe}


# ── 文件下载接口 ──────────────────────────────────────────────────────────────

@app.get("/api/download/{filename}")
async def download_file(filename: str):
    path = DOWNLOAD_DIR / filename
    if not path.exists():
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


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
