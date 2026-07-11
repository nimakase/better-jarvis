"""
用户档案 / core memory（常驻长期记忆）

MemGPT/Letta 式 "core memory" 的轻量单用户版：一小块【始终注入 system prompt】、
可被模型追加、可被用户在设置面板增删的长期稳定事实（风险偏好、家庭成员、长期目标、
关键日期等）。它解决"老事实永不淡化"——与 core/history.py 的逐字 transcript 互补：
  - history.py = 原始对话流（会被压缩 / 滚出窗口）
  - profile.py = 蒸馏后的少量硬事实（小而精、每轮钉住）

刻意保持小：超出上限就拒绝新增（提示用户先精简），避免又退化成旧记忆库那种盲目注入。
存在同一个 memory.db（独立表 core_memory），复用 core.memory 的连接助手。
"""

from datetime import datetime, timezone
from typing import Optional

from core.memory import _get_conn

MAX_FACTS = 40          # 常驻事实条数上限（保证"小而精"）
MAX_FACT_LEN = 300      # 单条字数上限


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS core_memory (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                text        TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_facts() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, text, created_at, updated_at FROM core_memory ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def count() -> int:
    with _get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM core_memory").fetchone()["n"]


def add_fact(text: str) -> dict:
    """追加一条长期事实。返回 {ok, message, id?}。"""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "message": "内容为空，未添加。"}
    if len(text) > MAX_FACT_LEN:
        text = text[:MAX_FACT_LEN]
    if count() >= MAX_FACTS:
        return {"ok": False, "message": f"用户档案已满（{MAX_FACTS} 条上限）。请先在设置面板精简后再加。"}
    # 简单去重：完全相同的文本不重复添加
    with _get_conn() as conn:
        dup = conn.execute("SELECT id FROM core_memory WHERE text = ?", (text,)).fetchone()
        if dup:
            return {"ok": True, "message": "该事实已在档案中。", "id": dup["id"]}
        now = _now()
        cur = conn.execute(
            "INSERT INTO core_memory (text, created_at, updated_at) VALUES (?, ?, ?)",
            (text, now, now),
        )
        return {"ok": True, "message": "已记入用户档案。", "id": cur.lastrowid}


def update_fact(fact_id: int, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "message": "内容为空。"}
    text = text[:MAX_FACT_LEN]
    with _get_conn() as conn:
        conn.execute("UPDATE core_memory SET text = ?, updated_at = ? WHERE id = ?",
                     (text, _now(), fact_id))
    return {"ok": True, "message": "已更新。"}


def delete_fact(fact_id: int) -> dict:
    with _get_conn() as conn:
        conn.execute("DELETE FROM core_memory WHERE id = ?", (fact_id,))
    return {"ok": True, "message": "已删除。"}


def build_block() -> str:
    """拼成注入 system prompt 的常驻档案块；空则返回空串。"""
    facts = list_facts()
    if not facts:
        return ""
    lines = ["【用户档案（长期记忆，始终可见；如与最新对话冲突，以用户最新说法为准）】"]
    lines += [f"- {f['text']}" for f in facts]
    return "\n".join(lines)


# 初始化
init_db()
