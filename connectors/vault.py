"""
证件保险箱 (Credential Vault)

独立于普通记忆库的高敏感加密存储，专门存证件 / 银行卡等信息。

【关键安全设计】
  1. 所有字段值用 Fernet 加密落盘（密钥在系统钥匙串，复用 core.memory 的密钥）。
  2. 用独立的 credentials 表，与 memory 表分离 —— 因此【绝不会】被
     core.memory.build_context_block() 注入到发往云端模型的 system prompt。
  3. 云端模型只接触"代号(alias)"和脱敏预览（尾号 1234），真实号码 / CVV
     绝不进入对话历史，也绝不发给 OpenRouter。
  4. 真实值只在两处出现：磁盘上的加密密文，以及用户本机浏览器（经 WebSocket
     侧信道直接推送）。
"""

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import config
# 复用 memory 子系统的 Fernet 加密与数据库连接（同一个 memory.db，但用独立表）
from core.memory import encrypt, decrypt, _get_conn, _fernet_instance

# 证件照片加密存储目录
IMG_DIR = config.DATA_DIR / "cred_images"
IMG_DIR.mkdir(parents=True, exist_ok=True)


# ── 字段敏感度分级 ────────────────────────────────────────────────────────────
# 完全保密：列表 / 预览里只显示脱敏掩码，必须显式 reveal 才取真实值
SECRET_FIELDS = {
    "card_number", "cvv", "cvc", "security_code", "pin",
    "id_number", "passport_number", "license_number", "account_number",
}
# 这些字段名展示时按"号码"处理（保留尾 4 位）
NUMBERISH_FIELDS = {
    "card_number", "id_number", "passport_number", "license_number", "account_number",
}

# 证件类型的中文名
TYPE_LABELS = {
    "bank_card":      "银行卡",
    "id_card":        "身份证",
    "passport":       "护照",
    "driver_license": "驾驶证",
    "other":          "其它证件",
}


# ── 建表 ──────────────────────────────────────────────────────────────────────

def init_vault() -> None:
    """建证件表，幂等。"""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS credentials (
                alias       TEXT PRIMARY KEY,    -- 代号（模型可见，非敏感），如 "招行卡"
                cred_type   TEXT,                -- bank_card / id_card / passport / ...
                fields_enc  TEXT NOT NULL,       -- Fernet 加密的 JSON 字段字典
                expires_at  TEXT,                -- 明文有效期 YYYY-MM-DD（低敏感，供提醒/列表）
                note        TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cred_expires ON credentials(expires_at);

            CREATE TABLE IF NOT EXISTS cred_images (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                owner       TEXT NOT NULL,       -- 证件代号；暂存阶段为 "__stage__<token>"
                fname       TEXT NOT NULL,       -- 加密图片文件名（位于 IMG_DIR）
                mime        TEXT,
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_credimg_owner ON cred_images(owner);
        """)


# ── 脱敏 ──────────────────────────────────────────────────────────────────────

def mask_value(field: str, value: Any) -> str:
    """生成脱敏预览。号码类保留尾 4 位，CVV/PIN 全部隐藏，其余非保密字段原样返回。"""
    v = str(value)
    if field in ("cvv", "cvc", "security_code", "pin"):
        return "•" * max(len(re.sub(r"\D", "", v)), 3)
    if field in NUMBERISH_FIELDS:
        digits = re.sub(r"\D", "", v)
        return ("•••• " + digits[-4:]) if len(digits) >= 4 else "••••"
    if field in SECRET_FIELDS:
        return "••••"
    return v  # 非保密字段（姓名、银行、有效期等）原样


def _masked_fields(fields: dict) -> dict:
    """整条记录的脱敏视图，供模型 / 列表使用。"""
    return {k: mask_value(k, val) for k, val in fields.items()}


# ── CRUD ──────────────────────────────────────────────────────────────────────

def save(
    alias: str,
    cred_type: str,
    fields: dict,
    *,
    expires_at: Optional[str] = None,
    note: str = "",
) -> None:
    """新增 / 覆盖一条证件记录。fields 整体加密。"""
    now = datetime.now(timezone.utc).isoformat()
    enc = encrypt(json.dumps(fields, ensure_ascii=False))
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO credentials (alias, cred_type, fields_enc, expires_at, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(alias) DO UPDATE SET
                cred_type  = excluded.cred_type,
                fields_enc = excluded.fields_enc,
                expires_at = excluded.expires_at,
                note       = excluded.note,
                updated_at = excluded.updated_at
        """, (alias, cred_type, enc, expires_at, note, now, now))


def _decrypt_fields(row) -> dict:
    try:
        return json.loads(decrypt(row["fields_enc"]))
    except Exception:
        return {}


def get_fields(alias: str, only: Optional[list[str]] = None) -> Optional[dict]:
    """
    取某证件的【真实】字段值。仅供本机侧信道使用（main.py reveal），
    绝不可把返回值放进发往云端模型的消息里。
    only: 只取指定字段名；None=全部。
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT fields_enc FROM credentials WHERE alias = ?", (alias,)
        ).fetchone()
    if row is None:
        return None
    fields = _decrypt_fields(row)
    if only:
        return {k: fields[k] for k in only if k in fields}
    return fields


def update_field(alias: str, field: str, value: str) -> bool:
    """手动补充 / 修改单个字段。"""
    fields = get_fields(alias)
    if fields is None:
        return False
    fields[field] = value
    row = get_meta(alias)
    save(alias, row["cred_type"], fields,
         expires_at=row.get("expires_at"), note=row.get("note", ""))
    return True


def set_expiry(alias: str, expires_at: str) -> bool:
    meta = get_meta(alias)
    if meta is None:
        return False
    fields = get_fields(alias) or {}
    save(alias, meta["cred_type"], fields, expires_at=expires_at, note=meta.get("note", ""))
    return True


def get_meta(alias: str) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT alias, cred_type, expires_at, note, created_at, updated_at FROM credentials WHERE alias = ?",
            (alias,),
        ).fetchone()
    return dict(row) if row else None


def delete(alias: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM credentials WHERE alias = ?", (alias,))
    _delete_images_by_owner(alias)   # 级联删除照片
    return cur.rowcount > 0


def rename(old_alias: str, new_alias: str) -> bool:
    with _get_conn() as conn:
        try:
            cur = conn.execute(
                "UPDATE credentials SET alias = ?, updated_at = ? WHERE alias = ?",
                (new_alias, datetime.now(timezone.utc).isoformat(), old_alias),
            )
            conn.execute("UPDATE cred_images SET owner = ? WHERE owner = ?", (new_alias, old_alias))
        except Exception:
            return False
    return cur.rowcount > 0


# ── 列表 / 摘要（脱敏，可安全给模型）──────────────────────────────────────────

def list_summary() -> list[dict]:
    """
    列出所有证件的【脱敏】摘要，可安全发给云端模型或前端。
    不含任何真实保密值。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT alias, cred_type, fields_enc, expires_at, note, updated_at FROM credentials ORDER BY updated_at DESC"
        ).fetchall()
    result = []
    for r in rows:
        fields = _decrypt_fields(r)
        result.append({
            "alias":       r["alias"],
            "type":        r["cred_type"],
            "type_label":  TYPE_LABELS.get(r["cred_type"], r["cred_type"]),
            "expires_at":  r["expires_at"],
            "note":        r["note"] or "",
            "fields":      list(fields.keys()),          # 字段名，非值
            "preview":     _masked_fields(fields),       # 脱敏预览
            "image_count": count_images(r["alias"]),
        })
    return result


