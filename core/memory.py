"""
记忆子系统 (L5)

四类记忆：
  - 工作记忆：Python 内存，会话结束丢弃（不在此模块，在 controller 里）
  - 长期稳定事实：SQLite memory 表，无过期
  - 带保质期状态：SQLite memory 表，带 expires_at
  - 情节快照：SQLite snapshots 表

敏感字段用 Fernet 加密落盘，密钥存系统钥匙串（keyring）。
"""

import json
import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet
import keyring

import config

# ── 加密 ──────────────────────────────────────────────────────────────────────

KEYRING_SERVICE = "jarvis"
KEYRING_KEY_NAME = "memory_encryption_key"


def _get_or_create_fernet() -> Fernet:
    """从系统钥匙串获取加密密钥，不存在则生成并保存。"""
    key = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY_NAME)
    if not key:
        # 优先用 .env 里配置的
        key = config.MEMORY_ENCRYPTION_KEY or None
    if not key:
        key = Fernet.generate_key().decode()
        keyring.set_password(KEYRING_SERVICE, KEYRING_KEY_NAME, key)
    return Fernet(key.encode() if isinstance(key, str) else key)


_fernet: Optional[Fernet] = None


def _fernet_instance() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = _get_or_create_fernet()
    return _fernet


def encrypt(text: str) -> str:
    return _fernet_instance().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet_instance().decrypt(token.encode()).decode()


# ── 数据库初始化 ──────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    # timeout：撞锁时 sqlite3 自身的等待秒数（与下面的 busy_timeout 配合）。
    conn = sqlite3.connect(str(config.MEMORY_DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # 并发加固：history 每轮落盘 + vault/profile 共用同一个 memory.db，多写易撞
    # "database is locked"。WAL 让写不阻塞读、busy_timeout 让撞锁时自动等待而非
    # 立即报错；synchronous=NORMAL 在 WAL 下是安全且更快的标准搭配。
    # 三条都幂等：journal_mode 是库级持久属性设一次即生效，其余为连接级。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")   # 毫秒
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    """建表，幂等。"""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,       -- JSON 字符串，敏感时为加密密文
                encrypted   INTEGER DEFAULT 0,   -- 1 = Fernet 加密
                source      TEXT,                -- 来源描述（如"用户说"）
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                expires_at  TEXT                 -- NULL = 永久；ISO8601 = 带保质期
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tag         TEXT NOT NULL,        -- 如 "monthly_investment_2026_05"
                summary     TEXT NOT NULL,        -- JSON 摘要
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memory_expires ON memory(expires_at);
            CREATE INDEX IF NOT EXISTS idx_snapshots_tag  ON snapshots(tag);
        """)


# ── 长期记忆 CRUD ─────────────────────────────────────────────────────────────

def write(
    key: str,
    value: Any,
    *,
    source: str = "",
    expires_at: Optional[datetime] = None,
    sensitive: bool = False,
) -> None:
    """写入或覆盖一条记忆。"""
    now = datetime.now(timezone.utc).isoformat()
    raw = json.dumps(value, ensure_ascii=False)
    stored = encrypt(raw) if sensitive else raw
    exp = expires_at.isoformat() if expires_at else None

    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO memory (key, value, encrypted, source, created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value      = excluded.value,
                encrypted  = excluded.encrypted,
                source     = excluded.source,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at
        """, (key, stored, int(sensitive), source, now, now, exp))


def read(key: str) -> Optional[Any]:
    """读取一条记忆，过期或不存在返回 None。"""
    _purge_expired()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value, encrypted FROM memory WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    raw = decrypt(row["value"]) if row["encrypted"] else row["value"]
    return json.loads(raw)


def delete(key: str) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM memory WHERE key = ?", (key,))


def list_all(include_expired: bool = False) -> list[dict]:
    """列出所有记忆条目（调试/用户查看用）。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT key, source, created_at, updated_at, expires_at, encrypted FROM memory"
        ).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    result = []
    for r in rows:
        if not include_expired and r["expires_at"] and r["expires_at"] < now:
            continue
        result.append(dict(r))
    return result


def _purge_expired() -> None:
    """删除已过期的带保质期条目。"""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM memory WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
        )


# ── 情节快照 ──────────────────────────────────────────────────────────────────

def write_snapshot(tag: str, summary: Any) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO snapshots (tag, summary, created_at) VALUES (?, ?, ?)",
            (tag, json.dumps(summary, ensure_ascii=False), now),
        )


def read_snapshots(tag_prefix: str, limit: int = 5) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT tag, summary, created_at FROM snapshots WHERE tag LIKE ? ORDER BY created_at DESC LIMIT ?",
            (f"{tag_prefix}%", limit),
        ).fetchall()
    return [{"tag": r["tag"], "summary": json.loads(r["summary"]), "created_at": r["created_at"]} for r in rows]


# ── 构建注入 system prompt 的动态上下文 ──────────────────────────────────────

def build_context_block(max_items: int = 20) -> str:
    """
    把当前活跃记忆拼成一段文字，注入 system prompt。
    只取最近更新的 max_items 条，避免占用过多 token。
    """
    _purge_expired()
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT key, value, encrypted, expires_at, updated_at
            FROM memory
            ORDER BY updated_at DESC
            LIMIT ?
        """, (max_items,)).fetchall()

    if not rows:
        return ""

    lines = ["【用户记忆】"]
    for r in rows:
        raw = decrypt(r["value"]) if r["encrypted"] else r["value"]
        try:
            val = json.loads(raw)
        except Exception:
            val = raw
        exp = f"（有效期至 {r['expires_at'][:10]}）" if r["expires_at"] else ""
        lines.append(f"- {r['key']}{exp}：{val}")

    return "\n".join(lines)


# 初始化
init_db()
