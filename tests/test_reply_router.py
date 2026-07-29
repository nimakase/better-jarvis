"""回复路由器单测(8 类回复 → 提议)。纯逻辑。

    .venv/bin/python tests/test_reply_router.py
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


# 相关 list:不 Note,交给你建 deal
p = r(rr.LIST_RELEVANT)
check("相关list 不 Note", p["make_note"] is False)
check("相关list 是你的动作", p["your_action"] is not None)

# 不相关 list:短 Note + 标相关性否
p = r(rr.LIST_IRRELEVANT)
check("不相关list Note", p["make_note"] and p["relevance_no"])

# 暂无货(无时间→默认 90;带时间→用时间)+ warm + 唤醒 Task
p = r(rr.INTERESTED_NO_STOCK)
check("暂无货 默认唤醒 90 天", p["task"]["wake_days"] == rr.DEFAULT_WAKE_DAYS)
check("暂无货 priority=warm", p["priority"] == "warm")
p2 = r(rr.INTERESTED_NO_STOCK, stock_wake_days=120)
check("暂无货 带时间用时间(120)", p2["task"]["wake_days"] == 120)

# 卡 NDA:短提醒 + warm + 需你推进
p = r(rr.STUCK_NDA)
check("卡NDA 短提醒 21 天", p["task"]["wake_days"] == rr.STUCK_REMINDER_DAYS)
check("卡NDA 需你推进", p["your_action"] is not None)

# 已有渠道:记对手 + 长唤醒 + cold
p = r(rr.HAS_CHANNEL)
check("已有渠道 长唤醒 180", p["task"]["wake_days"] == rr.HAS_CHANNEL_WAKE_DAYS)
check("已有渠道 priority=cold", p["priority"] == "cold")

# 明确不做:Opt-Out + cold + 释放动作
p = r(rr.EXPLICIT_NO)
check("明确不做 设 Opt-Out", p["sequence_opt_out"] is True)
check("明确不做 priority=cold", p["priority"] == "cold")
check("明确不做 提示释放", p["your_action"] is not None)

# 转介:Note + 对外触达是你的动作(无自动 Task)
p = r(rr.REFERRAL)
check("转介 Note", p["make_note"])
check("转介 对外触达你来", p["your_action"] is not None and p["task"] is None)

# 客套:无动作(不建 Task),不进清单
p = r(rr.PLEASANTRY)
check("客套 无 task", p["task"] is None)
check("客套 不可执行(不进清单)", rr.is_actionable(p) is False)
# 有动作的类别 is_actionable=True
check("暂无货 可执行", rr.is_actionable(r(rr.INTERESTED_NO_STOCK)) is True)
check("list_relevant 可执行(有你手动动作)", rr.is_actionable(r(rr.LIST_RELEVANT)) is True)

# 未知类别报错
try:
    rr.route("bogus")
    check("未知类别应报错", False)
except ValueError:
    check("未知类别应报错", True)

print("\n" + ("❌ 失败: " + str(fails) if fails else "✅ 全绿"))
sys.exit(1 if fails else 0)
