"""
对话持久层（chat history）

把对话 transcript 落盘到 SQLite（复用 core.memory 的同一个 memory.db 与连接助手），
让用户在刷新、关标签、换设备后仍能看到此前的对话——这是工作记忆(controller.messages)
之外的"可回看记录"层。

设计为多会话（conversation_id）结构，便于将来做会话列表 / 切换；当前前端只用一个
固定的 DEFAULT_CONVERSATION，但 schema 与 API 已经按多会话写好，扩展零迁移。

说明：
- transcript 内容本就会发往云端模型，故此处不额外加密（与证件保险箱的强隔离无关）。
- 这里只存"给人看"的消息（user 文本、assistant 文本、以及文件/证件这类卡片的轻量占位），
  不存工具调用的中间过程；模型的工具调用上下文仍由 controller.messages 在内存里维护。
"""

import json
from datetime import datetime, timezone
from typing import Any, Optional

from core.memory import _get_conn  # 复用同一个 memory.db 与连接方式

DEFAULT_CONVERSATION = "default"


def init_db() -> None:
    """建表，幂等。"""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id          TEXT PRIMARY KEY,
                title       TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role            TEXT NOT NULL,      -- user / assistant / system
                kind            TEXT NOT NULL,      -- text / file / credential / tool
                content         TEXT NOT NULL,      -- 文本；卡片类存 JSON 字符串
                created_at      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chatmsg_conv
                ON chat_messages(conversation_id, id);
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_conversation(conversation_id: str = DEFAULT_CONVERSATION,
                        title: str = "主对话") -> None:
    now = _now()
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO conversations (id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
        """, (conversation_id, title, now, now))


def append(role: str, content: Any, *, kind: str = "text",
           conversation_id: str = DEFAULT_CONVERSATION) -> None:
    """追加一条消息。content 为非字符串时按 JSON 落盘（卡片类）。"""
    ensure_conversation(conversation_id)
    now = _now()
    stored = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO chat_messages (conversation_id, role, kind, content, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (conversation_id, role, kind, stored, now))
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?",
                     (now, conversation_id))


def get_messages(conversation_id: str = DEFAULT_CONVERSATION,
                 limit: int = 500) -> list[dict]:
    """按时间正序返回最近 limit 条消息（供前端回放）。"""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT role, kind, content, created_at
            FROM chat_messages
            WHERE conversation_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (conversation_id, limit)).fetchall()
    out = []
    for r in reversed(rows):
        content: Any = r["content"]
        if r["kind"] != "text":
            try:
                content = json.loads(r["content"])
            except Exception:
                pass
        out.append({
            "role": r["role"],
            "kind": r["kind"],
            "content": content,
            "created_at": r["created_at"],
        })
    return out


def list_conversations() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT id, title, created_at, updated_at
            FROM conversations
            ORDER BY updated_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def clear(conversation_id: str = DEFAULT_CONVERSATION) -> None:
    """清空某会话的全部消息（保留会话本身）。"""
    with _get_conn() as conn:
        conn.execute("DELETE FROM chat_messages WHERE conversation_id = ?",
                     (conversation_id,))
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?",
                     (_now(), conversation_id))


# 初始化
init_db()
