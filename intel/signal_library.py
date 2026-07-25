"""
信号库存储层 — Signal Library storage layer.

共享情报层的本地 SQLite 存储。一处采集、多处消费：
  - 日报视图   query_report()
  - 潜客覆盖轨 query_for_node()
  - 机会轨     company_pointed_signals() / 审核队列 enqueue_review / decide_review

设计契约见 signal_library_spec.md。纯标准库，无第三方依赖。
DB 默认落在 Jarvis 数据目录（config.DATA_DIR）；脱离 jarvis 单测时回退到本模块同目录。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

try:  # 在 jarvis 内运行：DB 放统一数据目录
    import config
    _DATA_DIR = config.DATA_DIR
except Exception:  # 脱离 jarvis 单测：回退本目录
    _DATA_DIR = Path(__file__).resolve().parent

DEFAULT_DB = _DATA_DIR / "signal_library.db"

# 各 signal_type 的默认半衰期（天）。采集端没给 half_life_days 时用这个。
DEFAULT_HALF_LIFE = {
    "pricing": 21,
    "lead_time": 21,
    "shortage": 45,
    "oversupply": 45,
    "demand_shift": 45,
    "capacity": 90,
    "layoff": 90,
    "eol_pcn": 120,
    "closure": 120,
    "m_and_a": 120,
    "write_down": 120,
    "policy": 180,
}

# surplus_implication 默认值（采集端没给时的兜底推断）。强类型给高分。
_SURPLUS_DEFAULT = {
    "closure": 4, "eol_pcn": 4, "write_down": 4,
    "oversupply": 3, "m_and_a": 2, "demand_shift": 2, "layoff": 2,
}

VALID_SCOPES = {"macro", "sector", "component", "company"}
VALID_TYPES = set(DEFAULT_HALF_LIFE.keys())


# ────────────────────────── 连接 / 建库 ──────────────────────────

def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: str | Path = DEFAULT_DB) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint     TEXT UNIQUE,
                date_collected  TEXT NOT NULL,
                date_event      TEXT,
                source_url      TEXT,
                source_type     TEXT,
                scope           TEXT NOT NULL,
                signal_type     TEXT NOT NULL,
                direction       TEXT,
                severity        INTEGER,
                summary         TEXT NOT NULL,
                surplus_implication INTEGER DEFAULT 0,
                confidence      INTEGER,
                half_life_days  INTEGER,
                status          TEXT DEFAULT 'active',
                seen_count      INTEGER DEFAULT 1,
                raw_json        TEXT
            );
            CREATE TABLE IF NOT EXISTS signal_sectors    (signal_id INTEGER, sector_id TEXT);
            CREATE TABLE IF NOT EXISTS signal_components (signal_id INTEGER, component TEXT);
            CREATE TABLE IF NOT EXISTS signal_regions    (signal_id INTEGER, region TEXT);
            CREATE TABLE IF NOT EXISTS signal_companies (
                signal_id INTEGER, company_name TEXT, website TEXT, country TEXT, note TEXT
            );
            CREATE TABLE IF NOT EXISTS review_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sector_id   TEXT,
                reason      TEXT,
                signal_ids  TEXT,
                status      TEXT DEFAULT 'pending',
                created_at  TEXT,
                decided_at  TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_sig_status ON signals(status);
            CREATE INDEX IF NOT EXISTS ix_sig_type   ON signals(signal_type);
            CREATE INDEX IF NOT EXISTS ix_sec_sector ON signal_sectors(sector_id);
            CREATE INDEX IF NOT EXISTS ix_sec_sig    ON signal_sectors(signal_id);
            CREATE INDEX IF NOT EXISTS ix_comp_comp  ON signal_components(component);
            CREATE INDEX IF NOT EXISTS ix_companies_sig ON signal_companies(signal_id);
            """
        )
        conn.commit()
    finally:
        conn.close()


# ────────────────────────── 工具 ──────────────────────────

def _today() -> str:
    return date.today().isoformat()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _fingerprint(source_url: str, summary: str) -> str:
    basis = _norm(source_url) + "|" + _norm(summary)[:90]
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _age_days(date_collected: str, as_of: Optional[str] = None) -> float:
    d0 = datetime.fromisoformat(date_collected).date()
    d1 = date.fromisoformat(as_of) if as_of else date.today()
    return max(0.0, (d1 - d0).days)


def decayed_strength(severity: int, age_days: float, half_life_days: int) -> float:
    """有效强度 = severity × 0.5^(age/half_life)。"""
    if not severity:
        return 0.0
    hl = half_life_days or 45
    return severity * math.pow(0.5, age_days / hl)


# ────────────────────────── 采集入库（去重/合并） ──────────────────────────

