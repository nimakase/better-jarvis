"""
core/signals.py — 监督信号打标（进化的燃料管 · 之二）

巩固/学习需要知道「什么算对」。这些信号此前全被丢掉：用户的纠正、放弃、重试、
对主动建议的冷淡。本模块用廉价的词面检测把它们打标落库，给未来的记忆巩固作业
（consolidation）攒 ground truth。

设计要点：
- 【宁缺毋滥】：检测故意保守——只标注高置信的显式信号（开头的「不是/不对」等），
  含糊的不标。假阳性会污染学习数据，漏标可以靠日后语义分析补。
- 【只攒不判】：本模块只负责打标存储，不做任何实时行为调整；
  解读与消费是巩固作业的事（那时有完整上下文 + 离线算力）。
- 写入廉价、绝不阻断对话（与 telemetry 同一哲学）。

信号类型：
  correction   用户纠正上一轮（「不是」「不对」「我说的是…」）
  frustration  用户放弃/不耐（「算了」「别弄了」「怎么还…」）
  retry        用户要求重来（「重新」「再试一次」「还是不行」）
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from core.memory import _get_conn

# 只匹配【消息开头】的显式信号词——出现在开头才是对上一轮的直接反应，
# 句中出现（如转述、举例）不算。保守优先。
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("correction", re.compile(
        r"^(不是|不对|错了|不,|不，|我说的是|我指的是|你理解错|搞错了)")),
    ("frustration", re.compile(
        r"^(算了|别弄了|不用了|停|够了)|^(怎么还|又错|又不对)")),
    ("retry", re.compile(
        r"^(重新|再试|再来|重来|还是不行|还是不对|再改)")),
]

_EXCERPT = 200   # 存储的文本截断长度


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS supervision_signals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                kind         TEXT NOT NULL,
                user_text    TEXT NOT NULL,
                prev_excerpt TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signals_kind ON supervision_signals(kind);
        """)


def detect(user_message: str) -> str:
    """返回信号类型；无信号返回空串。纯函数，可确定性单测。"""
    text = (user_message or "").strip()
    if not text:
        return ""
    for kind, pat in _PATTERNS:
        if pat.search(text):
            return kind
    return ""


def tag(user_message: str, prev_assistant_text: str = "") -> str:
    """检测并落库。返回信号类型（空串=无信号）。绝不抛异常。"""
    try:
        kind = detect(user_message)
        if not kind:
            return ""
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO supervision_signals (kind, user_text, prev_excerpt, created_at)"
                " VALUES (?, ?, ?, ?)",
                (kind, user_message[:_EXCERPT],
                 (prev_assistant_text or "")[:_EXCERPT], _now()))
        return kind
    except Exception:
        return ""


def recent(limit: int = 50, kind: str = "") -> list[dict]:
    """最近的信号（巩固作业消费）。"""
    q = "SELECT * FROM supervision_signals"
    args: list = []
    if kind:
        q += " WHERE kind = ?"
        args.append(kind)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _get_conn() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


init_db()
