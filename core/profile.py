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
        # ⑮ 轻量版：时效/证据/软删列（幂等迁移——老库补列，新库一步到位）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(core_memory)")}
        for col, decl in (
            ("evidence", "TEXT NOT NULL DEFAULT ''"),        # 出处（哪次对话的什么原话）
            ("last_confirmed_at", "TEXT"),                   # 最近一次被再次确认
            ("superseded_at", "TEXT"),                       # 软删时间（NULL=活跃）
            ("supersede_reason", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE core_memory ADD COLUMN {col} {decl}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_facts(include_superseded: bool = False) -> list[dict]:
    q = "SELECT * FROM core_memory"
    if not include_superseded:
        q += " WHERE superseded_at IS NULL"
    q += " ORDER BY id"
    with _get_conn() as conn:
        return [dict(r) for r in conn.execute(q).fetchall()]


def count() -> int:
    """活跃事实数（软删的不占 MAX_FACTS 名额）。"""
    with _get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM core_memory WHERE superseded_at IS NULL"
        ).fetchone()["n"]


def add_fact(text: str, evidence: str = "") -> dict:
    """追加一条长期事实（可带出处）。返回 {ok, message, id?}。"""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "message": "内容为空，未添加。"}
    if len(text) > MAX_FACT_LEN:
        text = text[:MAX_FACT_LEN]
    if count() >= MAX_FACTS:
        return {"ok": False, "message": f"用户档案已满（{MAX_FACTS} 条上限）。请先在设置面板精简后再加。"}
    # 简单去重：完全相同的文本不重复添加（视为再次确认）
    with _get_conn() as conn:
        dup = conn.execute(
            "SELECT id FROM core_memory WHERE text = ? AND superseded_at IS NULL",
            (text,)).fetchone()
        if dup:
            conn.execute("UPDATE core_memory SET last_confirmed_at = ? WHERE id = ?",
                         (_now(), dup["id"]))
            return {"ok": True, "message": "该事实已在档案中（已刷新确认时间）。", "id": dup["id"]}
        now = _now()
        cur = conn.execute(
            "INSERT INTO core_memory (text, created_at, updated_at, evidence,"
            " last_confirmed_at) VALUES (?, ?, ?, ?, ?)",
            (text, now, now, (evidence or "")[:MAX_FACT_LEN], now),
        )
        return {"ok": True, "message": "已记入用户档案。", "id": cur.lastrowid}


def confirm_fact(fact_id: int) -> dict:
    """再次确认一条事实（刷新 last_confirmed_at——时效的依据）。"""
    with _get_conn() as conn:
        cur = conn.execute("UPDATE core_memory SET last_confirmed_at = ? WHERE id = ?",
                           (_now(), fact_id))
    return {"ok": cur.rowcount > 0, "message": "已确认" if cur.rowcount else "无此事实"}


def supersede_fact(fact_id: int, reason: str = "") -> dict:
    """软删一条事实（可回滚——巩固作业只允许软删，绝不硬删）。"""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE core_memory SET superseded_at = ?, supersede_reason = ?"
            " WHERE id = ? AND superseded_at IS NULL",
            (_now(), (reason or "")[:MAX_FACT_LEN], fact_id))
    return {"ok": cur.rowcount > 0,
            "message": "已标记过时（可恢复）" if cur.rowcount else "无此活跃事实"}


def restore_fact(fact_id: int) -> dict:
    """恢复一条被软删的事实。"""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE core_memory SET superseded_at = NULL, supersede_reason = ''"
            " WHERE id = ?", (fact_id,))
    return {"ok": cur.rowcount > 0, "message": "已恢复" if cur.rowcount else "无此事实"}


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
