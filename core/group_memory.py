"""
core/group_memory.py — 组作用域记忆（工具组专属业务笔记）

解决 core/profile.py（L1 全局档案）解决不了的问题：有些事实只在使用某个工具组
时才有意义（"跟 HubSpot 打交道时报价规则是……""飞书能力边界是……"），硬塞进
全局档案会污染每一轮 system prompt、还挤占 L1 本该很小的额度；不塞则每次用到
都要重新交代。

设计跟 core/profile.py 同构（结构、字数/条数纪律、软删语义均照抄，保持一致）：
  - 按 group 分桶（对应 core/registry 里 @tool(group=...) 的分组，或渐进披露的
    load_tools(group=...) 分组名），不是全局唯一一份。
  - 刻意保持小：每组独立的条数/字数上限，防止再退化成"盲目注入"的旧记忆库。
  - 只在对应工具组被加载时才需要注入（真正的注入挂钩在 core/controller.py，
    因其属 PROTECTED，需人工审核后接入——本模块只负责存储与读写，不做注入）。

存同一个 memory.db（独立表 group_memory），复用 core.memory 的连接助手。
"""

from datetime import datetime, timezone

from core.memory import _get_conn

MAX_NOTES_PER_GROUP = 20    # 每组常驻笔记条数上限（保证"小而精"，同 profile.py 纪律）
MAX_NOTE_LEN = 300          # 单条字数上限


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS group_memory (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name  TEXT NOT NULL,
                text        TEXT NOT NULL,
                evidence    TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                superseded_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_group_memory_group ON group_memory(group_name);
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_group(group: str) -> str:
    return (group or "").strip().lower()


def list_notes(group: str, include_superseded: bool = False) -> list[dict]:
    group = _norm_group(group)
    q = "SELECT * FROM group_memory WHERE group_name = ?"
    if not include_superseded:
        q += " AND superseded_at IS NULL"
    q += " ORDER BY id"
    with _get_conn() as conn:
        return [dict(r) for r in conn.execute(q, (group,)).fetchall()]


def count(group: str) -> int:
    group = _norm_group(group)
    with _get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM group_memory"
            " WHERE group_name = ? AND superseded_at IS NULL", (group,)
        ).fetchone()["n"]


def add_note(group: str, text: str, evidence: str = "") -> dict:
    """给某个工具组追加一条专属笔记。返回 {ok, message, id?}。"""
    group = _norm_group(group)
    text = (text or "").strip()
    if not group:
        return {"ok": False, "message": "未指定工具组，未添加。"}
    if not text:
        return {"ok": False, "message": "内容为空，未添加。"}
    if len(text) > MAX_NOTE_LEN:
        text = text[:MAX_NOTE_LEN]
    if count(group) >= MAX_NOTES_PER_GROUP:
        return {"ok": False,
                "message": f"『{group}』组的笔记已满（{MAX_NOTES_PER_GROUP} 条上限），"
                           f"请先精简后再加，避免退化成盲目注入。"}
    with _get_conn() as conn:
        dup = conn.execute(
            "SELECT id FROM group_memory WHERE group_name = ? AND text = ?"
            " AND superseded_at IS NULL", (group, text)).fetchone()
        if dup:
            conn.execute("UPDATE group_memory SET updated_at = ? WHERE id = ?",
                         (_now(), dup["id"]))
            return {"ok": True, "message": "该笔记已存在（已刷新时间）。", "id": dup["id"]}
        now = _now()
        cur = conn.execute(
            "INSERT INTO group_memory (group_name, text, evidence, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (group, text, (evidence or "")[:MAX_NOTE_LEN], now, now),
        )
        return {"ok": True, "message": f"已记入『{group}』组笔记。", "id": cur.lastrowid}


def supersede_note(note_id: int, reason: str = "") -> dict:
    """软删一条笔记（可回滚，不硬删——与 profile.py 的纪律一致）。"""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE group_memory SET superseded_at = ? WHERE id = ? AND superseded_at IS NULL",
            (_now(), note_id))
    return {"ok": cur.rowcount > 0, "message": "已标记过时（可恢复）" if cur.rowcount else "无此活跃笔记"}


def restore_note(note_id: int) -> dict:
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE group_memory SET superseded_at = NULL WHERE id = ?", (note_id,))
    return {"ok": cur.rowcount > 0, "message": "已恢复" if cur.rowcount else "无此笔记"}


def delete_note(note_id: int) -> dict:
    with _get_conn() as conn:
        conn.execute("DELETE FROM group_memory WHERE id = ?", (note_id,))
    return {"ok": True, "message": "已删除。"}


def build_block(group: str) -> str:
    """拼成某工具组被加载时可附加注入的笔记块；该组无笔记则返回空串。

    注入挂钩本身不在这里——本模块只管存储/读写。真正"哪个组加载时把这段话
    塞进 system prompt"的接线，在 core/controller.py（PROTECTED，需人工审核）。
    """
    notes = list_notes(group)
    if not notes:
        return ""
    lines = [f"【{group} 组专属笔记（长期记忆，仅在此工具组加载时可见）】"]
    lines += [f"- {n['text']}" for n in notes]
    return "\n".join(lines)


def groups_with_notes() -> list[str]:
    """有笔记的组名列表（供设置面板/自省用）。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT group_name FROM group_memory"
            " WHERE superseded_at IS NULL ORDER BY group_name").fetchall()
    return [r["group_name"] for r in rows]


# 初始化
init_db()
