"""
证件保险箱 —— 注册给主控模型的工具

【安全边界】
  - 模型只接触：代号(alias)、证件类型、字段名、脱敏预览、有效期。
  - reveal_credential 返回给模型的也只是脱敏确认；真实号码由 main.py 读取
    加密库后，经 WebSocket 侧信道直接推到浏览器，永不进入对话历史 / 云端。
"""

import json
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.controller import register_tool
from connectors import vault
from connectors import cred_ocr


# ── 本机识别并保存（不经云端）─────────────────────────────────────────────────

def scan_images(image_paths: list[str], cred_type: str = "auto") -> dict:
    """
    只做本机 OCR + 解析，【不保存】。支持多张图（正反面），合并后解析。
    返回真实预填字段供本机确认表单使用（只在用户本机浏览器出现，不发云端）。
    CVV 不在其中——按设计由用户手动填写。
    """
    parsed = cred_ocr.extract_multi(image_paths, cred_type)
    fields = {k: v for k, v in parsed["fields"].items() if k != "_raw_lines"}
    return {
        "ok": True,
        "cred_type": parsed["cred_type"],
        "type_label": vault.TYPE_LABELS.get(parsed["cred_type"], parsed["cred_type"]),
        "fields": fields,           # 真实值（本机预填）
        "expiry": parsed["expiry"],
        "image_count": parsed.get("image_count", len(image_paths)),
        "raw_lines": parsed.get("raw_lines", []),
    }


def scan_image(image_path: str, cred_type: str = "auto") -> dict:
    """单图便捷封装（兼容旧调用）。"""
    return scan_images([image_path], cred_type)


def ingest_image(image_path: str, alias: str, cred_type: str = "auto", note: str = "") -> dict:
    """
    本机 OCR + 解析 + 加密保存。返回脱敏摘要（dict）。
    供 /api/credentials/ingest 端点与 ingest_credential_image 工具共用。
    """
    parsed = cred_ocr.extract(image_path, cred_type)
    fields = parsed["fields"]
    if not fields or set(fields.keys()) <= {"_raw_lines"}:
        return {
            "ok": False,
            "alias": alias,
            "message": "本地 OCR 未能识别出有效字段，请换更清晰的照片，或在保险箱里手动补充。",
            "raw_lines": parsed.get("raw_lines", []),
        }
    vault.save(alias, parsed["cred_type"], fields, expires_at=parsed["expiry"], note=note)
    summary = vault.get_meta(alias)
    preview = {k: vault.mask_value(k, v) for k, v in fields.items() if k != "_raw_lines"}
    return {
        "ok": True,
        "alias": alias,
        "type": parsed["cred_type"],
        "type_label": vault.TYPE_LABELS.get(parsed["cred_type"], parsed["cred_type"]),
        "expires_at": parsed["expiry"],
        "preview": preview,
        "message": f"已加密保存「{alias}」。",
    }


# ── 工具：列出证件（脱敏，可给模型）───────────────────────────────────────────

async def _t_list_credentials() -> str:
    items = vault.list_summary()
    if not items:
        return "保险箱里还没有证件。可以在网页右上角「保险箱」里上传，或上传证件照片后告诉我代号。"
    lines = ["【证件保险箱（脱敏）】"]
    for c in items:
        exp = f"，有效期至 {c['expires_at']}" if c.get("expires_at") else ""
        prev = "，".join(f"{k}:{v}" for k, v in c["preview"].items() if k != "_raw_lines")
        lines.append(f"- {c['alias']}（{c['type_label']}{exp}）：{prev}")
    lines.append("\n如需查看完整号码/CVV，直接说「给我看 <代号> 的卡号」，号码会安全显示在网页上。")
    return "\n".join(lines)


# ── 工具：揭示某证件字段（真实值走侧信道，不经模型）──────────────────────────

async def _t_reveal_credential(alias: str, fields: str = "") -> str:
    """
    fields: 逗号分隔的字段名（如 "card_number,cvv"），留空=全部。
    返回给模型的只是脱敏确认 + 侧信道标记；真实值由 main.py 推到前端。
    """
    meta = vault.get_meta(alias)
    if meta is None:
        return f"保险箱里没有代号为「{alias}」的证件。可用 list_credentials 查看已有代号。"

    only = [f.strip() for f in fields.split(",") if f.strip()] or None
    real = vault.get_fields(alias, only=only)
    if not real:
        return f"「{alias}」下没有找到{('字段 ' + fields) if fields else '任何字段'}。"

    preview = {k: vault.mask_value(k, v) for k, v in real.items() if k != "_raw_lines"}
    # 这个 JSON 会成为 tool 结果进入对话历史 —— 只含脱敏值，安全。
    return json.dumps({
        "__credential_reveal__": True,
        "alias": alias,
        "fields": [k for k in real.keys() if k != "_raw_lines"],
        "preview": preview,
        "message": f"已在网页上安全显示「{alias}」的：{('、'.join(preview.keys()))}（此处仅脱敏）。",
    }, ensure_ascii=False)


