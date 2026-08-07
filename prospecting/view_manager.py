"""prospecting/view_manager.py — 编排:Breeze 抽取 → 算状态 → 存 → 归段 → 写 view。

核心 compute_segments / apply_segments 用【注入依赖】,纯逻辑可单测(见 tests)。
run() 把真依赖(breeze_outreach + view_writer)接上,驱动浏览器。

数据流见 docs/客户循环-view管理重设计.md §3。
"""
from __future__ import annotations

from typing import Callable, Optional

from prospecting import outreach_store
from prospecting import outreach_state
from prospecting import account_grading as grading
from prospecting.outreach_state import account_outreach_state


def compute_segments(accounts: list,
                     breeze_ask: Callable[[str], list],
                     roster_of: Optional[Callable[[str], list]] = None,
                     persist: bool = True,
                     today=None) -> dict:
    """逐 prospecting 账户:Breeze 抽联系人 → 算状态 → (存) → 归段。

    breeze_ask(account) -> [{name,job_title,sent_dates,replied}](已按联系人聚合)
    roster_of(account)  -> [{name,job_title}] 全部关联联系人(算未试),可空
    返回 {view: [account, ...]}。
    """
    segments: dict = {}
    for acct in accounts:
        contacts = breeze_ask(acct) or []
        roster = roster_of(acct) if roster_of else None
        state = account_outreach_state(contacts, all_contacts=roster, today=today)
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


MAINTAIN_VIEW = "维护到点"          # core 维护段的 view 名(view_url_map 的键)


def is_valid_account_name(name) -> bool:
    """账户名是否可用:非空、非 HubSpot 的 '--' 占位、且含至少一个字母数字。

    导入信息缺失时 account name 会渲染成 '--' —— 这类账户【无名、不可寻址】(名单法按名定位 view),
    必须剔除出处理流,否则会被当成名叫 '--' 的真账户去问 Breeze、并把 '--' 写进 view 过滤器(垃圾)。
    """
    s = "".join(str(name or "").split()).strip()
    if s in ("", "--"):
        return False
    return any(ch.isalnum() for ch in s)


def nameless_accounts(records: list) -> list:
    """挑出导入缺名(account_name 为空 / '--' / 纯符号)的账户记录 —— 无法进任何 view,
    要【单独上报】让 Ned 补名或重导,不进 prospecting/core。返回原始记录列表。"""
    return [r for r in (records or []) if not is_valid_account_name(r.get("account_name"))]


def split_accounts(records: list):
    """把读取到的账户记录分成 (prospecting 账户名列表, core 账户[{name,last_activity}])。

    用 account_grading.classify 判 type(有 deal → core)。缺名账户(见 is_valid_account_name)剔除。纯逻辑,可单测。
    """
    prospecting, core = [], []
    for r in records:
        name = (r.get("account_name") or "").strip()
        if not is_valid_account_name(name):
            continue
        if grading.classify(r)["type"] == "core":
            core.append({"name": name, "last_activity": r.get("last_activity_date")})
        else:
            prospecting.append(name)
    return prospecting, core


def core_maintenance_segment(core_accounts: list, today=None) -> list:
    """core 里到维护点(>2 月没 touch)的账户名。纯逻辑,可单测。"""
    return [c["name"] for c in (core_accounts or [])
            if outreach_state.core_maintenance_due(c.get("last_activity"), today=today)]


def run(browser, accounts: list, view_url_map: dict,
        roster_of: Optional[Callable[[str], list]] = None,
        apply: bool = False, logger=None) -> dict:
    """(仅 prospecting)真依赖编排。view_url_map: {view中文名: view_url}。⚠ 依赖 Breeze/名单法实盘校准。"""
    from prospecting import breeze_outreach, view_writer

    def _ask(acct):
        return breeze_outreach.ask_breeze(browser, acct, logger=logger).get("contacts", [])

    def _write(url, names, ap):
        return view_writer.set_view_membership(browser, url, names, apply=ap, logger=logger)

    segments = compute_segments(accounts, _ask, roster_of=roster_of, persist=True)
    applied = apply_segments(segments, lambda v: view_url_map.get(v), _write, apply=apply)
    return {"segments": {k: len(v) for k, v in segments.items()},
            "applied": applied, "accounts": len(accounts)}


def run_view_cycle(browser, view_url_map: dict, grade_view_url: Optional[str] = None,
                   apply: bool = False, limit: Optional[int] = None, resume: bool = True,
                   today=None, logger=None) -> dict:
    """夜跑主编排:读全书 → 分 prospecting/core → Breeze 抽 outreach 算冷开发段 +
    core 维护段 → 各段名单写进对应 view。view_url_map: {段名: view_url}(段名见 outreach_state.VIEW_NAME
    + "维护到点")。apply=False 全程 dry-run(不改 view)。limit 限 prospecting 数量(冷启动分批)。

    grade_view_url:全字段的分级源 view(有 type/deal/日期全套列 + 全部账户);默认从
      connectors.customer_loop_tools._view_url() 取(68742792)。段 view 只有名字列,不能拿来读。

    ⚠ 依赖 Breeze/名单法(已实盘校准)。回信账户已进"已回复·待跟进"段;建 Task 提醒留后续
      (需回复分类给出跟进日期,见 reply_path)。
    """
    from prospecting import account_reader as reader, breeze_outreach, view_writer

    if grade_view_url is None:
        try:
            import connectors.customer_loop_tools as clt
            grade_view_url = clt._view_url()
        except Exception:
            grade_view_url = None
    if grade_view_url:                       # 必须先到全字段源 view 再读(否则缺列报错)
        browser.page.goto(grade_view_url, wait_until="domcontentloaded")
        browser.page.wait_for_timeout(2500)

    report = reader.grade_all(browser)
    if not report.get("complete", True):
        return {"skipped": "读取不完整,跳过本次(重跑)",
                "read": f"{report.get('total')}/{report.get('expected_total')}"}

    prospecting, core = split_accounts(report.get("records", []))

    # 断点续跑:跳过 store 里已算过的 prospecting(崩了/分批重跑时接着来,不重复问 Breeze)。
    todo = prospecting
    if resume:
        done = set(outreach_store.all_states().keys())
        todo = [a for a in prospecting if outreach_store._norm(a) not in done]
    if limit:
        todo = todo[:limit]

    def _ask(acct):
        return breeze_outreach.ask_breeze(browser, acct, logger=logger).get("contacts", [])

    compute_segments(todo, _ask, persist=True, today=today)   # 逐账户算+即时存 store(返回值不用)

    # core 维护到点也入 store(供累积写 view;core 不用 Breeze)
    due = core_maintenance_segment(core, today=today)
    for name in due:
        outreach_store.upsert_account_state(name, {"state": "core_maintain", "view": MAINTAIN_VIEW})

    def _write(url, names, ap):
        return view_writer.set_view_membership(browser, url, names, apply=ap, logger=logger)

    # 从 store 【累积】汇总各段(含历史已处理),写进 view —— 分批/重跑也能拿到完整名单。
    cumulative = outreach_store.accounts_by_view()
    applied = apply_segments(cumulative, lambda v: view_url_map.get(v), _write, apply=apply)
    return {"prospecting_total": len(prospecting), "processed_this_run": len(todo),
            "core_total": len(core), "core_due": len(due),
            "cumulative_segments": {k: len(v) for k, v in cumulative.items()},
            "applied": applied, "applied_mode": "APPLY" if apply else "dry-run"}
