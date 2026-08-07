"""
core/building_block_notes.py — building block 使用验证笔记（任务 #12）

背景：任务 #10 把 BUILDING_BLOCKS 从"手工精选"扩到"AST 自动抽取任意模块"
（core.skill_policy.auto_module_api_text），代价是自动抽取的签名明确标注
"未经人工核实用法"——签名对不代表用法对（调对了方法名，参数顺序/语义可能仍是
猜的）。这个模块提供一条低成本的信任累积路径：不是靠人工逐条审核，而是每次
造技能实际【用到】某个 building block 且通过了静态校验+隔离冒烟（core.
tool_builder._author_verified_loop 的门），就记一笔"这个模块在实践中被这样
用过、没在 import/加载阶段炸"。

诚实边界：这不是语义正确性证明——冒烟只验证"能正常 import/构造，handler 没
在模块顶层炸"，不执行 handler 本身，不代表"这次调用产出的结果是对的"。但比
"AST 抽取、从没跑过一次"依然是净改善，所以标注措辞用"经隔离冒烟验证可用"，
不用"已验证正确"这种过度承诺的说法。

存储：跟 core/group_memory.py 同一个模式（同一个 memory.db，独立表），
按模块名分桶，超过上限淘汰最老的（不是无限堆积的审计日志，是"最近这么用过，
可信"这种精简信号）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.memory import _get_conn

MAX_NOTES_PER_MODULE = 8   # 跟 group_memory 的"小而精"同一个纪律，避免又膨胀成杂物堆
MAX_NOTE_LEN = 300


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS building_block_notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                module     TEXT NOT NULL,
                note       TEXT NOT NULL,
                evidence   TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bb_notes_module ON building_block_notes(module);
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_note(module: str, note: str, evidence: str = "") -> dict:
    """记一笔验证笔记；超过 MAX_NOTES_PER_MODULE 自动淘汰该模块最老的一条。
    绝不抛异常（写入失败降级为 no-op）——这是造技能成功路径上的旁路增强，
    不该因为记笔记失败而拖累主流程。"""
    module = (module or "").strip()
    note = (note or "").strip()[:MAX_NOTE_LEN]
    if not module or not note:
        return {"ok": False, "message": "module/note 不能为空"}
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO building_block_notes (module, note, evidence, created_at)"
                " VALUES (?, ?, ?, ?)",
                (module, note, (evidence or "")[:200], _now()))
            rows = conn.execute(
                "SELECT id FROM building_block_notes WHERE module = ? ORDER BY id DESC",
                (module,)).fetchall()
            stale = [r["id"] for r in rows[MAX_NOTES_PER_MODULE:]]
            if stale:
                conn.executemany("DELETE FROM building_block_notes WHERE id = ?",
                                 [(i,) for i in stale])
        return {"ok": True, "message": "已记录"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"记录失败：{e}"}


def notes_for(module: str) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM building_block_notes WHERE module = ? ORDER BY id DESC",
            (module,)).fetchall()
    return [dict(r) for r in rows]


def verified_modules() -> set[str]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT module FROM building_block_notes").fetchall()
    return {r["module"] for r in rows}


def build_block(module: str) -> str:
    """渲成可拼进 building_blocks_api_text() 的一小段；无笔记则返回空串。"""
    notes = notes_for(module)
    if not notes:
        return ""
    lines = [f"\n\n## {module} 的【实践验证记录】（真实造技能时用过、经隔离冒烟验证可用，"
             "不代表业务逻辑一定对，但比从没跑过的签名可信）："]
    for n in notes:
        lines.append(f"- {n['note']}")
    return "\n".join(lines)


init_db()
