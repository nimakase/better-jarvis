"""证件保险箱 REST API（全部本机处理，不经云端模型）。"""

import uuid

from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response

import config
from core.safety import safe_filename
from connectors import vault
from connectors.credentials import scan_images

router = APIRouter()
UPLOAD_DIR = config.UPLOAD_DIR


@router.get("/api/credentials")
async def api_list_credentials():
    return JSONResponse(vault.list_summary())


@router.post("/api/credentials/scan")
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


@router.post("/api/credentials/{alias}/reveal")
async def api_reveal_credential(alias: str, body: dict = None):
    """供保险箱面板直接揭示（本机，不经模型）。body 可含 {"fields": ["card_number"]}。"""
    only = (body or {}).get("fields") or None
    real = vault.get_fields(alias, only=only)
    if real is None:
        return JSONResponse({"error": "不存在"}, status_code=404)
    real.pop("_raw_lines", None)
    return JSONResponse({"alias": alias, "values": real})


@router.post("/api/credentials/manual")
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


@router.get("/api/credentials/{alias}/images")
async def api_credential_images(alias: str):
    """列出某证件的照片元数据（id/mime），不含图片内容。"""
    return JSONResponse(vault.list_images(alias))


@router.get("/api/credentials/{alias}/image/{image_id}")
async def api_credential_image(alias: str, image_id: int):
    """返回解密后的图片字节（仅本机显示）。"""
    res = vault.read_image(image_id)
    if res is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    data, mime = res
    return Response(content=data, media_type=mime)


@router.delete("/api/credentials/{alias}/image/{image_id}")
async def api_delete_credential_image(alias: str, image_id: int):
    ok = vault.delete_image(image_id)
    return {"ok": ok}


@router.delete("/api/credentials/{alias}")
async def api_delete_credential(alias: str):
    ok = vault.delete(alias)
    return {"ok": ok}
