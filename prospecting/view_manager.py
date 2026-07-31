"""prospecting/view_manager.py — 编排:Breeze 抽取 → 算状态 → 存 → 归段 → 写 view。

核心 compute_segments / apply_segments 用【注入依赖】,纯逻辑可单测(见 tests)。
run() 把真依赖(breeze_outreach + view_writer)接上,驱动浏览器。

数据流见 docs/客户循环-view管理重设计.md §3。
"""
from __future__ import annotations

from typing import Callable, Optional

from prospecting import outreach_store
from prospecting.outreach_state import account_outreach_state


def compute_segments(accounts: list,
                     breeze_ask: Callable[[str], list],
                     roster_of: Optional[Callable[[str], list]] = None,
                     persist: bool = True) -> dict:
    """逐 prospecting 账户:Breeze 抽联系人 → 算状态 → (存) → 归段。

    breeze_ask(account) -> [{name,job_title,sent_dates,replied}](已按联系人聚合)
    roster_of(account)  -> [{name,job_title}] 全部关联联系人(算未试),可空
    返回 {view: [account, ...]}。
    """
    segments: dict = {}
    for acct in accounts:
        contacts = breeze_ask(acct) or []
        roster = roster_of(acct) if roster_of else None
        state = account_outreach_state(contacts, all_contacts=roster)
        if persist:
            outreach_store.upsert_account_state(acct, state)
        segments.setdefault(state["view"], []).append(acct)
    return segments


def apply_segments(segments: dict,
                   view_url_of: Callable[[str], Optional[str]],
                   view_write: Callable[[str, list, bool], dict],
                   apply: bool = False) -> dict:
    """把各段账户名单写进对应 HubSpot view。

    view_url_of(view_name) -> 该段对应的 view URL(Ned 预先建好并配置的映射),没有则跳过。
    view_write(url, names, apply) -> 写成员(view_writer.set_view_membership)。
    返回 {view: 写入结果}。
    """
    results: dict = {}
    for view_name, names in segments.items():
        url = view_url_of(view_name)
        if not url:
            results[view_name] = {"ok": False, "count": len(names),
                                  "reason": f"未配置 view URL: {view_name}"}
            continue
        results[view_name] = view_write(url, sorted(set(names)), apply)
    return results


def run(browser, accounts: list, view_url_map: dict,
        roster_of: Optional[Callable[[str], list]] = None,
        apply: bool = False, logger=None) -> dict:
    """真依赖编排(驱动浏览器)。view_url_map: {view中文名: view_url}。⚠ 依赖 Breeze/名单法实盘校准。"""
    from prospecting import breeze_outreach, view_writer

    def _ask(acct):
        return breeze_outreach.ask_breeze(browser, acct, logger=logger).get("contacts", [])

    def _write(url, names, ap):
        return view_writer.set_view_membership(browser, url, names, apply=ap, logger=logger)

    segments = compute_segments(accounts, _ask, roster_of=roster_of, persist=True)
    applied = apply_segments(segments, lambda v: view_url_map.get(v), _write, apply=apply)
    return {"segments": {k: len(v) for k, v in segments.items()},
            "applied": applied, "accounts": len(accounts)}
