"""
core/episodic.py — L4 情节记忆（对话摘要 / 过往经过，语义可检索）

与其它层的分工：
  - profile(L1)/entities(L2)：稳定事实（关于「人」/关于「对象」）。
  - episodic(L4)：一段段「发生过什么、聊过什么」的自由文本，随时间累积，
    【不常驻】，由模型带着问题用 recall(query) 语义召回最相关的几条。

写入来源有二：
  1) 长对话压缩时，controller._compress_history 把生成的「早期对话摘要」
     自动落这里（source=compress），从此不再"聊完即蒸发"。
  2) 模型判断某段经过值得长期记住时，主动调 remember_episode（source=model）。

嵌入用 core.embedding（本地，文本不出机器）。向量以 blob 存在行内，
单用户几千条量级直接 numpy 余弦即可，无需引入向量数据库。
每行记录嵌入维度 dim，检索时只比对同维度的行（换嵌入后端后自动兼容）。

存在同一个 memory.db（新表 episodes），复用 core.memory 的连接助手。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import numpy as np

from core.memory import _get_conn
from core import embedding


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS episodes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                text        TEXT NOT NULL,
                tags        TEXT NOT NULL DEFAULT '',
                source      TEXT NOT NULL DEFAULT '',
                dim         INTEGER NOT NULL DEFAULT 0,
                embedding   BLOB,
                created_at  TEXT NOT NULL
            );
        """)


def save(text: str, *, tags: Optional[list] = None, source: str = "") -> dict:
    """把一段经过/摘要编码入库。best-effort：调用方（如压缩钩子）应吞掉异常。"""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "message": "内容为空，未保存。"}
    vec = embedding.embed_one(text, kind="passage").astype(np.float32)
    tags_str = ",".join(t.strip() for t in tags if t and t.strip()) if tags else ""
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO episodes (text, tags, source, dim, embedding, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (text, tags_str, source, int(vec.shape[0]), vec.tobytes(), _now()))
        rid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    return {"ok": True, "message": "已记入情节记忆。", "id": rid}


def recall(query: str, k: int = 5, min_score: float = 0.0) -> list[dict]:
    """按语义召回最相关的若干条情节。返回 [{id,text,tags,source,score,created_at}]。"""
    q = (query or "").strip()
    if not q:
        return []
    qv = embedding.embed_one(q, kind="query").astype(np.float32)
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, text, tags, source, dim, embedding, created_at FROM episodes"
        ).fetchall()
    mats, metas = [], []
    for r in rows:
        if not r["embedding"] or r["dim"] != qv.shape[0]:
            continue  # 只比对同维度（同一嵌入后端）的行
        mats.append(np.frombuffer(r["embedding"], dtype=np.float32))
        metas.append(r)
    if not mats:
        return []
    matrix = np.vstack(mats)
    out = []
    for idx, score in embedding.cosine_topk(qv, matrix, k):
        if score < min_score:
            continue
        r = metas[idx]
        out.append({
            "id": r["id"],
            "text": r["text"],
            "tags": [t for t in (r["tags"] or "").split(",") if t],
            "source": r["source"],
            "score": round(score, 4),
            "created_at": r["created_at"],
        })
    return out


def count() -> int:
    with _get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"]


def recent(limit: int = 20) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, text, tags, source, created_at FROM episodes "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def delete(episode_id: int) -> dict:
    with _get_conn() as conn:
        conn.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
    return {"ok": True, "message": "已删除。"}


# 初始化
init_db()
