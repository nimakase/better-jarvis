"""prospecting/outreach_state.py — 冷开发状态判定(纯逻辑,零依赖、可单测)。

设计见 docs/客户循环-view管理重设计.md。计数底座 = 联系人层:
  - 一个联系人连发 3 封同标题邮件、无回复 = 该联系人到顶(一轮)。
  - 账户状态由各联系人状态往上推;换不换联系人由 Ned 决定,这里只摊牌。

输入来自 Breeze 抽取(逐联系人 outreach)+ HubSpot 账户关联的全部联系人名单。
本模块【只算】,不读 HubSpot、不问 Breeze、不写 view。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from prospecting.account_grading import _to_date  # 复用日期解析(纯函数)

# ── 旋钮(与设计文档一致)──────────────────────────────────────
ROUND_SIZE = 3            # 一轮 = 给一个联系人连发几封
LIMIT_CONTACTS = 2        # 到顶 = 有几个联系人各跑满一轮且无回复
CORE_MAINTAIN_DAYS = 60   # core 距上次 touch 超过这个天数 → 维护到点(2 个月)

# 账户冷开发状态 → 中文 view 名(prospecting 流)
VIEW_NAME = {
    "replied": "已回复·待跟进",
    "not_started": "未开发",
    "in_progress": "开发中",
    "one_done": "1联系人到顶·可发第2个",
    "at_limit": "到顶",
}


def _norm_name(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def _n_sent(sent_dates) -> int:
    return sum(1 for d in (sent_dates or []) if d)


def contact_state(sent_dates, replied: bool) -> str:
    """单个联系人的状态。replied > exhausted(≥3封无回复) > in_progress(1-2封) > none。"""
    if replied:
        return "replied"
    n = _n_sent(sent_dates)
    if n >= ROUND_SIZE:
        return "exhausted"
    if n >= 1:
        return "in_progress"
    return "none"


def account_outreach_state(contacts: list, all_contacts: Optional[list] = None) -> dict:
    """算某 prospecting 账户的冷开发状态。

    contacts:     [{"name","job_title","sent_dates":[ISO...],"replied":bool}] —— Breeze 抽的、Ned 发过信的联系人。
    all_contacts: [{"name","job_title"}] —— 账户在 HubSpot 关联的全部联系人(算"还剩几个没试过")。可空。
    返回 {state, view, exhausted_count, tried_contacts, untouched_contacts, ...}。

    优先级:有人回信 > 未开发 > 开发中(还有 sequence 在跑) > 到顶(≥2 联系人跑满无回复) > 1 联系人到顶。
    """
    states = [(c, contact_state(c.get("sent_dates"), bool(c.get("replied")))) for c in (contacts or [])]
    replied = [c for c, s in states if s == "replied"]
    exhausted = [c for c, s in states if s == "exhausted"]
    in_progress = [c for c, s in states if s == "in_progress"]

    if replied:
        state = "replied"
    elif not states:
        state = "not_started"
    elif in_progress:
        state = "in_progress"          # 还有联系人在跑 sequence(HubSpot 自动发)→ 先别管
    elif len(exhausted) >= LIMIT_CONTACTS:
        state = "at_limit"
    elif len(exhausted) >= 1:
        state = "one_done"
    else:
        state = "not_started"

    emailed = {_norm_name(c.get("name")) for c, _ in states if c.get("name")}
    untouched = []
    for c in (all_contacts or []):
        if _norm_name(c.get("name")) not in emailed:
            untouched.append({"name": c.get("name"), "job_title": c.get("job_title")})

    return {
        "state": state,
        "view": VIEW_NAME[state],
        "exhausted_count": len(exhausted),
        "in_progress_count": len(in_progress),
        "replied_contacts": [c.get("name") for c in replied],
        "tried_contacts": [
            {"name": c.get("name"), "job_title": c.get("job_title"),
             "n_sent": _n_sent(c.get("sent_dates")), "state": s}
            for c, s in states
        ],
        "untouched_contacts": untouched,
        "untouched_count": len(untouched),
    }


def core_maintenance_due(last_touch, today: Optional[date] = None,
                         interval_days: int = CORE_MAINTAIN_DAYS) -> bool:
    """core 账户是否到维护点:从没 touch → 是;上次 touch 距今 ≥ interval_days → 是。"""
    today = today or datetime.now(timezone.utc).date()
    d = _to_date(last_touch)
    if d is None:
        return True
    return (today - d).days >= interval_days