def ingest_signals(signals: Iterable[dict], db_path: str | Path = DEFAULT_DB,
                   collected_date: Optional[str] = None) -> dict:
    """灌入一批信号。按 fingerprint 去重：已存在则合并刷新，否则插入。

    返回 {"inserted": n, "merged": m, "skipped": k}。
    """
    conn = connect(db_path)
    collected = collected_date or _today()
    inserted = merged = skipped = 0
    try:
        for s in signals:
            summary = (s.get("summary") or "").strip()
            stype = s.get("signal_type")
            scope = s.get("scope")
            if not summary or stype not in VALID_TYPES or scope not in VALID_SCOPES:
                skipped += 1
                continue

            fp = _fingerprint(s.get("source_url", ""), summary)
            row = conn.execute("SELECT id, confidence, severity, seen_count FROM signals WHERE fingerprint = ?", (fp,)).fetchone()

            if row:  # 合并：刷新采集日、取较高 severity/confidence、计数 +1
                conn.execute(
                    """UPDATE signals SET date_collected = ?, status = 'active',
                       severity = MAX(COALESCE(severity,0), ?),
                       confidence = MAX(COALESCE(confidence,0), ?),
                       seen_count = seen_count + 1 WHERE id = ?""",
                    (collected, s.get("severity") or 0, s.get("confidence") or 0, row["id"]),
                )
                merged += 1
                continue

            impl = s.get("surplus_implication")
            if impl is None:
                impl = _SURPLUS_DEFAULT.get(stype, 0)
            half_life = s.get("half_life_days") or DEFAULT_HALF_LIFE.get(stype, 45)

            cur = conn.execute(
                """INSERT INTO signals
                   (fingerprint, date_collected, date_event, source_url, source_type,
                    scope, signal_type, direction, severity, summary,
                    surplus_implication, confidence, half_life_days, status, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'active', ?)""",
                (fp, collected, s.get("date_event"), s.get("source_url"), s.get("source_type"),
                 scope, stype, s.get("direction"), s.get("severity"), summary,
                 impl, s.get("confidence"), half_life, json.dumps(s, ensure_ascii=False)),
            )
            sid = cur.lastrowid
            for sec in s.get("sectors", []) or []:
                conn.execute("INSERT INTO signal_sectors(signal_id, sector_id) VALUES (?,?)", (sid, sec))
            for comp in s.get("components", []) or []:
                conn.execute("INSERT INTO signal_components(signal_id, component) VALUES (?,?)", (sid, comp))
            for reg in s.get("regions", []) or []:
                conn.execute("INSERT INTO signal_regions(signal_id, region) VALUES (?,?)", (sid, reg))
            for c in s.get("companies", []) or []:
                conn.execute(
                    "INSERT INTO signal_companies(signal_id, company_name, website, country, note) VALUES (?,?,?,?,?)",
                    (sid, c.get("company_name"), c.get("website"), c.get("country"), c.get("note")),
                )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return {"inserted": inserted, "merged": merged, "skipped": skipped}


# ────────────────────────── 衰减 / 过期 ──────────────────────────

def expire_stale(db_path: str | Path = DEFAULT_DB, as_of: Optional[str] = None,
                 age_factor: float = 3.0) -> int:
    """把超过 age_factor × 半衰期 的 active 信号标记为 expired。返回过期条数。"""
    conn = connect(db_path)
    n = 0
    try:
        rows = conn.execute("SELECT id, date_collected, half_life_days FROM signals WHERE status = 'active'").fetchall()
        for r in rows:
            if _age_days(r["date_collected"], as_of) > age_factor * (r["half_life_days"] or 45):
                conn.execute("UPDATE signals SET status = 'expired' WHERE id = ?", (r["id"],))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n


# ────────────────────────── 消费：日报视图 ──────────────────────────

def latest_collected_date(db_path: str | Path = DEFAULT_DB) -> Optional[str]:
    """信号库最近一次采集到的信号日期（date_collected 最大值）。空库返回 None。

    供潜客工作流做"信号新鲜度"自检：判断离上次采集过了多少天。
    """
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT MAX(date_collected) FROM signals").fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] else None


def query_report(db_path: str | Path = DEFAULT_DB, days: int = 14,
                 as_of: Optional[str] = None) -> dict:
    """返回最近 days 天的 active 信号，按 signal_type 分组（日报渲染用）。"""
    conn = connect(db_path)
    out: dict[str, list] = {}
    try:
        rows = conn.execute("SELECT * FROM signals WHERE status = 'active' ORDER BY date_collected DESC").fetchall()
        for r in rows:
            if _age_days(r["date_collected"], as_of) > days:
                continue
            out.setdefault(r["signal_type"], []).append(dict(r))
    finally:
        conn.close()
    return out


# ────────────────────────── 消费：潜客覆盖轨 ──────────────────────────

