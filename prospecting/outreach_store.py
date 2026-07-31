"""prospecting/outreach_store.py — 冷开发状态持久化(贾维斯侧库的一部分)。

按账户存 outreach_state.account_outreach_state() 的结果 + 算出时间。供:
  - 增量夜跑对比/复用;
  - 按 view 汇总账户名(名单法写 HubSpot view)。
复用 customer_loop_store 的 store_dir()。纯标准库,可单测(JARVIS_CUSTOMER_LOOP_DIR 覆盖目录)。
"""
from __future__ import annotations

import json
from pathlib import Path

from prospecting.customer_loop_store import store_dir, _now


def _path() -> Path:
    return store_dir() / "outreach_state.json"


def _norm(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def _load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(d: dict) -> None:
    _path().write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def upsert_account_state(account: str, state_result: dict) -> None:
    """存/更新某账户的冷开发状态(state_result = account_outreach_state 的返回)。按账户名归一为键。"""
    d = _load()
    rec = dict(state_result)
    rec["account"] = account
    rec["computed_at"] = _now()
    d[_norm(account)] = rec
    _save(d)


def get_account_state(account: str) -> dict | None:
    return _load().get(_norm(account))


def all_states() -> dict:
    """返回 {归一账户名: 记录}。"""
    return _load()


def accounts_by_view() -> dict:
    """按 view 汇总账户【原始名】(用于名单法写 HubSpot view)。返回 {view: [account, ...]}。"""
    out: dict = {}
    for rec in _load().values():
        view = rec.get("view")
        if not view:
            continue
        out.setdefault(view, []).append(rec.get("account"))
    for v in out:
        out[v] = sorted(x for x in out[v] if x)
    return out


def remove_account(account: str) -> None:
    d = _load()
    if _norm(account) in d:
        del d[_norm(account)]
        _save(d)
