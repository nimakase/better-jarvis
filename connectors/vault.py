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
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import config

logger = logging.getLogger("jarvis.vault")
# 复用 memory 子系统的 Fernet 加密与数据库连接（同一个 memory.db，但用独立表）
from core.memory import encrypt, decrypt, _get_conn, _fernet_instance

# 证件照片加密存储目录
IMG_DIR = config.DATA_DIR / "cred_images"
IMG_DIR.mkdir(parents=True, exist_ok=True)

# 文档原件加密存储目录（保单 / 合同 / 协议等）
DOC_DIR = config.DATA_DIR / "vault_docs"
DOC_DIR.mkdir(parents=True, exist_ok=True)

# 文档类型中文名（与证件分开管理）
DOC_TYPE_LABELS = {
    "policy":    "保单",
    "contract":  "合同",
    "agreement": "协议",
    "other":     "其它文档",
}


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

            -- 文档库（保单 / 合同 / 协议等），与证件分表管理
            CREATE TABLE IF NOT EXISTS documents (
                alias       TEXT PRIMARY KEY,    -- 代号，如 "车险保单"
                doc_type    TEXT,                -- policy / contract / agreement / other
                fields_enc  TEXT NOT NULL,       -- Fernet 加密的摘要字段 JSON
                expires_at  TEXT,                -- 到期日 YYYY-MM-DD（低敏感，供提醒）
                note        TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_doc_expires ON documents(expires_at);

            -- 文档原件（加密落盘，保留版本历史）
            CREATE TABLE IF NOT EXISTS doc_files (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                owner       TEXT NOT NULL,       -- 文档代号；暂存阶段为 "__stage__<token>"
                fname       TEXT NOT NULL,       -- 加密文件名（位于 DOC_DIR）
                orig_name   TEXT,                -- 原始文件名
                mime        TEXT,
                size        INTEGER,
                version     INTEGER DEFAULT 1,   -- 版本号 1,2,3...
                is_current  INTEGER DEFAULT 1,   -- 1=当前版本，0=历史
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_docfile_owner ON doc_files(owner);
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
    """返回未来 days 天内到期（或已过期）的证件【和文档】摘要，供提醒用。
    每条带 kind 字段（credential / document）以便区分。"""
    from datetime import date, timedelta
    today = date.today()
    limit = today + timedelta(days=days)
    out = []
    items = [dict(c, kind="credential") for c in list_summary()]
    items += [dict(d, kind="document") for d in list_documents_summary()]
    for c in items:
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
        except FileNotFoundError:
            pass  # 文件已不在，目标即达成
        except Exception as e:
            # DB 行已删但磁盘文件删不掉 → 遗留证件照片，敏感数据需留痕排查
            logger.warning("删除证件图片失败（可能遗留文件）：%s · %s", r["fname"], e)


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
    except FileNotFoundError:
        pass  # 文件已不在，目标即达成
    except Exception as e:
        logger.warning("删除证件图片失败（可能遗留文件）：%s · %s", row["fname"], e)
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
            except FileNotFoundError:
                pass  # 文件已不在，目标即达成
            except Exception as e:
                logger.warning("清理过期暂存图片失败（可能遗留文件）：%s · %s", r["fname"], e)
            with _get_conn() as conn:
                conn.execute("DELETE FROM cred_images WHERE id = ?", (r["id"],))


# ── 文档库 CRUD（摘要字段加密落盘；列表为本机/模型可见的非密元信息）──────────

def save_document(
    alias: str,
    doc_type: str,
    fields: dict,
    *,
    expires_at: Optional[str] = None,
    note: str = "",
) -> None:
    """新增 / 覆盖一条文档记录（按代号 upsert）。摘要字段整体加密。
    created_at 在首次创建时写入，之后更新只动 updated_at。"""
    now = datetime.now(timezone.utc).isoformat()
    enc = encrypt(json.dumps(fields, ensure_ascii=False))
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO documents (alias, doc_type, fields_enc, expires_at, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(alias) DO UPDATE SET
                doc_type   = excluded.doc_type,
                fields_enc = excluded.fields_enc,
                expires_at = excluded.expires_at,
                note       = excluded.note,
                updated_at = excluded.updated_at
        """, (alias, doc_type, enc, expires_at, note, now, now))


def get_document_meta(alias: str) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT alias, doc_type, expires_at, note, created_at, updated_at FROM documents WHERE alias = ?",
            (alias,),
        ).fetchone()
    return dict(row) if row else None


def get_document_fields(alias: str) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute("SELECT fields_enc FROM documents WHERE alias = ?", (alias,)).fetchone()
    if row is None:
        return None
    return _decrypt_fields(row)


def update_document_field(alias: str, field: str, value: str) -> bool:
    fields = get_document_fields(alias)
    if fields is None:
        return False
    fields[field] = value
    meta = get_document_meta(alias)
    save_document(alias, meta["doc_type"], fields,
                  expires_at=meta.get("expires_at"), note=meta.get("note", ""))
    return True


def delete_document(alias: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM documents WHERE alias = ?", (alias,))
    _delete_doc_files_by_owner(alias)
    return cur.rowcount > 0


def rename_document(old_alias: str, new_alias: str) -> bool:
    with _get_conn() as conn:
        try:
            cur = conn.execute(
                "UPDATE documents SET alias = ?, updated_at = ? WHERE alias = ?",
                (new_alias, datetime.now(timezone.utc).isoformat(), old_alias),
            )
            conn.execute("UPDATE doc_files SET owner = ? WHERE owner = ?", (new_alias, old_alias))
        except Exception:
            return False
    return cur.rowcount > 0


def list_documents_summary() -> list[dict]:
    """列出所有文档的摘要（非密元信息：代号、类型、摘要字段、到期日、版本/文件数）。
    可安全发给模型/前端——摘要字段是用户希望"一眼看到"的元信息，不含原件内容。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT alias, doc_type, fields_enc, expires_at, note, updated_at FROM documents ORDER BY updated_at DESC"
        ).fetchall()
    result = []
    for r in rows:
        fields = _decrypt_fields(r)
        files = list_doc_files(r["alias"])
        cur = next((f for f in files if f["is_current"]), None)
        result.append({
            "alias":           r["alias"],
            "doc_type":        r["doc_type"],
            "type_label":      DOC_TYPE_LABELS.get(r["doc_type"], r["doc_type"]),
            "expires_at":      r["expires_at"],
            "note":            r["note"] or "",
            "fields":          fields,                       # 摘要字段（非密）
            "file_count":      len(files),
            "current_version": cur["version"] if cur else None,
            "current_name":    cur["orig_name"] if cur else None,
            "updated_at":      r["updated_at"],
        })
    return result


# ── 文档原件（加密存储 + 版本历史）────────────────────────────────────────────

def save_doc_file(owner: str, data: bytes, orig_name: str = "", mime: str = "application/octet-stream") -> int:
    """加密保存一份文档原件，返回记录 id。
    - 暂存（owner 形如 __stage__<token>）：始终 version=1、is_current=1。
    - 正式 owner：自动取下一个版本号，并把旧的当前版本降级为历史。"""
    fname = f"{uuid.uuid4().hex}.enc"
    (DOC_DIR / fname).write_bytes(_enc_bytes(data))
    now = datetime.now(timezone.utc).isoformat()
    staged = owner.startswith("__stage__")
    with _get_conn() as conn:
        if staged:
            version = 1
        else:
            row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM doc_files WHERE owner = ?", (owner,)).fetchone()
            version = (row["v"] or 0) + 1
            conn.execute("UPDATE doc_files SET is_current = 0 WHERE owner = ?", (owner,))
        cur = conn.execute(
            "INSERT INTO doc_files (owner, fname, orig_name, mime, size, version, is_current, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (owner, fname, orig_name, mime, len(data), version, now),
        )
        return cur.lastrowid


def list_doc_files(owner: str) -> list[dict]:
    """列出某文档的所有原件版本（新→旧）。不含文件内容。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, orig_name, mime, size, version, is_current, created_at "
            "FROM doc_files WHERE owner = ? ORDER BY version DESC", (owner,)
        ).fetchall()
    return [dict(r) for r in rows]


def read_doc_file(file_id: int):
    """返回 (bytes, mime, orig_name)；不存在返回 None。仅解密到内存供本机查看/下载。"""
    with _get_conn() as conn:
        row = conn.execute("SELECT fname, mime, orig_name FROM doc_files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        return None
    p = DOC_DIR / row["fname"]
    if not p.exists():
        return None
    try:
        return _dec_bytes(p.read_bytes()), (row["mime"] or "application/octet-stream"), (row["orig_name"] or "document")
    except Exception:
        return None


def set_current_doc_file(owner: str, file_id: int) -> bool:
    """把某历史版本设为当前版本（回滚 / 切换）。"""
    with _get_conn() as conn:
        row = conn.execute("SELECT id FROM doc_files WHERE id = ? AND owner = ?", (file_id, owner)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE doc_files SET is_current = 0 WHERE owner = ?", (owner,))
        conn.execute("UPDATE doc_files SET is_current = 1 WHERE id = ?", (file_id,))
    return True


def delete_doc_file(file_id: int) -> bool:
    with _get_conn() as conn:
        row = conn.execute("SELECT fname FROM doc_files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM doc_files WHERE id = ?", (file_id,))
    try:
        (DOC_DIR / row["fname"]).unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("删除文档原件失败（可能遗留文件）：%s · %s", row["fname"], e)
    return True


def _delete_doc_files_by_owner(owner: str) -> None:
    with _get_conn() as conn:
        rows = conn.execute("SELECT fname FROM doc_files WHERE owner = ?", (owner,)).fetchall()
        conn.execute("DELETE FROM doc_files WHERE owner = ?", (owner,))
    for r in rows:
        try:
            (DOC_DIR / r["fname"]).unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("删除文档原件失败（可能遗留文件）：%s · %s", r["fname"], e)


def commit_staged_docs(token: str, alias: str) -> int:
    """把暂存的文档原件归档到某代号名下，并按版本号续接（旧版本降级为历史）。
    返回归档的文件数。"""
    staged_owner = f"__stage__{token}"
    with _get_conn() as conn:
        staged = conn.execute(
            "SELECT id FROM doc_files WHERE owner = ? ORDER BY id", (staged_owner,)
        ).fetchall()
        if not staged:
            return 0
        row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM doc_files WHERE owner = ?", (alias,)).fetchone()
        base = row["v"] or 0
        # 旧的当前版本降级
        conn.execute("UPDATE doc_files SET is_current = 0 WHERE owner = ?", (alias,))
        n = 0
        for i, s in enumerate(staged, 1):
            is_cur = 1 if i == len(staged) else 0   # 最后一份作为当前版本
            conn.execute(
                "UPDATE doc_files SET owner = ?, version = ?, is_current = ? WHERE id = ?",
                (alias, base + i, is_cur, s["id"]),
            )
            n += 1
        return n


def discard_staged_docs(token: str) -> None:
    _delete_doc_files_by_owner(f"__stage__{token}")


def purge_stale_staged_docs(max_age_sec: int = 3600) -> None:
    """清理超时未归档的暂存文档原件。"""
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_sec
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, fname, created_at FROM doc_files WHERE owner LIKE '__stage__%'"
        ).fetchall()
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["created_at"]).timestamp()
        except Exception:
            ts = 0
        if ts < cutoff:
            try:
                (DOC_DIR / r["fname"]).unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning("清理过期暂存文档失败（可能遗留文件）：%s · %s", r["fname"], e)
            with _get_conn() as conn:
                conn.execute("DELETE FROM doc_files WHERE id = ?", (r["id"],))


# 初始化
init_vault()
