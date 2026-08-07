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
from prospecting import reply_router
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
    """夜跑主编排(v2)。读全书 → 剔缺名 → 分 prospecting/core:
      - Prospecting:Breeze 抽 outreach(带 domain 消歧)→ 状态机(轮次+双时钟);检测到 inbound
        (reply_pending)→ reply_classify 定局(真回复→已回复+出提议;OOO/none→回落轮次);
        Breeze error(重名/缺名)跳过、不误标 not_started。
      - Core:Breeze 数 deal(ask_deal_summary,HubSpot 无赢单列)→ core_tier(won→T0/T1)→ 按 tier 判维护到点。
      - Decay 死线:HubSpot 无 Decay Stage 列 → derive_decay_stage(Last Activity)推导。
    各段名单从 store 累积写进对应 HubSpot view(名单法)。回复提议进 customer_loop_store 待确认(不自动写)。
    apply=False 全程 dry-run;limit 限【每类】账户数(分批,冷启动用)。view_url_map: {段名: view_url}
    (段名见 outreach_state.VIEW_NAME + "维护到点")。

    grade_view_url:全字段源 view(含 Account type/deals/日期/**Company domain name** 列 + 全部账户);
      默认取 connectors.customer_loop_tools._view_url()。段 view 只有名字列,不能拿来读。

    ⚠ 依赖 Breeze/名单法/LLM(均已实盘校准)。Bitable 驾驶舱 + 建 Task 留后续批次。
    """
    from prospecting import account_reader as reader, breeze_outreach, view_writer, reply_classify
    from prospecting import customer_loop_store as cls

    if grade_view_url is None:
        try:
            import connectors.customer_loop_tools as clt
            grade_view_url = clt._view_url()
        except Exception:
            grade_view_url = None
    if grade_view_url:                       # 必须先到全字段源 view 再读(否则缺列报错)
        browser.page.goto(grade_view_url, wait_until="domcontentloaded")
        browser.page.wait_for_timeout(2500)

    res = reader.read_all(browser)
    records = res.get("rows", [])
    expected = res.get("expected_total")
    if expected is not None and len(records) < expected:
        return {"skipped": "读取不完整,跳过本次(重跑)", "read": f"{len(records)}/{expected}"}

    by_name = {(r.get("account_name") or ""): r for r in records}
    nameless = nameless_accounts(records)                       # 导入缺名:剔除 + 单独上报
    prospecting, core = split_accounts(records)

    # 断点续跑:跳过 store 里已算过的(崩了/分批重跑接着来,不重复问 Breeze)。limit 限每类数量(分批)。
    done = set(outreach_store.all_states().keys()) if resume else set()
    pros_todo = [a for a in prospecting if outreach_store._norm(a) not in done]
    core_todo = [c for c in core if outreach_store._norm(c["name"]) not in done]
    if limit:
        pros_todo, core_todo = pros_todo[:limit], core_todo[:limit]

    errors, proposals = [], []

    # ── Prospecting:Breeze 抽 outreach(带 domain 消歧)→ 状态机 → 回复交 reply_classify 定局 ──
    for acct in pros_todo:
        rec = by_name.get(acct, {})
        site = rec.get("company_domain")
        r = breeze_outreach.ask_breeze(browser, acct, website=site, logger=logger)
        if r.get("error"):
            errors.append({"account": acct, "error": r["error"]})   # ⚠ 别持久化成 not_started
            continue
        contacts = r.get("contacts", [])
        decay = outreach_state.derive_decay_stage(rec.get("last_activity_date"), today=today)
        st = account_outreach_state(contacts, decay_stage=decay, today=today)
        if st.get("state") == "reply_pending":                  # 检测到 inbound → reply_classify 判真假
            verdict = reply_classify.classify(browser.page, acct, logger=logger, website=site)
            st = account_outreach_state(contacts, decay_stage=decay,
                                        reply_is_real=verdict is not None, today=today)
            if verdict:                                         # 真回复 → 出路由提议(待确认,不自动写)
                prop = reply_router.route(verdict.get("category"),
                                          stock_wake_days=verdict.get("stock_wake_days"), account_name=acct)
                proposals.append({"account": acct, "proposal": prop,
                                  "reply_date": verdict.get("reply_date"), "summary": verdict.get("summary")})
        outreach_store.upsert_account_state(acct, st)

    # ── Core:Breeze 数 deal(HubSpot 无赢单列)→ core_tier(won→T0/T1)→ 按 tier 判维护到点 ──
    core_due = []
    for c in core_todo:
        name = c["name"]
        rec = by_name.get(name, {})
        ds = breeze_outreach.ask_deal_summary(browser, name, website=rec.get("company_domain"), logger=logger)
        won = (ds.get("parsed") or {}).get("won", 0)
        openn = rec.get("num_open_deals") or (ds.get("parsed") or {}).get("open", 0)
        tier = grading.core_tier(won, open_deals=openn)         # list_quality 暂缺(Bitable note 未接)
        due = outreach_state.core_maintenance_due(
            c.get("last_activity"), tier=tier if tier in ("T0", "T1", "T2") else None, today=today)
        outreach_store.upsert_account_state(name, {
            "state": "core", "tier": tier, "won": won, "maintain_due": due,
            "view": MAINTAIN_VIEW if due else None})            # 只有到点的进"维护到点"view
        if due:
            core_due.append(name)

    if proposals:
        cls.save_proposals(proposals)                          # 回复提议进待确认库(不自动写)

    # ── 从 store 【累积】汇总各段,写进对应 HubSpot view(名单法)──
    def _write(url, names, ap):
        return view_writer.set_view_membership(browser, url, names, apply=ap, logger=logger)
    cumulative = outreach_store.accounts_by_view()
    applied = apply_segments(cumulative, lambda v: view_url_map.get(v), _write, apply=apply)

    return {
        "prospecting_total": len(prospecting), "core_total": len(core),
        "processed_prospecting": len(pros_todo), "processed_core": len(core_todo),
        "reply_proposals": len(proposals), "core_due": len(core_due),
        "nameless_count": len(nameless), "breeze_errors": errors,
        "cumulative_segments": {k: len(v) for k, v in cumulative.items()},
        "applied": applied, "applied_mode": "APPLY" if apply else "dry-run",
    }