def expiring_within(days: int) -> list[dict]:
    """返回未来 days 天内到期（或已过期）的证件摘要，供提醒用。"""
    from datetime import date, timedelta
    today = date.today()
    limit = today + timedelta(days=days)
    out = []
    for c in list_summary():
        exp = c.get("expires_at")
        if not exp:
            continue
        try:
            d = datetime.strptime(exp[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if d <= limit:
            c["days_left"] = (d - today).days
            out.append(c)
    return sorted(out, key=lambda x: x["days_left"])


# ── 证件照片（加密存储）────────────────────────────────────────────────────────

def _enc_bytes(data: bytes) -> bytes:
    return _fernet_instance().encrypt(data)


def _dec_bytes(token: bytes) -> bytes:
    return _fernet_instance().decrypt(token)


def save_image(owner: str, data: bytes, mime: str = "image/jpeg") -> int:
    """加密保存一张图片，返回记录 id。owner 为代号或暂存 token。"""
    fname = f"{uuid.uuid4().hex}.enc"
    (IMG_DIR / fname).write_bytes(_enc_bytes(data))
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO cred_images (owner, fname, mime, created_at) VALUES (?, ?, ?, ?)",
            (owner, fname, mime, now),
        )
        return cur.lastrowid


def list_images(owner: str) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, mime, created_at FROM cred_images WHERE owner = ? ORDER BY id", (owner,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_images(owner: str) -> int:
    with _get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM cred_images WHERE owner = ?", (owner,)).fetchone()
    return row["n"] if row else 0


def read_image(image_id: int):
    """返回 (bytes, mime)；不存在返回 None。真实图片只解密到内存供本机显示。"""
    with _get_conn() as conn:
        row = conn.execute("SELECT fname, mime FROM cred_images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        return None
    p = IMG_DIR / row["fname"]
    if not p.exists():
        return None
    try:
        return _dec_bytes(p.read_bytes()), (row["mime"] or "image/jpeg")
    except Exception:
        return None


def _delete_images_by_owner(owner: str) -> None:
    with _get_conn() as conn:
        rows = conn.execute("SELECT fname FROM cred_images WHERE owner = ?", (owner,)).fetchall()
        conn.execute("DELETE FROM cred_images WHERE owner = ?", (owner,))
    for r in rows:
        try:
            (IMG_DIR / r["fname"]).unlink()
        except Exception:
            pass


def delete_images(alias: str) -> None:
    _delete_images_by_owner(alias)


def delete_image(image_id: int) -> bool:
    with _get_conn() as conn:
        row = conn.execute("SELECT fname FROM cred_images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM cred_images WHERE id = ?", (image_id,))
    try:
        (IMG_DIR / row["fname"]).unlink()
    except Exception:
        pass
    return True


def commit_staged(token: str, alias: str) -> int:
    """把暂存图片归档到某代号名下，返回归档数量。"""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE cred_images SET owner = ? WHERE owner = ?", (alias, f"__stage__{token}")
        )
        return cur.rowcount


def discard_staged(token: str) -> None:
    _delete_images_by_owner(f"__stage__{token}")


def purge_stale_staged(max_age_sec: int = 3600) -> None:
    """清理超过 max_age_sec 仍未归档的暂存图片（用户放弃保存的情况）。"""
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_sec
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, fname, created_at FROM cred_images WHERE owner LIKE '__stage__%'"
        ).fetchall()
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["created_at"]).timestamp()
        except Exception:
            ts = 0
        if ts < cutoff:
            try:
                (IMG_DIR / r["fname"]).unlink()
            except Exception:
                pass
            with _get_conn() as conn:
                conn.execute("DELETE FROM cred_images WHERE id = ?", (r["id"],))


# 初始化
init_vault()
