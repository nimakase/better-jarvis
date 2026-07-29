"""prospecting/customer_loop_store.py — 客户循环的贾维斯侧状态库(情报层的一部分)。

见 docs/客户循环与Breeze设计方案.md §0「贾维斯侧数据库」。这里存 HubSpot 存不了的:
  - 冷启动是否已应用、每次运行的记录;
  - 「只覆盖自己写的」守卫所需的"jarvis 上次写的值"(读 account_writer 落的 write_log.jsonl)。

轮数/诊断/回复提炼等其它衍生数据后续再加(优先复用 core/entities、intel/signal_library)。
纯标准库,可单测(存储目录可用环境变量 JARVIS_CUSTOMER_LOOP_DIR 覆盖)。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def store_dir() -> Path:
    override = os.environ.get("JARVIS_CUSTOMER_LOOP_DIR")
    if override:
        d = Path(override)
    else:
        try:
            import config
            d = config.DATA_DIR / "customer_loop"
        except Exception:
            d = Path.home() / ".jarvis" / "customer_loop"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 状态(冷启动标记 / 运行记录)────────────────────────────────
def _state_path() -> Path:
    return store_dir() / "state.json"


def _load_state() -> dict:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(st: dict) -> None:
    _state_path().write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def is_coldstart_applied() -> bool:
    return bool(_load_state().get("coldstart_applied"))


def mark_coldstart_applied(summary: Optional[dict] = None) -> None:
    st = _load_state()
    st["coldstart_applied"] = True
    st["coldstart_applied_at"] = _now()
    if summary is not None:
        st["coldstart_summary"] = summary
    _save_state(st)


def reset_coldstart() -> None:
    """把「冷启动已应用」重置回未应用——下次夜跑会重新做全书冷启动对账。
    用于:调了阈值想重来,或首次 apply 有失败要重跑。"""
    st = _load_state()
    st["coldstart_applied"] = False
    st.pop("coldstart_applied_at", None)
    _save_state(st)


def record_run(kind: str, summary: Optional[dict] = None) -> None:
    st = _load_state()
    runs = st.setdefault("runs", [])
    runs.append({"kind": kind, "at": _now(), "summary": summary or {}})
    st["runs"] = runs[-200:]   # 只留最近 200 条
    _save_state(st)


def last_run(kind: str) -> Optional[dict]:
    for r in reversed(_load_state().get("runs", [])):
        if r.get("kind") == kind:
            return r
    return None


# ── 回复路:已处理回信跟踪 + 待确认提议 ─────────────────────────
def _reply_key(account: str, reply_date) -> str:
    return f"{account}|{reply_date or '-'}"


def is_reply_processed(account: str, reply_date) -> bool:
    """这条回信(account+reply_date)是否已生成过提议——防每晚重复提同一条。"""
    return _reply_key(account, reply_date) in set(_load_state().get("processed_replies", []))


def mark_reply_processed(account: str, reply_date) -> None:
    st = _load_state()
    keys = st.get("processed_replies", [])
    k = _reply_key(account, reply_date)
    if k not in keys:
        keys.append(k)
    st["processed_replies"] = keys[-2000:]
    _save_state(st)


def save_proposals(proposals: list) -> None:
    """追加待确认提议(回复派生的 Note/Task,等 Ned 批准才写)。"""
    st = _load_state()
    pend = st.get("pending_proposals", [])
    pend.extend(proposals or [])
    st["pending_proposals"] = pend[-500:]
    _save_state(st)


def get_pending_proposals() -> list:
    return _load_state().get("pending_proposals", [])


def archive_proposals(proposals: list) -> None:
    """把一批提议(回复判断)追加进历史日志——留给将来回测对表用。用完即删的东西
    事后补不回来,故批准前先归档。每条记 archived_at + 原始提议(含分类摘要)。"""
    if not proposals:
        return
    path = store_dir() / "proposal_history.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for p in proposals:
            rec = dict(p)
            rec["archived_at"] = _now()
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def clear_proposals() -> None:
    st = _load_state()
    st["pending_proposals"] = []
    _save_state(st)


# ── 池成员快照(检测账户离开/进入客户池)────────────────────────
def _pool_snapshot_path() -> Path:
    return store_dir() / "pool_snapshot.json"


def _norm_accounts(accounts: list) -> list:
    """去空、去重、排序,统一比对口径。"""
    return sorted({(a or "").strip() for a in (accounts or []) if (a or "").strip()})


def diff_pool(current_accounts: list) -> dict:
    """当前池账户名 vs 上次快照,返回 {first_run, left, entered, prev_count, curr_count}。

    只比对不落盘;调用方拿到 diff、用于报告后,再 save_pool_snapshot 落新快照。
    left = 上次有这次没有(被 reassign / reset 工作流挪走 / 手动放弃);entered = 新进池。
    """
    cur = _norm_accounts(current_accounts)
    p = _pool_snapshot_path()
    if not p.exists():
        return {"first_run": True, "left": [], "entered": [],
                "prev_count": None, "curr_count": len(cur)}
    try:
        prev = json.loads(p.read_text(encoding="utf-8")).get("accounts", [])
    except Exception:
        prev = []
    prev_set, cur_set = set(prev), set(cur)
    return {"first_run": False,
            "left": sorted(prev_set - cur_set),
            "entered": sorted(cur_set - prev_set),
            "prev_count": len(prev_set), "curr_count": len(cur_set)}


def save_pool_snapshot(current_accounts: list) -> None:
    """把当前池账户名落成新快照(供下次 diff)。只在【完整读取】后调用,避免漏读误判离开。"""
    cur = _norm_accounts(current_accounts)
    _pool_snapshot_path().write_text(
        json.dumps({"at": _now(), "count": len(cur), "accounts": cur},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")


# ── 「只覆盖自己写的」守卫 ──────────────────────────────────────
def _write_log_path() -> Path:
    return store_dir() / "write_log.jsonl"


def jarvis_last_write(account: str, field: str):
    """返回 jarvis 最近一次【真写】的该字段值;从没写过返回 None。"""
    p = _write_log_path()
    if not p.exists():
        return None
    val = None
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("applied") and r.get("account") == account and r.get("field") == field:
                val = r.get("value")   # 后写覆盖,循环到底得最后一次
    except Exception:
        return None
    return val


def _is_blank(v) -> bool:
    return v in (None, "", "--")


def should_auto_write(account: str, field: str, current_value) -> bool:
    """稳态守卫:是否允许自动改这个字段(不误伤人手改动)。

    规则:
      - jarvis 从没写过这字段 → 只在现值为空时才写(填空白;非空多是人工/遗留,不碰);
      - jarvis 写过 → 只有现值 == jarvis 上次写的,才算「它自己的」可续改;
        现值 ≠ 上次写的 → 是人手动改过 → 退让不写。
    (冷启动首轮不走这里,走 reconcile 桶。)
    """
    last = jarvis_last_write(account, field)
    if last is None:
        return _is_blank(current_value)
    return current_value == last
