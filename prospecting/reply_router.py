"""prospecting/reply_router.py — 回复路由器(纯逻辑)。

设计见 docs/客户循环与Breeze设计方案.md 第四节「回复路由器」。
输入:回复类别(由 Breeze 读客户邮件后分类得出)+ 少量抽取信息(时间/对手)。
输出:一份【提议】(该写什么 Note、建什么 Task、动什么字段、哪些是你手动做)。

⚠ 这些都是【提议】,不是自动执行——因为 Breeze 读的是客户邮件(不可信内容),按
   trust 污染闸,同回合不能自动对外写。提议进【日报待确认】,Ned 批准(干净新回合)后才写。
   (见护栏合规:方案 a。)

纯逻辑、零依赖、可单测。Note 正文由 Breeze 的英文摘要填(时间戳+来源+内容),
本模块只决定"要不要 Note、配什么 Task/字段/动作"。
"""
from __future__ import annotations

# ── 八类回复(与④光谱对齐)──────────────────────────────
LIST_RELEVANT = "list_relevant"          # 发来相关 list
LIST_IRRELEVANT = "list_irrelevant"      # 发来不相关 list(成品料)
INTERESTED_NO_STOCK = "interested_no_stock"  # 有兴趣·暂无货(可能带时间)
STUCK_NDA = "stuck_nda"                   # 有兴趣·卡 NDA/内部流程
HAS_CHANNEL = "has_channel"              # 已有合作渠道(提到对手)
EXPLICIT_NO = "explicit_no"              # 明确不做
REFERRAL = "referral"                    # 转介给别人
PLEASANTRY = "pleasantry"                # 纯客套 /"我看看"

CATEGORIES = {LIST_RELEVANT, LIST_IRRELEVANT, INTERESTED_NO_STOCK, STUCK_NDA,
              HAS_CHANNEL, EXPLICIT_NO, REFERRAL, PLEASANTRY}

# ── 可调旋钮(天)────────────────────────────────────────
DEFAULT_WAKE_DAYS = 90        # 暂无货没给时间时 / 纯客套跟进
STUCK_REMINDER_DAYS = 21      # 卡 NDA 的超时提醒(短,别卡死)
HAS_CHANNEL_WAKE_DAYS = 180   # 已有渠道:长周期唤醒


def _task(wake_days: int, title: str) -> dict:
    return {"wake_days": wake_days, "title": title}


def route(category: str, *, stock_wake_days: int | None = None,
          account_name: str = "") -> dict:
    """把一类回复映射成提议。

    stock_wake_days:暂无货时对方给了明确时间(如"Q4")→ 换算成天数传入;None 则用默认 90。
    返回 dict:
      category, make_note(bool), note_tag(给 Note 附的类别标签),
      task({wake_days,title} 或 None), priority("warm"/"cold"/None),
      sequence_opt_out(bool), relevance_no(bool,喂剔除工具), your_action(需你手动做的事 或 None)
    """
    if category not in CATEGORIES:
        raise ValueError(f"未知回复类别:{category}")

    p = {"category": category, "make_note": False, "note_tag": "", "task": None,
         "priority": None, "sequence_opt_out": False, "relevance_no": False,
         "your_action": None}
    acct = account_name or "this account"

    if category == LIST_RELEVANT:
        # 你当天建 deal → 自动归 Core Hot;deal 即痕迹,不 Note。
        p["your_action"] = "work this list & log a deal (per discipline)"

    elif category == LIST_IRRELEVANT:
        p["make_note"] = True
        p["note_tag"] = "irrelevant list (finished goods)"
        p["relevance_no"] = True     # 标"相关性=否",喂月度剔除工具

    elif category == INTERESTED_NO_STOCK:
        p["make_note"] = True
        p["note_tag"] = "no stock now"
        p["task"] = _task(stock_wake_days or DEFAULT_WAKE_DAYS,
                          f"Ask {acct} for excess list")
        p["priority"] = "warm"

    elif category == STUCK_NDA:
        p["make_note"] = True
        p["note_tag"] = "stuck on NDA/internal process"
        p["task"] = _task(STUCK_REMINDER_DAYS, f"Chase {acct}: NDA/process")
        p["priority"] = "warm"
        p["your_action"] = "may need you to push the NDA/internal process"

    elif category == HAS_CHANNEL:
        p["make_note"] = True
        p["note_tag"] = "already has a channel (note competitor)"
        p["task"] = _task(HAS_CHANNEL_WAKE_DAYS, f"Long wake {acct}")
        p["priority"] = "cold"

    elif category == EXPLICIT_NO:
        p["make_note"] = True
        p["note_tag"] = "explicit no (record reason)"
        p["sequence_opt_out"] = True   # 掐自动发信,护发件人信誉
        p["priority"] = "cold"
        p["your_action"] = "consider releasing to the ownerless pool"

    elif category == REFERRAL:
        p["make_note"] = True
        p["note_tag"] = "referral to another contact"
        p["your_action"] = "start outreach to the new contact ('your boss asked me to reach you')"

    elif category == PLEASANTRY:
        # 纯客套 → 无动作:一句"收到/我看看"不值得 jarvis 替你排提醒;更重要的是,
        # 一旦热单被误分成客套,自动建提醒反而有害(把该现在做的单排到几月后)。
        # 想跟进你自己会跟。分类本身仍进贾维斯库存档。
        pass

    return p


def is_actionable(proposal: dict) -> bool:
    """提议里有没有真要做的事(task/priority/opt-out/相关性标记/你手动动作)。
    没有(如纯客套)→ 不进日报待确认清单,免噪音。"""
    return bool(proposal.get("task") or proposal.get("priority")
                or proposal.get("sequence_opt_out") or proposal.get("relevance_no")
                or proposal.get("your_action"))