# ── 工具：手动补充/修改字段 ───────────────────────────────────────────────────

async def _t_update_credential(alias: str, field: str, value: str) -> str:
    if vault.get_meta(alias) is None:
        return f"没有代号为「{alias}」的证件。"
    vault.update_field(alias, field, value)
    return f"已更新「{alias}」的 {field}（脱敏：{vault.mask_value(field, value)}）。"


# ── 工具：删除证件 ────────────────────────────────────────────────────────────

async def _t_delete_credential(alias: str) -> str:
    ok = vault.delete(alias)
    return f"已删除证件「{alias}」。" if ok else f"没有代号为「{alias}」的证件。"


# ── 工具：用本机已存图片路径识别保存 ──────────────────────────────────────────

async def _t_ingest_credential_image(image_path: str, alias: str, cred_type: str = "auto", note: str = "") -> str:
    try:
        r = ingest_image(image_path, alias, cred_type, note)
    except RuntimeError as e:
        return str(e)
    if not r["ok"]:
        return r["message"]
    prev = "，".join(f"{k}:{v}" for k, v in r["preview"].items() if k != "_raw_lines")
    exp = f"，有效期至 {r['expires_at']}" if r.get("expires_at") else ""
    return f"{r['message']}类型：{r['type_label']}{exp}。识别到（脱敏）：{prev}"


# ── 工具定义 ──────────────────────────────────────────────────────────────────

CREDENTIAL_TOOL_DEFS = [
    {
        "name": "list_credentials",
        "description": (
            "列出证件保险箱里所有已保存证件的脱敏摘要（代号、类型、有效期、尾号预览）。"
            "当用户问『我有哪些证件 / 银行卡』『XX 什么时候到期』『保险箱里有什么』时使用。"
            "返回内容已脱敏，不含完整号码。"
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "reveal_credential",
        "description": (
            "安全显示某个证件的完整信息。当用户要看完整卡号、CVV、身份证号、护照号等真实值时使用。"
            "真实号码会直接显示在用户网页上，不会发给你；你只会收到脱敏确认。"
            "用 alias（代号）定位证件；fields 指定只看哪些字段（如只问卡号就传 'card_number'）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "alias":  {"type": "string", "description": "证件代号，如 '招行卡'、'身份证'"},
                "fields": {"type": "string", "description": "要显示的字段名，逗号分隔，如 'card_number' 或 'card_number,cvv'；留空=全部。常见字段：card_number/cvv/expiry/bank/id_number/name/birth_date/passport_number/address"},
            },
            "required": ["alias"],
        },
    },
    {
        "name": "ingest_credential_image",
        "description": (
            "对本机上一张证件照片做【本地】OCR 识别并加密保存到保险箱（不经云端）。"
            "当用户给出证件图片路径并希望保存时使用。"
            "image_path=图片完整路径，alias=用户给的代号，cred_type 可选 bank_card/id_card/passport/auto。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "证件图片完整路径"},
                "alias":      {"type": "string", "description": "给这个证件起的代号"},
                "cred_type":  {"type": "string", "description": "bank_card/id_card/passport/auto，默认 auto"},
                "note":       {"type": "string", "description": "备注（可选）"},
            },
            "required": ["image_path", "alias"],
        },
    },
    {
        "name": "update_credential",
        "description": (
            "手动补充或修改某证件的一个字段（OCR 没识别全时用）。"
            "注意：用户在对话里直接说出的值会经过云端模型，敏感号码建议改用网页保险箱里录入。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string", "description": "证件代号"},
                "field": {"type": "string", "description": "字段名，如 expiry、bank、cvv"},
                "value": {"type": "string", "description": "字段值"},
            },
            "required": ["alias", "field", "value"],
        },
    },
    {
        "name": "delete_credential",
        "description": "从保险箱删除某个证件。用户说『删掉 XX 证件』时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string", "description": "要删除的证件代号"},
            },
            "required": ["alias"],
        },
    },
]

CREDENTIAL_HANDLERS = {
    "list_credentials":        _t_list_credentials,
    "reveal_credential":       _t_reveal_credential,
    "ingest_credential_image": _t_ingest_credential_image,
    "update_credential":       _t_update_credential,
    "delete_credential":       _t_delete_credential,
}


def register_credential_tools():
    for defn in CREDENTIAL_TOOL_DEFS:
        register_tool(defn, CREDENTIAL_HANDLERS[defn["name"]])
