"""
文档保险箱 —— 保单 / 合同 / 协议等的存储与工具

设计：
  - 原件（PDF/Word/图片等）在本机 AES 加密落盘，保留版本历史。
  - 「一眼摘要」由本地读取文字后，经云端模型提取关键字段（用户已选 AI 自动提取）。
    摘要是非密元信息（保险公司/保额/到期日等），可安全展示并给模型查询；
    原件内容仅在提取那一刻经过模型一次，之后不再上云。
  - 到期日单列，复用证件保险箱的到期提醒。
"""

import json
import re
from functools import partial

import config
from core.registry import tool as _tool
from connectors import vault

tool = partial(_tool, group="docvault")


# 各文档类型建议提取的字段（用于提示模型；模型可按实际增减）
DOC_FIELD_HINTS = {
    "policy":    "保险公司、险种、保单号、保额、年保费、投保人、被保险人、生效日、到期日",
    "contract":  "合同名称、对方、标的/事项、合同金额、签订日、到期日",
    "agreement": "协议名称、对方、主题、签订日、到期日",
    "other":     "文档标题、相关方、关键日期、关键金额等最重要的几项",
}


def _extract_text(path: str) -> str:
    """本地读取文档文字（复用 document 连接器的同步提取）。"""
    from pathlib import Path
    from connectors import document as doc

    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return doc._read_pdf(path, None)
    if ext in (".docx", ".doc"):
        return doc._read_docx(path)
    if ext in (".xlsx", ".xls", ".xlsm"):
        return doc._read_xlsx(path, None)
    if ext in (".pptx", ".ppt"):
        return doc._read_pptx(path)
    if ext == ".csv":
        return doc._read_csv(path)
    if ext in (".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml"):
        return doc._read_text(path)
    return ""   # 图片等走视觉提取，见 extract_summary


def _parse_json(s: str) -> dict:
    """从模型输出里抠出 JSON（容忍 ```json 包裹 / 前后噪声）。"""
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
        return {}


async def extract_summary(path: str, doc_type: str = "auto") -> dict:
    """本地读文字 → 云端模型提取摘要字段。返回
    {ok, doc_type, fields(dict 中文键), expires_at, raw_excerpt}。"""
    from pathlib import Path
    from openai import AsyncOpenAI

    ext = Path(path).suffix.lower()
    is_image = ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")

    type_for_hint = doc_type if doc_type in DOC_FIELD_HINTS else "other"
    hint = DOC_FIELD_HINTS[type_for_hint]
    label = vault.DOC_TYPE_LABELS.get(doc_type, "文档")

    instruction = (
        f"你是文档信息提取助手。请从这份「{label}」中提取关键摘要信息，"
        f"只输出严格 JSON，格式：\n"
        '{"doc_type": "policy|contract|agreement|other", '
        '"fields": {"字段名": "值"}, "expires_at": "YYYY-MM-DD 或 null"}\n'
        f"建议字段：{hint}。字段名用简洁中文；找不到的不要编造、直接省略；"
        "金额保留单位；所有日期统一为 YYYY-MM-DD；到期/失效日同时填入 expires_at。"
        "只输出 JSON，不要任何解释文字。"
    )

    client = AsyncOpenAI(api_key=config.OPENROUTER_API_KEY, base_url=config.OPENROUTER_BASE_URL)

    if is_image:
        import base64
        mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif"}
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        content = [
            {"type": "image_url", "image_url": {"url": f"data:{mime_map.get(ext,'image/png')};base64,{b64}"}},
            {"type": "text", "text": instruction},
        ]
        raw_excerpt = "[图片文档]"
    else:
        text = _extract_text(path)
        if not text.strip():
            return {"ok": False, "message": "无法从该文件读取文字内容，可改用手动录入。"}
        snippet = text[:15000]
        raw_excerpt = text[:1500]
        content = instruction + "\n\n文档内容：\n" + snippet

    try:
        resp = await client.chat.completions.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": content}],
        )
        parsed = _parse_json(resp.choices[0].message.content or "")
    except Exception as e:
        return {"ok": False, "message": f"提取失败：{e}"}

    fields = parsed.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    fields = {str(k): str(v) for k, v in fields.items() if v not in (None, "", "null")}
    expires_at = parsed.get("expires_at")
    if expires_at in ("null", "", None):
        expires_at = None
    guessed = parsed.get("doc_type")
    final_type = doc_type if doc_type in vault.DOC_TYPE_LABELS else (guessed if guessed in vault.DOC_TYPE_LABELS else "other")

    return {
        "ok": True,
        "doc_type": final_type,
        "type_label": vault.DOC_TYPE_LABELS.get(final_type, final_type),
        "fields": fields,
        "expires_at": expires_at,
        "raw_excerpt": raw_excerpt,
    }