def query_for_node(sector_ids: list[str], components: Optional[list[str]] = None,
                   db_path: str | Path = DEFAULT_DB, min_strength: float = 1.0,
                   as_of: Optional[str] = None) -> list[dict]:
    """当天节点 → 取相关、有余料含义、衰减后仍有强度的信号，按强度降序。"""
    conn = connect(db_path)
    try:
        ids: set[int] = set()
        if sector_ids:
            q = "SELECT DISTINCT signal_id FROM signal_sectors WHERE sector_id IN (%s)" % ",".join("?" * len(sector_ids))
            ids |= {row[0] for row in conn.execute(q, sector_ids).fetchall()}
        if components:
            # 元件匹配只对「广义市场信号」(component/macro scope) 生效，
            # 避免别的赛道的公司级/赛道级信号仅因共用某元件就被错误拉进来。
            q = ("SELECT DISTINCT sc.signal_id FROM signal_components sc "
                 "JOIN signals s ON s.id = sc.signal_id "
                 "WHERE sc.component IN (%s) AND s.scope IN ('component','macro')"
                 ) % ",".join("?" * len(components))
            ids |= {row[0] for row in conn.execute(q, components).fetchall()}
        if not ids:
            return []
        q = "SELECT * FROM signals WHERE id IN (%s) AND status='active' AND surplus_implication >= 1" % ",".join("?" * len(ids))
        results = []
        for r in conn.execute(q, list(ids)).fetchall():
            strength = decayed_strength(r["severity"] or 0, _age_days(r["date_collected"], as_of), r["half_life_days"] or 45)
            if strength >= min_strength:
                d = dict(r)
                d["strength"] = round(strength, 2)
                results.append(d)
        results.sort(key=lambda x: x["strength"], reverse=True)
        return results
    finally:
        conn.close()


# ────────────────────────── 消费：机会轨 ──────────────────────────

def company_pointed_signals(db_path: str | Path = DEFAULT_DB, min_implication: int = 3,
                            as_of: Optional[str] = None,
                            days: Optional[int] = None) -> list[dict]:
    """点名了具体公司、且余料含义强的 active 信号 → 日报「点名公司（金线索）」板块。

    days：只取最近 N 天采集到的（与 query_report 同口径）。None = 不限（历史行为）。

    ⚠ 这是全库唯一【没有强度衰减】的消费口——其余消费方（query_for_node /
    detect_hot_sectors）都走 decayed_strength，老信号会自己淡出。这里是裸筛，
    所以调用方**必须自己给窗口**，否则会把库里累积的所有点名公司全捞出来。
    日报传 days=14 即可（快照语义：窗口内重复出现是正确的）。
    """
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT s.id AS signal_id, s.signal_type, s.severity, s.surplus_implication,
                      s.date_collected, s.source_url, s.summary,
                      c.company_name, c.website, c.country, c.note
               FROM signals s JOIN signal_companies c ON c.signal_id = s.id
               WHERE s.status='active' AND s.surplus_implication >= ?
               ORDER BY s.severity DESC""",
            (min_implication,),
        ).fetchall()
        out = [dict(r) for r in rows]
        if days is not None:
            out = [r for r in out if _age_days(r["date_collected"], as_of) <= days]
        return out
    finally:
        conn.close()


def enqueue_review(sector_id: str, reason: str, signal_ids: list[int],
                   db_path: str | Path = DEFAULT_DB) -> int:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO review_queue(sector_id, reason, signal_ids, status, created_at) VALUES (?,?,?,'pending',?)",
            (sector_id, reason, json.dumps(signal_ids), _today()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_review(status: str = "pending", db_path: str | Path = DEFAULT_DB) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM review_queue WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def decide_review(review_id: int, approved: bool, db_path: str | Path = DEFAULT_DB) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE review_queue SET status = ?, decided_at = ? WHERE id = ?",
            ("approved" if approved else "rejected", _today(), review_id),
        )
        conn.commit()
    finally:
        conn.close()


def detect_hot_sectors(db_path: str | Path = DEFAULT_DB, threshold: float = 6.0,
                       as_of: Optional[str] = None) -> list[dict]:
    """按 sector 聚合余料信号的衰减强度，超阈值的赛道 → 建议进人工审核队列。"""
    conn = connect(db_path)
    agg: dict[str, dict] = {}
    try:
        rows = conn.execute(
            """SELECT ss.sector_id AS sector_id, s.id AS sid, s.severity AS severity,
                      s.date_collected AS date_collected, s.half_life_days AS half_life_days
               FROM signal_sectors ss JOIN signals s ON s.id = ss.signal_id
               WHERE s.status='active' AND s.surplus_implication >= 1"""
        ).fetchall()
        for r in rows:
            st = decayed_strength(r["severity"] or 0, _age_days(r["date_collected"], as_of), r["half_life_days"] or 45)
            a = agg.setdefault(r["sector_id"], {"sector_id": r["sector_id"], "total_strength": 0.0, "signal_ids": []})
            a["total_strength"] += st
            a["signal_ids"].append(r["sid"])
    finally:
        conn.close()
    hot = [a for a in agg.values() if a["total_strength"] >= threshold]
    hot.sort(key=lambda x: x["total_strength"], reverse=True)
    return hot


# 导入即建表（项目惯例：core/memory、profile、history 同样在模块级初始化）。
# 保证日报、情报台看板、采集工作流等直接调库的路径无需先经其它入口即可用。
init_db()


if __name__ == "__main__":
    print(f"signal_library initialized at {DEFAULT_DB}")
