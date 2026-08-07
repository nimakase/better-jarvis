"""prospecting/reply_router.py — 回复路由器(纯逻辑,v2)。

设计见 docs/客户循环-view管理重设计.md 附二 §14。输入:回复类别(Breeze 读客户邮件后分类)+
少量抽取信息(跟进时机)。输出:一份【提议】(该写什么 Note、建什么 Task、动什么、哪些你手动做)。

v2(2026-08-05):8 类【按动作归并成 6 类】—— 原 interested_no_stock / stuck_nda / has_channel
三者动作相同(记 Note + 排带唤醒日的跟进 Task),差别只在唤醒天数(参数),故合成 interested_later,
唤醒天数由分类器估的 stock_wake_days 带入(NDA 类给短、有渠道给长、无货给中);原因/对手写进 Note 摘要。
动作从下线的 priority 字段【重定向】到 v2:list_quality(喂 Bitable 的 list 质量判断)/ build_deal /
switch_contact / sequence_opt_out / task / your_action。不再写 priority、不再喂旧"月度剔除工具"。

⚠ 这些都是【提议】,不自动执行——Breeze 读的是客户邮件(不可信内容),按 trust 污染闸,
   同回合不能自动对外写。提议进【日报/Bitable 待确认】,Ned 批准(干净新回合)后才写。

纯逻辑、零依赖、可单测。Note 正文由 Breeze 的英文摘要填,本模块只决定"要不要 Note、配什么动作"。
"""
from __future__ import annotations

# ── 六类回复(= 六个互不相同的动作)──────────────────────────
LIST_RELEVANT = "list_relevant"          # 发来【可做的】料 list → 你建 deal 转 Core
LIST_IRRELEVANT = "list_irrelevant"      # 发来 list 但成品/不可做 → 标 junk
INTERESTED_LATER = "interested_later"    # 有兴趣但当下无货可卖(暂无货 / 卡 NDA / 已有渠道)→ 排跟进
EXPLICIT_NO = "explicit_no"              # 明确不做 → 停 sequence + 释放
REFERRAL = "referral"                    # 转介给别人 → 换 contact 重开
PLEASANTRY = "pleasantry"                # 纯客套 /"我看看" → 无动作、存档

CATEGORIES = {LIST_RELEVANT, LIST_IRRELEVANT, INTERESTED_LATER,
              EXPLICIT_NO, REFERRAL, PLEASANTRY}

# ── 可调旋钮(天)────────────────────────────────────────
DEFAULT_WAKE_DAYS = 90        # interested_later 没给明确时机时的默认跟进周期


def _task(wake_days: int, title: str) -> dict:
    return {"wake_days": wake_days, "title": title}


def route(category: str, *, stock_wake_days: int | None = None,
          account_name: str = "") -> dict:
    """把一类回复映射成提议(v2)。

    stock_wake_days:interested_later 时分类器估的跟进时机(天);None 则用默认 90。
    返回 dict:
      category, make_note(bool), note_tag, task({wake_days,title} 或 None),
      your_action(需你手动做的事 或 None), sequence_opt_out(bool),
      list_quality("good"/"junk"/None,喂 Bitable list 质量), build_deal(bool), switch_contact(bool)
    """
    if category not in CATEGORIES:
        raise ValueError(f"未知回复类别:{category}")

    p = {"category": category, "make_note": False, "note_tag": "", "task": None,
         "your_action": None, "sequence_opt_out": False,
         "list_quality": None, "build_deal": False, "switch_contact": False}
    acct = account_name or "this account"

    if category == LIST_RELEVANT:
        # 发来可做的料 → 你当天建 deal(=痕迹,不 Note),自动走 Core;顺带记 list 质量=good。
        p["build_deal"] = True
        p["list_quality"] = "good"
        p["your_action"] = "work this list & log a deal (per discipline)"

    elif category == LIST_IRRELEVANT:
        p["make_note"] = True
        p["note_tag"] = "irrelevant list (finished goods)"
        p["list_quality"] = "junk"       # 喂 Bitable list 质量 → not-core(L6),别再硬发

    elif category == INTERESTED_LATER:
        # 合并原 no_stock / stuck_nda / has_channel:同一个动作 = Note + 带唤醒日的跟进 Task。
        # 唤醒天数由分类器 stock_wake_days 带入(NDA 短、渠道长、无货中);原因/对手在 Note 摘要里。
        p["make_note"] = True
        p["note_tag"] = "interested, not now"
        p["task"] = _task(stock_wake_days or DEFAULT_WAKE_DAYS, f"Ask {acct} for excess list")

    elif category == EXPLICIT_NO:
        p["make_note"] = True
        p["note_tag"] = "explicit no (record reason)"
        p["sequence_opt_out"] = True     # 掐自动发信,护发件人信誉
        p["your_action"] = "consider releasing to the ownerless pool"

    elif category == REFERRAL:
        p["make_note"] = True
        p["note_tag"] = "referral to another contact"
        p["switch_contact"] = True       # 喂"换 contact 重开"分支
        p["your_action"] = "start outreach to the new contact ('your boss asked me to reach you')"

    elif category == PLEASANTRY:
        # 纯客套 → 无动作:一句"收到/我看看"不值得排提醒;更怕热单被误分成客套后自动排提醒
        # 反而有害。想跟进你自己会跟。分类本身仍进贾维斯库存档。
        pass

    return p


def is_actionable(proposal: dict) -> bool:
    """提议里有没有真要做的事(task/你手动动作/opt-out/建 deal/换人/list 质量标记)。
    没有(如纯客套)→ 不进待确认清单,免噪音。"""
    return bool(proposal.get("task") or proposal.get("your_action")
                or proposal.get("sequence_opt_out") or proposal.get("build_deal")
                or proposal.get("switch_contact") or proposal.get("list_quality"))
