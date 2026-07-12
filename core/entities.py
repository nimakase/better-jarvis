"""
core/entities.py — L2 结构化实体记忆（客户 / 供应商 / 料号 / 报价 / 自定义）

与 L1 用户档案(core/profile.py) 的分工：
  - profile：关于「用户本人」的少量稳定事实，每轮全量注入 system prompt（常驻）。
  - entities：关于「一个个具体对象」的事实，数量可增长，【不常驻】，
    由模型按需用工具精确查询（lookup_entity / search_entities / list_entities）。

刻意做成「通用实体 + JSON 字段」，不为每种类型硬编码表结构：
    kind(类型) + name(主名) + 任意 fields(JSON) + notes + tags。
精确匹配优先（料号、公司名要的是精确，不该用向量），辅以子串模糊查——
这也是为什么 L2 不走嵌入：实体检索要的是准，不是"语义相近"。

存在同一个 memory.db（新表 entities），复用 core.memory 的连接助手，
与 profile.py 使用同一套连接方式，保持一致。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from core.memory import _get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kind        TEXT NOT NULL,
                name        TEXT NOT NULL,
                fields      TEXT NOT NULL DEFAULT '{}',
                notes       TEXT NOT NULL DEFAULT '',
                tags        TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                UNIQUE(kind, name)
            );
            CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
        """)


def _row_to_dict(r) -> dict:
    d = dict(r)
    try:
        d["fields"] = json.loads(d.get("fields") or "{}")
    except Exception:
        d["fields"] = {}
    d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
    return d


def upsert(kind: str, name: str, fields: Optional[dict] = None,
           notes: str = "", tags: Optional[list] = None) -> dict:
    """新增或更新一个实体（按 kind+name 唯一）。fields 做浅合并，不覆盖旧键之外的。"""
    kind = (kind or "").strip()
    name = (name or "").strip()
    if not kind or not name:
        return {"ok": False, "message": "kind 和 name 都不能为空。"}
    tags_str = ",".join(t.strip() for t in tags if t and t.strip()) if tags else ""
    now = _now()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, fields, notes, tags FROM entities WHERE kind=? AND name=?",
            (kind, name)).fetchone()
        if row:
            try:
                merged = json.loads(row["fields"] or "{}")
            except Exception:
                merged = {}
            if fields:
                merged.update(fields)
            new_notes = notes or row["notes"]
            new_tags = tags_str or row["tags"]
            conn.execute(
                "UPDATE entities SET fields=?, notes=?, tags=?, updated_at=? WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), new_notes, new_tags, now, row["id"]))
            return {"ok": True, "message": f"已更新实体 {kind}/{name}。", "id": row["id"]}
        conn.execute(
            "INSERT INTO entities (kind, name, fields, notes, tags, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (kind, name, json.dumps(fields or {}, ensure_ascii=False),
             notes, tags_str, now, now))
        rid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        return {"ok": True, "message": f"已记入实体 {kind}/{name}。", "id": rid}


def get(kind: str, name: str) -> Optional[dict]:
    with _get_conn() as conn:
        r = conn.execute("SELECT * FROM entities WHERE kind=? AND name=?",
                         (kind, name)).fetchone()
    return _row_to_dict(r) if r else None


def find(query: str, kind: Optional[str] = None, limit: int = 10) -> list[dict]:
    """按名字/备注/字段子串模糊查。打分：名字全等>名字含>其它字段含。精确优先。"""
    q = (query or "").strip()
    ql = q.lower()
    with _get_conn() as conn:
        if kind:
            rows = conn.execute("SELECT * FROM entities WHERE kind=?", (kind,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM entities").fetchall()
    scored = []
    for r in rows:
        d = _row_to_dict(r)
        if not ql:
            scored.append((0, d))
            continue
        name_l = d["name"].lower()
        hay = " ".join([
            d["name"], d["notes"],
            json.dumps(d["fields"], ensure_ascii=False),
            " ".join(d["tags"]),
        ]).lower()
        if name_l == ql:
            scored.append((3, d))
        elif ql in name_l:
            scored.append((2, d))
        elif ql in hay:
            scored.append((1, d))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    return [d for _, d in scored[:limit]]


def list_by_kind(kind: str, limit: int = 50) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM entities WHERE kind=? ORDER BY updated_at DESC LIMIT ?",
            (kind, limit)).fetchall()
    return [_row_to_dict(r) for r in rows]


def delete(kind: str, name: str) -> dict:
    with _get_conn() as conn:
        conn.execute("DELETE FROM entities WHERE kind=? AND name=?", (kind, name))
    return {"ok": True, "message": f"已删除 {kind}/{name}。"}


def kinds() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM entities GROUP BY kind ORDER BY kind"
        ).fetchall()
    return [dict(r) for r in rows]


# 初始化
init_db()
