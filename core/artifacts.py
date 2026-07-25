"""
core/artifacts.py — 产物登记表 + 统一图书馆（产物成为一等对象）

病根：产物散在四处（reports/、workspace/、skills/、docs/），一生下来就没身份——
不知谁生的、什么状态、活多久、绑哪个工具。「找不到 / 越积越多 / 乱堆 /
删工具后产物成孤儿」全是这个的后果。

本模块给每个落盘产物记一行户口：
    {id, producer, kind, path, label, state, ttl_days, size, created_at}

  - producer：谁生的，格式 "skill:<名>" / "report:<类型>" / "workflow:<名>" / "manual"
  - kind：类型（报告/清单/文档/情报/其他），也是图书馆里的分类目录名
  - state：trial（试用产物，短 TTL）| kept（保留）| expired（过期待清）
  - 图书馆：DATA_DIR/图书馆/<kind>/<日期>_<标签><扩展名> —— 面向人的整理视图
    （硬链接优先，跨卷回退复制）。代码照旧从原路径读；人从 Finder 打开图书馆
    一目了然。「产物在哪」与「代码在哪」彻底分开。

清理哲学（与确认闸配合）：
  - sweep_expired() 只【标记】过期并移出图书馆视图，绝不删原文件；
  - 真正删除文件走 purge_producer(delete_files=True)，由工具层暴露为
    irreversible 动作 —— 自动被 core/effects 的确认闸拦一道，用户点头才删。

复用 core.memory 的 memory.db（新表 artifacts），与 reports 的 index 思路一致，
只是统一到所有产物。
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from core.memory import _get_conn

try:
    import config
    LIBRARY_DIR = config.DATA_DIR / "图书馆"
except Exception:  # 测试/沙箱环境无 config 时退化到仓库内
    LIBRARY_DIR = Path(__file__).resolve().parent.parent / "图书馆"

KINDS = ["报告", "清单", "文档", "情报", "其他"]
DEFAULT_TRIAL_TTL_DAYS = 7   # trial 产物默认保质期；kept 默认不过期

_SAFE_LABEL = re.compile(r"[^\w一-鿿\-]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS artifacts (
                id            TEXT PRIMARY KEY,
                producer      TEXT NOT NULL,
                kind          TEXT NOT NULL DEFAULT '其他',
                path          TEXT NOT NULL,
                library_path  TEXT NOT NULL DEFAULT '',
                label         TEXT NOT NULL DEFAULT '',
                state         TEXT NOT NULL DEFAULT 'kept',
                ttl_days      INTEGER,
                size          INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_producer ON artifacts(producer);
            CREATE INDEX IF NOT EXISTS idx_artifacts_state    ON artifacts(state);
        """)


# ── 图书馆视图 ────────────────────────────────────────────────────────────────

def _library_link(src: Path, kind: str, label: str) -> str:
    """把产物挂进图书馆（硬链接优先，失败回退复制）。返回图书馆路径（失败空串）。"""
    try:
        kind_dir = LIBRARY_DIR / (kind if kind in KINDS else "其他")
        kind_dir.mkdir(parents=True, exist_ok=True)
        safe = _SAFE_LABEL.sub("_", label).strip("_") or src.stem
        dst = kind_dir / f"{date.today().isoformat()}_{safe}{src.suffix}"
        n = 1
        while dst.exists():
            dst = kind_dir / f"{date.today().isoformat()}_{safe}_{n}{src.suffix}"
            n += 1
        try:
            os.link(src, dst)          # 同卷零拷贝
        except OSError:
            shutil.copy2(src, dst)     # 跨卷回退
        return str(dst)
    except Exception:
        return ""                      # 图书馆是锦上添花，绝不阻断登记


def _unlink_library(library_path: str) -> None:
    try:
        p = Path(library_path)
        if library_path and p.exists():
            p.unlink()
    except Exception:
        pass


# ── 登记 / 查询 / 状态 ────────────────────────────────────────────────────────

def register(path: str | Path, *, producer: str, kind: str = "其他",
             label: str = "", state: str = "kept",
             ttl_days: Optional[int] = None) -> dict:
    """登记一个产物。state='trial' 未给 ttl 时套默认试用期。"""
    p = Path(path)
    if state == "trial" and ttl_days is None:
        ttl_days = DEFAULT_TRIAL_TTL_DAYS
    size = p.stat().st_size if p.exists() else 0
    lib = _library_link(p, kind, label or p.stem) if p.exists() else ""
    rec = {
        "id": uuid.uuid4().hex[:12], "producer": producer, "kind": kind,
        "path": str(p), "library_path": lib, "label": label or p.stem,
        "state": state, "ttl_days": ttl_days, "size": size,
        "created_at": _now(), "updated_at": _now(),
    }
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO artifacts (id, producer, kind, path, library_path, label,"
            " state, ttl_days, size, created_at, updated_at)"
            " VALUES (:id, :producer, :kind, :path, :library_path, :label,"
            " :state, :ttl_days, :size, :created_at, :updated_at)", rec)
    return rec


def list_artifacts(producer: Optional[str] = None, state: Optional[str] = None,
                   limit: int = 100) -> list[dict]:
    q, args = "SELECT * FROM artifacts", []
    conds = []
    if producer:
        conds.append("producer = ?"); args.append(producer)
    if state:
        conds.append("state = ?"); args.append(state)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
    with _get_conn() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def set_state(artifact_id: str, state: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("UPDATE artifacts SET state = ?, updated_at = ? WHERE id = ?",
                           (state, _now(), artifact_id))
        return cur.rowcount > 0


def footprint() -> dict:
    """数据足迹总览：按 producer 汇总 {条数, 总大小}。回答「你攒了我多少东西」。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT producer, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes,"
            " SUM(CASE WHEN state='trial' THEN 1 ELSE 0 END) AS trials"
            " FROM artifacts GROUP BY producer ORDER BY bytes DESC").fetchall()
    return {r["producer"]: {"count": r["n"], "bytes": r["bytes"], "trials": r["trials"]}
            for r in rows}


# ── 回收 ──────────────────────────────────────────────────────────────────────

def sweep_expired(today: Optional[datetime] = None) -> list[dict]:
    """标记过期的 trial 产物（超 ttl_days），移出图书馆视图。【不删原文件】。"""
    now = today or datetime.now(timezone.utc)
    swept = []
    for a in list_artifacts(state="trial", limit=10000):
        ttl = a.get("ttl_days")
        if ttl is None:
            continue
        try:
            born = datetime.fromisoformat(a["created_at"])
        except ValueError:
            continue
        if now - born > timedelta(days=ttl):
            set_state(a["id"], "expired")
            _unlink_library(a.get("library_path", ""))
            swept.append(a)
    return swept


def purge_producer(producer: str, *, delete_files: bool = False) -> dict:
    """清一个 producer 名下所有产物。

    delete_files=False：仅列出（供「删还是留」的对话）；
    delete_files=True ：删登记 + 图书馆链接 + 原文件（不可逆——工具层应
    以 irreversible 效应暴露，让确认闸拦一道）。
    """
    items = list_artifacts(producer=producer, limit=10000)
    if not delete_files:
        return {"producer": producer, "items": items, "deleted": False}
    for a in items:
        _unlink_library(a.get("library_path", ""))
        try:
            p = Path(a["path"])
            if p.exists():
                p.unlink()
        except Exception:
            pass
    with _get_conn() as conn:
        conn.execute("DELETE FROM artifacts WHERE producer = ?", (producer,))
    return {"producer": producer, "items": items, "deleted": True}


init_db()
