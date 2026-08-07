"""回复路由器单测(6 类回复 → v2 提议)。纯逻辑。

    python -m tests.test_reply_router
"""
import sys
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from prospecting import reply_router as rr   # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def r(cat, **kw):
    return rr.route(cat, account_name="Acme", **kw)


# 六类,每类一个独立动作
check("恰好 6 类", len(rr.CATEGORIES) == 6)

# 相关 list:不 Note,建 deal + list 质量 good + 你的动作
p = r(rr.LIST_RELEVANT)
check("相关list 不 Note", p["make_note"] is False)
check("相关list build_deal", p["build_deal"] is True)
check("相关list list_quality=good", p["list_quality"] == "good")
check("相关list 是你的动作", p["your_action"] is not None)

# 不相关 list:Note + list 质量 junk(喂 not-core)
p = r(rr.LIST_IRRELEVANT)
check("不相关list Note", p["make_note"] is True)
check("不相关list list_quality=junk", p["list_quality"] == "junk")

# interested_later(合并原三类):Note + 带唤醒日 Task;无时机→默认 90,带时机→用时机
p = r(rr.INTERESTED_LATER)
check("interested_later 默认唤醒 90", p["task"]["wake_days"] == rr.DEFAULT_WAKE_DAYS)
check("interested_later Note", p["make_note"] is True)
p2 = r(rr.INTERESTED_LATER, stock_wake_days=21)
check("interested_later 带时机用时机(21,如 NDA 短跟进)", p2["task"]["wake_days"] == 21)
p3 = r(rr.INTERESTED_LATER, stock_wake_days=180)
check("interested_later 长时机(180,如已有渠道)", p3["task"]["wake_days"] == 180)

# 明确不做:Opt-Out + 释放动作(不再写 priority)
p = r(rr.EXPLICIT_NO)
check("明确不做 设 Opt-Out", p["sequence_opt_out"] is True)
check("明确不做 提示释放", p["your_action"] is not None)
check("明确不做 无 priority 字段", "priority" not in p)

# 转介:Note + switch_contact + 对外触达你来(无自动 Task)
p = r(rr.REFERRAL)
check("转介 Note", p["make_note"] is True)
check("转介 switch_contact", p["switch_contact"] is True)
check("转介 对外触达你来", p["your_action"] is not None and p["task"] is None)

# 客套:无动作
p = r(rr.PLEASANTRY)
check("客套 无 task", p["task"] is None)
check("客套 不可执行(不进清单)", rr.is_actionable(p) is False)

# is_actionable
check("interested_later 可执行", rr.is_actionable(r(rr.INTERESTED_LATER)) is True)
check("list_relevant 可执行", rr.is_actionable(r(rr.LIST_RELEVANT)) is True)
check("list_irrelevant 可执行(有 list_quality)", rr.is_actionable(r(rr.LIST_IRRELEVANT)) is True)

# 未知类别报错
try:
    rr.route("bogus")
    check("未知类别应报错", False)
except ValueError:
    check("未知类别应报错", True)

print("\n" + ("❌ 失败: " + str(fails) if fails else "✅ 全绿"))
sys.exit(1 if fails else 0)