async def ingest_document(path: str, alias: str, doc_type: str = "auto", note: str = "",
                          store_file: bool = True) -> dict:
    """提取摘要 + 保存文档 + 加密归档原件（按版本）。
    若 alias 已存在则视为更新：合并字段、原件追加为新版本。返回摘要 dict。"""
    summary = await extract_summary(path, doc_type)
    if not summary.get("ok"):
        return {"ok": False, "alias": alias, "message": summary.get("message", "提取失败")}

    existing = vault.get_document_fields(alias)
    fields = dict(existing or {})
    fields.update(summary["fields"])     # 新提取覆盖同名字段

    vault.save_document(alias, summary["doc_type"], fields,
                        expires_at=summary.get("expires_at"), note=note)

    file_msg = ""
    if store_file:
        try:
            with open(path, "rb") as f:
                data = f.read()
            from pathlib import Path as _P
            import mimetypes
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            vault.save_doc_file(alias, data, orig_name=_P(path).name, mime=mime)
            files = vault.list_doc_files(alias)
            file_msg = f"（原件已加密保存，当前为第 {files[0]['version']} 版）" if files else ""
        except Exception as e:
            file_msg = f"（原件保存失败：{e}）"

    return {
        "ok": True,
        "alias": alias,
        "doc_type": summary["doc_type"],
        "type_label": summary["type_label"],
        "fields": fields,
        "expires_at": summary.get("expires_at"),
        "updated": existing is not None,
        "message": f"已{'更新' if existing is not None else '保存'}「{alias}」{file_msg}",
    }


# ── 工具：列出文档（摘要可给模型）─────────────────────────────────────────────

@tool(
    "list_documents",
    (
        "列出【文档保险箱】里所有文档的摘要（代号、类型、关键字段如保险公司/保额/到期日、版本数）。"
        "用户问『我有哪些保单/合同』『XX 保单什么时候到期』时使用。"
        "只管保单、合同、协议等文档——查证件/银行卡请改用 list_credentials。"
    ),
    {"type": "object", "properties": {}},
)
async def _t_list_documents() -> str:
    items = vault.list_documents_summary()
    if not items:
        return "文档保险箱里还没有文档。可在网页「保险箱 → 文档」里上传保单/合同，我会自动提取摘要。"
    lines = ["【文档保险箱】"]
    for d in items:
        exp = f"，到期 {d['expires_at']}" if d.get("expires_at") else ""
        ver = f"，v{d['current_version']}" if d.get("current_version") else ""
        summary = "；".join(f"{k}:{v}" for k, v in (d.get("fields") or {}).items())
        lines.append(f"- {d['alias']}（{d['type_label']}{exp}{ver}）：{summary or '（无摘要字段）'}")
    return "\n".join(lines)


# ── 工具：用本机文件路径录入 / 更新文档 ───────────────────────────────────────

@tool(
    "ingest_document_file",
    (
        "把本机一份文档（保单/合同/协议等，PDF/Word/图片）读取并自动提取摘要后加密保存到文档保险箱。"
        "若该代号已存在，则视为【更新】：原件追加为新版本、摘要字段合并更新。"
        "用户说『把这份保单存进保险箱』或『更新 XX 保单（给了新文件）』时使用。"
        "【用于归档保存，不用于临时阅读】——只想看内容用 read_document。"
        "path=文件完整路径，alias=代号，doc_type 可选 policy/contract/agreement/other/auto。"
    ),
    {
        "type": "object",
        "properties": {
            "path":     {"type": "string", "description": "文档文件完整路径"},
            "alias":    {"type": "string", "description": "给文档起的代号，如 '车险保单'"},
            "doc_type": {"type": "string", "description": "policy/contract/agreement/other/auto，默认 auto"},
            "note":     {"type": "string", "description": "备注（可选）"},
        },
        "required": ["path", "alias"],
    },
)
async def _t_ingest_document_file(path: str, alias: str, doc_type: str = "auto", note: str = "") -> str:
    import os
    path = path.strip().strip('"').strip("'")
    if not os.path.exists(path):
        return f"文件不存在：{path}"
    r = await ingest_document(path, alias, doc_type, note)
    if not r["ok"]:
        return r["message"]
    summary = "；".join(f"{k}:{v}" for k, v in (r.get("fields") or {}).items())
    exp = f"，到期 {r['expires_at']}" if r.get("expires_at") else ""
    return f"{r['message']}。类型：{r['type_label']}{exp}。摘要：{summary or '（未提取到字段，可在网页补充）'}"


# ── 工具：更新文档某个摘要字段 ────────────────────────────────────────────────

@tool(
    "update_document",
    "更新文档保险箱里某文档的一个摘要字段（不换原件，只改字段）。如改保额、到期日、对方等。",
    {
        "type": "object",
        "properties": {
            "alias": {"type": "string", "description": "文档代号"},
            "field": {"type": "string", "description": "字段名（中文），如 保额、到期日、对方"},
            "value": {"type": "string", "description": "字段值"},
        },
        "required": ["alias", "field", "value"],
    },
)
async def _t_update_document(alias: str, field: str, value: str) -> str:
    if vault.get_document_meta(alias) is None:
        return f"文档保险箱里没有代号为「{alias}」的文档。"
    vault.update_document_field(alias, field, value)
    # 到期日字段同步到 expires_at（供提醒）
    if field in ("到期日", "失效日", "到期", "expires_at"):
        meta = vault.get_document_meta(alias)
        vault.save_document(alias, meta["doc_type"], vault.get_document_fields(alias) or {},
                            expires_at=value, note=meta.get("note", ""))
    return f"已更新「{alias}」的 {field} 为：{value}。"


# ── 工具：删除文档 ────────────────────────────────────────────────────────────

@tool(
    "delete_document",
    "从文档保险箱删除某文档（含所有版本原件）。用户说『删掉 XX 保单/合同』时使用。",
    {
        "type": "object",
        "properties": {"alias": {"type": "string", "description": "要删除的文档代号"}},
        "required": ["alias"],
    },
)
async def _t_delete_document(alias: str) -> str:
    ok = vault.delete_document(alias)
    return f"已删除文档「{alias}」及其所有版本原件。" if ok else f"没有代号为「{alias}」的文档。"
