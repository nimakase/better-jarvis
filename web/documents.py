"""文档保险箱 REST API（保单/合同/协议等，全部本机处理）。

提取摘要会把原件文字经云端模型一次（用户已选 AI 自动提取）；原件本身只
在本机 AES 加密落盘，保留版本历史。
"""

import uuid
from urllib.parse import quote

from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response

import config
from core.safety import safe_filename
from connectors import vault
from connectors.doc_vault import extract_summary

router = APIRouter()
UPLOAD_DIR = config.UPLOAD_DIR


@router.get("/api/documents")
async def api_list_documents():
    return JSONResponse(vault.list_documents_summary())


@router.get("/api/documents/{alias}/fields")
async def api_document_fields(alias: str):
    """取某文档的摘要字段（非密，用于网页预填编辑表单）。"""
    fields = vault.get_document_fields(alias)
    if fields is None:
        return JSONResponse({"error": "不存在"}, status_code=404)
    meta = vault.get_document_meta(alias) or {}
    return JSONResponse({"alias": alias, "doc_type": meta.get("doc_type"),
                         "expires_at": meta.get("expires_at"), "note": meta.get("note", ""),
                         "fields": fields})


@router.post("/api/documents/scan")
async def api_scan_document(
    doc_type: str = Form("auto"),
    files: list[UploadFile] = File(...),
):
    """上传一份文档原件 → 本机加密暂存 + AI 提取摘要，返回预填字段供核对。
    返回 scan_token；用户确认时归档为版本，放弃则稍后自动清理。"""
    vault.purge_stale_staged_docs()
    token = uuid.uuid4().hex
    saved = []
    for i, file in enumerate(files):
        data = await file.read()
        try:
            import mimetypes
            mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
            vault.save_doc_file(f"__stage__{token}", data, orig_name=file.filename or f"file{i}", mime=mime)
        except Exception:
            pass
        dest = UPLOAD_DIR / f"doc_{i}_{safe_filename(file.filename)}"
        dest.write_bytes(data)
        saved.append(dest)
    try:
        result = await extract_summary(str(saved[0]), doc_type) if saved else {"ok": False, "message": "没有文件"}
    except Exception as e:
        result = {"ok": False, "message": f"提取失败：{e}"}
    finally:
        for p in saved:
            try:
                p.unlink()   # 删临时明文（加密副本已暂存）
            except Exception:
                pass
    if isinstance(result, dict):
        result["scan_token"] = token
    return JSONResponse(result)


@router.post("/api/documents/manual")
async def api_manual_document(body: dict):
    """保存/更新一个文档摘要（网页表单填写，不经云端）。可带 scan_token 归档原件。"""
    alias = (body.get("alias") or "").strip()
    if not alias:
        return JSONResponse({"ok": False, "message": "缺少代号"}, status_code=400)
    doc_type = body.get("doc_type", "other")
    fields = body.get("fields", {}) or {}
    expires_at = body.get("expires_at") or None
    note = body.get("note", "")
    existed = vault.get_document_meta(alias) is not None
    vault.save_document(alias, doc_type, fields, expires_at=expires_at, note=note)

    token = body.get("scan_token")
    file_msg = ""
    if token:
        if body.get("keep_file", True):
            n = vault.commit_staged_docs(token, alias)
            if n:
                files = vault.list_doc_files(alias)
                file_msg = f"，原件已存为第 {files[0]['version']} 版"
        else:
            vault.discard_staged_docs(token)
    verb = "更新" if existed else "保存"
    return JSONResponse({"ok": True, "alias": alias, "message": f"已加密{verb}「{alias}」{file_msg}"})


@router.get("/api/documents/{alias}/files")
async def api_document_files(alias: str):
    """列出某文档的所有原件版本（新→旧）。"""
    return JSONResponse(vault.list_doc_files(alias))


@router.get("/api/documents/{alias}/file/{file_id}")
async def api_document_file(alias: str, file_id: int):
    """返回解密后的原件字节（仅本机查看/下载）。"""
    res = vault.read_doc_file(file_id)
    if res is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    data, mime, orig = res
    headers = {"Content-Disposition": f"inline; filename*=UTF-8''{quote(orig)}"}
    return Response(content=data, media_type=mime, headers=headers)


@router.post("/api/documents/{alias}/file/{file_id}/current")
async def api_set_current_file(alias: str, file_id: int):
    ok = vault.set_current_doc_file(alias, file_id)
    return {"ok": ok}


@router.delete("/api/documents/{alias}/file/{file_id}")
async def api_delete_document_file(alias: str, file_id: int):
    ok = vault.delete_doc_file(file_id)
    return {"ok": ok}


@router.delete("/api/documents/{alias}")
async def api_delete_document(alias: str):
    ok = vault.delete_document(alias)
    return {"ok": ok}
