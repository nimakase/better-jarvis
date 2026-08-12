"""
core/procedures.py — 过程性记忆(procedural memory)：解决问题的方法

CoALA 认知架构把记忆分四类：工作 / 情节 / 语义 / 过程。贾维斯此前 L1(profile 常驻
事实)、L2(情节快照)等都是语义记忆(某件事「是什么」)，唯独缺过程记忆——遇到某类
问题「该怎么解」的可复用经验。本模块补上这一类，专供 core/consolidation.py 的
过程记忆巩固作业写入、connectors/consolidation_tools.recall_procedure 按需查询。

架构上刻意与 profile.py（core memory，始终注入 system prompt）不同：过程条目会
随时间积累、且多数与当前任务无关，常驻只会挤占预算、增加噪音。这里遵循渐进式
披露（progressive disclosure）：默认只暴露一张"索引"（build_index，仅 problem
摘要，供巩固作业/子 agent 扫一眼判断"是否已有同类经验"，避免重复造经验或误判
为新知识）；完整 method 靠 recall(query) 按需取——索引常驻、正文按需展开。

存储：复用 core.memory 的 memory.db（同一份 SQLite 文件），独立表
procedural_memory，与 core_memory（L1 事实表）互不干扰。
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.memory import _get_conn
from core.capability import _score, _tokens

MAX_PROCEDURES = 200      # 总量上限（比 profile 的 40 宽松——过程记忆不常驻，不必"小而精"）
MAX_FIELD_LEN = 800       # method 允许比 fact 长（步骤性描述）


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS procedural_memory (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                problem            TEXT NOT NULL,
                method             TEXT NOT NULL,
                evidence           TEXT NOT NULL DEFAULT '',
                tags               TEXT NOT NULL DEFAULT '',
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL,
                last_confirmed_at  TEXT,
                times_confirmed    INTEGER NOT NULL DEFAULT 0,
                superseded_at      TEXT,
                supersede_reason   TEXT NOT NULL DEFAULT ''
            );
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_procedures(include_superseded: bool = False) -> list[dict]:
    q = "SELECT * FROM procedural_memory"
    if not include_superseded:
        q += " WHERE superseded_at IS NULL"
    q += " ORDER BY id"
    with _get_conn() as conn:
        return [dict(r) for r in conn.execute(q).fetchall()]


def count() -> int:
    """活跃经验数（软删的不占 MAX_PROCEDURES 名额）。"""
    with _get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM procedural_memory WHERE superseded_at IS NULL"
        ).fetchone()["n"]


def add_procedure(problem: str, method: str, evidence: str = "", tags: str = "") -> dict:
    """追加一条过程记忆（problem=触发场景/症状，method=具体解法）。返回 {ok, message, id?}。"""
    problem = (problem or "").strip()
    method = (method or "").strip()
    if not problem or not method:
        return {"ok": False, "message": "problem/method 不能为空，未添加。"}
    problem = problem[:MAX_FIELD_LEN]
    method = method[:MAX_FIELD_LEN]
    if count() >= MAX_PROCEDURES:
        return {"ok": False, "message": f"过程记忆已满（{MAX_PROCEDURES} 条上限），请先清理再加。"}
    with _get_conn() as conn:
        dup = conn.execute(
            "SELECT id FROM procedural_memory WHERE problem = ? AND superseded_at IS NULL",
            (problem,)).fetchone()
        if dup:
            conn.execute(
                "UPDATE procedural_memory SET last_confirmed_at = ?,"
                " times_confirmed = times_confirmed + 1 WHERE id = ?",
                (_now(), dup["id"]))
            return {"ok": True, "message": "该经验已存在（已刷新确认次数）。", "id": dup["id"]}
        now = _now()
        cur = conn.execute(
            "INSERT INTO procedural_memory (problem, method, evidence, tags, created_at,"
            " updated_at, last_confirmed_at, times_confirmed) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (problem, method, (evidence or "")[:MAX_FIELD_LEN], (tags or "")[:200], now, now, now),
        )
        return {"ok": True, "message": "已记入过程记忆。", "id": cur.lastrowid}


def confirm_procedure(procedure_id: int) -> dict:
    """再次确认一条经验（刷新 last_confirmed_at + 累加 times_confirmed——复现次数是
    该经验价值的信号）。"""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE procedural_memory SET last_confirmed_at = ?,"
            " times_confirmed = times_confirmed + 1 WHERE id = ?",
            (_now(), procedure_id))
    return {"ok": cur.rowcount > 0, "message": "已确认" if cur.rowcount else "无此经验"}


def supersede_procedure(procedure_id: int, reason: str = "") -> dict:
    """软删一条经验（可回滚——巩固作业只允许软删，绝不硬删）。"""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE procedural_memory SET superseded_at = ?, supersede_reason = ?"
            " WHERE id = ? AND superseded_at IS NULL",
            (_now(), (reason or "")[:MAX_FIELD_LEN], procedure_id))
    return {"ok": cur.rowcount > 0,
            "message": "已标记过时（可恢复）" if cur.rowcount else "无此活跃经验"}


def restore_procedure(procedure_id: int) -> dict:
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE procedural_memory SET superseded_at = NULL, supersede_reason = ''"
            " WHERE id = ?", (procedure_id,))
    return {"ok": cur.rowcount > 0, "message": "已恢复" if cur.rowcount else "无此经验"}


def update_procedure(procedure_id: int, problem: str = None, method: str = None) -> dict:
    sets, args = [], []
    if problem is not None:
        sets.append("problem = ?")
        args.append(problem.strip()[:MAX_FIELD_LEN])
    if method is not None:
        sets.append("method = ?")
        args.append(method.strip()[:MAX_FIELD_LEN])
    if not sets:
        return {"ok": False, "message": "无更新内容。"}
    sets.append("updated_at = ?")
    args.append(_now())
    args.append(procedure_id)
    with _get_conn() as conn:
        conn.execute(f"UPDATE procedural_memory SET {', '.join(sets)} WHERE id = ?", args)
    return {"ok": True, "message": "已更新。"}


def build_index() -> str:
    """轻量索引：仅 problem 摘要（不含 method），供巩固作业/子 agent 扫一眼去重、
    判断"这类问题是否已有经验"——真正的解法用 recall() 按需展开（渐进式披露）。"""
    procs = list_procedures()
    if not procs:
        return ""
    lines = ["【过程记忆索引（仅问题摘要；需要具体方法时用 recall_procedure 查询）】"]
    lines += [f"- #{p['id']} {p['problem'][:60]}" for p in procs]
    return "\n".join(lines)


def recall(query: str, limit: int = 5, min_score: float = 0.15) -> list[dict]:
    """按 query 词面相关性检索过程记忆（轻量词面打分，复用 core.capability 的打分
    工具；量级小，词面已够用，真需要语义检索时再升级不迟）。空 query 返回最近若干条。"""
    query = (query or "").strip()
    procs = list_procedures()
    if not query:
        return procs[:limit]
    qt = _tokens(query)
    scored = []
    for p in procs:
        pt = _tokens(p["problem"] + " " + (p.get("tags") or ""))
        s = max(_score(qt, pt), _score(pt, qt))
        if s >= min_score:
            scored.append((p, s))
    scored.sort(key=lambda x: -x[1])
    return [p for p, _ in scored[:limit]]


# 初始化
init_db()
