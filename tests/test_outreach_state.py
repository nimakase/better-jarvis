"""tests/test_outreach_state.py — 冷开发状态纯逻辑单测。跑:python -m tests.test_outreach_state"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospecting import outreach_state as os_  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _c(name, n, replied=False, title=None):
    """造一个联系人:发了 n 封(日期随便),是否回复,岗位。"""
    return {"name": name, "job_title": title,
            "sent_dates": [f"2025-04-{8+i:02d}" for i in range(n)], "replied": replied}


# ── contact_state ──────────────────────────────────────────────
check(os_.contact_state([], False) == "none", "0 封 = none")
check(os_.contact_state(["2025-04-08"], False) == "in_progress", "1 封 = in_progress")
check(os_.contact_state(["a", "b"], False) == "in_progress", "2 封 = in_progress")
check(os_.contact_state(["a", "b", "c"], False) == "exhausted", "3 封无回复 = exhausted")
check(os_.contact_state(["a", "b", "c"], True) == "replied", "回复优先于 exhausted")
check(os_.contact_state(["a"], True) == "replied", "回复优先于 in_progress")

# ── account: 未开发 ────────────────────────────────────────────
r = os_.account_outreach_state([])
check(r["state"] == "not_started" and r["view"] == "未开发", f"空 = 未开发, got {r['state']}")

# ── account: 开发中(有联系人在跑)────────────────────────────
r = os_.account_outreach_state([_c("A", 2)])
check(r["state"] == "in_progress", f"1 个联系人发了 2 封 = 开发中, got {r['state']}")

# ── account: 1 联系人到顶 ──────────────────────────────────────
r = os_.account_outreach_state([_c("A", 3)])
check(r["state"] == "one_done", f"1 个联系人跑满 = one_done, got {r['state']}")
check(r["exhausted_count"] == 1, "exhausted_count=1")

# ── account: 到顶(2 联系人跑满无回复)────────────────────────
r = os_.account_outreach_state([_c("A", 3), _c("B", 3)])
check(r["state"] == "at_limit" and r["view"] == "到顶", f"2 联系人跑满 = 到顶, got {r['state']}")
check(r["exhausted_count"] == 2, "exhausted_count=2")

# ── account: 回复优先(有人回信,即便别人到顶)────────────────
r = os_.account_outreach_state([_c("A", 3), _c("B", 3, replied=True)])
check(r["state"] == "replied", f"有人回信 = replied(优先), got {r['state']}")
check(r["replied_contacts"] == ["B"], "回信联系人 = B")

# ── account: 开发中优先于 one_done(一个到顶、一个还在跑)──────
r = os_.account_outreach_state([_c("A", 3), _c("B", 1)])
check(r["state"] == "in_progress", f"还有在跑的 = 开发中, got {r['state']}")

# ── Insta 实例 + 未试联系人 + 岗位 ────────────────────────────
contacts = [
    _c("Annette Giesbrecht", 3, title="Purchasing"),
    _c("Sascha Friedrich", 3),
    _c("Alfred Vrieling", 3),
    _c("Christof Schönfeld", 3),
]
roster = [{"name": n, "job_title": None} for n in
          ["Annette Giesbrecht", "Sascha Friedrich", "Alfred Vrieling",
           "Christof Schönfeld", "New CEO", "New Buyer"]]
r = os_.account_outreach_state(contacts, all_contacts=roster)
check(r["state"] == "at_limit", f"Insta = 到顶, got {r['state']}")
check(r["untouched_count"] == 2, f"还剩 2 个没试过, got {r['untouched_count']}")
check({c["name"] for c in r["untouched_contacts"]} == {"New CEO", "New Buyer"}, "未试 = New CEO/New Buyer")
check(any(t["name"] == "Annette Giesbrecht" and t["job_title"] == "Purchasing"
          for t in r["tried_contacts"]), "岗位带上了")

# ── 名字大小写/空格容错 ────────────────────────────────────────
r = os_.account_outreach_state([_c("acme corp", 3)],
                               all_contacts=[{"name": "ACME  Corp"}, {"name": "Other"}])
check(r["untouched_count"] == 1 and r["untouched_contacts"][0]["name"] == "Other",
      "大小写/空格归一后 acme 算已试")

# ── core 维护到点 ──────────────────────────────────────────────
today = date(2025, 7, 1)
check(os_.core_maintenance_due(None, today=today) is True, "从没 touch = 到点")
check(os_.core_maintenance_due("2025-06-25", today=today) is False, "6 天前 touch = 未到点")
check(os_.core_maintenance_due("2025-04-01", today=today) is True, "3 个月前 = 到点")

print("✅ test_outreach_state 全部通过")
