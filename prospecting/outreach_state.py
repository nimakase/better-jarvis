"""prospecting/outreach_state.py — 冷开发状态判定(纯逻辑,零依赖、可单测)。

设计见 docs/客户循环-view管理重设计.md。【简化版】不数联系人/轮次,只看两件事:
  - 有没有人回信;
  - 最后一封 outreach 离现在多久(还在发 vs 已停)。

状态(4 个):
  未开发   —— 从没发过 outreach
  开发中   —— 最后一封 outreach 在 ACTIVE_DAYS 天内(sequence 还在跑,别管)
  待处理   —— 有发过、但最后一封超过 ACTIVE_DAYS 且没人回 → 归一类:换人发 or 放弃(Ned 定)
  已回复   —— 有联系人回过信 → 建 Task 跟进(优先级最高)

联系人明细(谁发过几封、岗位、未试过谁)照样算出来放进结果,供 Ned 换人时参考,但【不参与状态判定】。
本模块只算,不读 HubSpot、不问 Breeze、不写 view。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from prospecting.account_grading import _to_date  # 复用日期解析(纯函数)

# ── 旋钮 ──────────────────────────────────────────────────────
ACTIVE_DAYS = 14          # 最后一封 outreach 在此天数内 → 还在发(开发中);超了 → 待处理
CORE_MAINTAIN_DAYS = 60   # core 距上次 touch 超过这个天数 → 维护到点(2 个月)

VIEW_NAME = {
    "replied": "已回复·待跟进",
    "not_started": "未开发",
    "in_progress": "开发中",
    "pending": "待处理·换人或放弃",
}


def _norm_name(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def _dates(sent_dates) -> list:
    return sorted(d for d in (_to_date(x) for x in (sent_dates or [])) if d)


def account_outreach_state(contacts: list, all_contacts: Optional[list] = None,
                           today: Optional[date] = None) -> dict:
    """算某 prospecting 账户的冷开发状态(简化版)。

    contacts:     [{"name","job_title","sent_dates":[ISO...],"replied":bool}] —— Breeze 抽的。
    all_contacts: [{"name","job_title"}] —— 账户全部关联联系人(算"还剩谁没试过",供换人参考)。可空。
    today:        判定基准日;默认今天(UTC)。
    返回 {state, view, last_outreach, days_since_last, replied_contacts, tried_contacts, untouched_*}。

    优先级:有人回信 > 从没发过 > 最后一封在 14 天内(开发中)> 否则待处理。
    """
    today = today or datetime.now(timezone.utc).date()
    replied = [c.get("name") for c in (contacts or []) if c.get("replied")]

    tried, all_dates = [], []
    for c in (contacts or []):
        ds = _dates(c.get("sent_dates"))
        all_dates += ds
        tried.append({"name": c.get("name"), "job_title": c.get("job_title"),
                      "n_sent": len(ds), "last": ds[-1].isoformat() if ds else None,
                      "replied": bool(c.get("replied"))})

    if replied:
        state = "replied"
    elif not all_dates:
        state = "not_started"
    else:
        last = max(all_dates)
        state = "in_progress" if (today - last).days <= ACTIVE_DAYS else "pending"

    last_out = max(all_dates) if all_dates else None
    emailed = {_norm_name(c.get("name")) for c in (contacts or []) if c.get("name")}
    untouched = [{"name": c.get("name"), "job_title": c.get("job_title")}
                 for c in (all_contacts or []) if _norm_name(c.get("name")) not in emailed]

    return {
        "state": state,
        "view": VIEW_NAME[state],
        "last_outreach": last_out.isoformat() if last_out else None,
        "days_since_last": (today - last_out).days if last_out else None,
        "replied_contacts": replied,
        "tried_contacts": tried,          # 明细(供换人参考,不参与判定)
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
