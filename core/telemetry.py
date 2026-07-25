"""
core/telemetry.py — 执行遥测（进化的燃料管 · 之一）

每次工具分发记一行：{tool, ok, 耗时, 错误摘要, 会话类型, 时间}。
另有能力缺口表（capability_gaps）：贾维斯说「我做不到/没这工具」时落一条，
定期聚合后可主动提议造工具。

用途（下游消费方）：
  - self_review 反思：从「读源码猜缺陷」升级为「这周 X 失败 7 次，报错都是 Y，去修它」；
  - 能力索引：哪些工具常用/从没被用过（试用工具转正/清理的依据）；
  - 资源自觉（未来）：调用频次与耗时画像。

设计：写入必须廉价且绝不影响主流程——同步 sqlite 单行 INSERT（WAL 下毫秒级），
一切异常吞掉。复用 memory.db（新表），与 artifacts 同一模式。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from core.memory import _get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tool_calls (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tool       TEXT NOT NULL,
                ok         INTEGER NOT NULL,
                ms         INTEGER NOT NULL DEFAULT 0,
                error      TEXT NOT NULL DEFAULT '',
                session    TEXT NOT NULL DEFAULT 'interactive',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool);
            CREATE INDEX IF NOT EXISTS idx_tool_calls_time ON tool_calls(created_at);
            CREATE TABLE IF NOT EXISTS capability_gaps (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                need       TEXT NOT NULL,
                context    TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            -- ㉔ 动作回执：投递到底成没成，可查、可感知（此前发射即忘）
            CREATE TABLE IF NOT EXISTS delivery_receipts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                track      TEXT NOT NULL,
                title      TEXT NOT NULL DEFAULT '',
                delivered  INTEGER NOT NULL,
                channels   TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_receipts_time ON delivery_receipts(created_at);
        """)


def record(tool: str, ok: bool, ms: int, error: str = "",
           session: str = "interactive") -> None:
    """记一次工具调用。绝不抛异常、绝不阻塞主流程。"""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO tool_calls (tool, ok, ms, error, session, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (tool, 1 if ok else 0, int(ms), (error or "")[:300], session, _now()))
    except Exception:
        pass


def record_delivery(track: str, title: str, delivered: bool, channels: dict) -> None:
    """记一条投递回执。channels: {渠道: {ok, error?}}。绝不抛异常。"""
    import json as _json
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO delivery_receipts (track, title, delivered, channels, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (track, (title or "")[:200], 1 if delivered else 0,
                 _json.dumps(channels, ensure_ascii=False)[:1000], _now()))
    except Exception:
        pass


def delivery_receipts(limit: int = 20, failed_only: bool = False) -> list[dict]:
    """最近的投递回执（供 delivery_status 工具 / 健康感官消费）。"""
    import json as _json
    q = "SELECT * FROM delivery_receipts"
    if failed_only:
        q += " WHERE delivered = 0"
    q += " ORDER BY id DESC LIMIT ?"
    with _get_conn() as conn:
        rows = [dict(r) for r in conn.execute(q, (limit,)).fetchall()]
    for r in rows:
        try:
            r["channels"] = _json.loads(r["channels"])
        except Exception:
            r["channels"] = {}
    return rows


def recent_delivery_failures(days: int = 3) -> int:
    """近 N 天投递失败次数（健康感官用）。"""
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        with _get_conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM delivery_receipts"
                " WHERE delivered = 0 AND created_at >= ?", (since,)).fetchone()["n"]
    except Exception:
        return 0


def log_gap(need: str, context: str = "") -> None:
    """记一条能力缺口（「我做不到/没这工具」）。"""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO capability_gaps (need, context, created_at) VALUES (?, ?, ?)",
                (need[:500], (context or "")[:500], _now()))
    except Exception:
        pass


# ── 聚合查询（喂 self_review / 能力索引）─────────────────────────────────────

def stats(days: int = 7) -> list[dict]:
    """近 N 天按工具汇总：调用数、失败数、失败率、平均耗时、最近错误样本。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT tool, COUNT(*) AS n,"
            " SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failures,"
            " CAST(AVG(ms) AS INTEGER) AS avg_ms"
            " FROM tool_calls WHERE created_at >= ?"
            " GROUP BY tool ORDER BY failures DESC, n DESC", (since,)).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            if rec["failures"]:
                err = conn.execute(
                    "SELECT error FROM tool_calls WHERE tool=? AND ok=0 AND error != ''"
                    " ORDER BY id DESC LIMIT 1", (rec["tool"],)).fetchone()
                rec["last_error"] = err["error"] if err else ""
            out.append(rec)
        return out


def trouble_report(days: int = 7, min_failures: int = 2) -> str:
    """人/模型可读的「哪里在坏」摘要（self_review 反思 prompt 的注入块）。空则返回空串。"""
    troubled = [s for s in stats(days) if (s.get("failures") or 0) >= min_failures]
    if not troubled:
        return ""
    lines = [f"【近 {days} 天工具故障画像（来自真实运行遥测，优先据此定位缺陷）】"]
    for s in troubled:
        rate = s["failures"] / s["n"] * 100
        lines.append(f"- {s['tool']}：{s['n']} 次调用，失败 {s['failures']}（{rate:.0f}%），"
                     f"平均 {s['avg_ms']}ms；最近错误：{s.get('last_error') or '无记录'}")
    return "\n".join(lines)


def unused_tools(days: int = 30) -> list[str]:
    """近 N 天从没被调用过的已注册工具（试用清理/渐进披露的依据）。"""
    from core import registry
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _get_conn() as conn:
        used = {r["tool"] for r in conn.execute(
            "SELECT DISTINCT tool FROM tool_calls WHERE created_at >= ?", (since,))}
    return [s.name for s in registry.iter_specs() if s.name not in used]


init_db()
