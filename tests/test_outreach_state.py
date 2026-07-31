"""tests/test_outreach_state.py — 冷开发状态(简化版)单测。跑:python -m tests.test_outreach_state"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospecting import outreach_state as os_  # noqa: E402

TODAY = date(2025, 6, 1)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _c(name, dates, replied=False, title=None):
    return {"name": name, "job_title": title, "sent_dates": dates, "replied": replied}


# ── 未开发:没发过 ─────────────────────────────────────────────
r = os_.account_outreach_state([], today=TODAY)
check(r["state"] == "not_started" and r["view"] == "未开发", f"空=未开发, got {r['state']}")
r = os_.account_outreach_state([_c("A", [])], today=TODAY)
check(r["state"] == "not_started", "有联系人但没发过=未开发")

# ── 开发中:最后一封在 14 天内 ─────────────────────────────────
r = os_.account_outreach_state([_c("A", ["2025-05-25"])], today=TODAY)  # 7 天前
check(r["state"] == "in_progress" and r["view"] == "开发中", f"7天前=开发中, got {r['state']}")
r = os_.account_outreach_state([_c("A", ["2025-05-18"])], today=TODAY)  # 恰好 14 天
check(r["state"] == "in_progress", "恰好 14 天=开发中(含边界)")

# ── 待处理:最后一封超过 14 天、没回 ───────────────────────────
r = os_.account_outreach_state([_c("A", ["2025-05-01", "2025-05-05"])], today=TODAY)  # 27 天前
check(r["state"] == "pending" and r["view"].startswith("待处理"), f">14天=待处理, got {r['state']}")
check(r["last_outreach"] == "2025-05-05" and r["days_since_last"] == 27, "最后日期/天数")

# ── 多联系人取【最晚】那封判定 ────────────────────────────────
r = os_.account_outreach_state([_c("A", ["2025-04-01"]), _c("B", ["2025-05-28"])], today=TODAY)
check(r["state"] == "in_progress", "有一个联系人近期发过=开发中(取最晚)")

# ── 已回复优先(即便最后一封很久)────────────────────────────
r = os_.account_outreach_state([_c("A", ["2025-01-01"], replied=True)], today=TODAY)
check(r["state"] == "replied" and r["replied_contacts"] == ["A"], "有回信=已回复(优先)")

# ── 明细 + 岗位 + 未试联系人(不参与判定,仅参考)──────────────
contacts = [_c("Annette", ["2025-05-02", "2025-05-04"], title="Einkauf"),
            _c("Sascha", ["2025-05-02"])]
roster = [{"name": "Annette"}, {"name": "Sascha"}, {"name": "New CEO"}]
r = os_.account_outreach_state(contacts, all_contacts=roster, today=TODAY)
check(r["untouched_count"] == 1 and r["untouched_contacts"][0]["name"] == "New CEO", "未试=New CEO")
annette = next(t for t in r["tried_contacts"] if t["name"] == "Annette")
check(annette["n_sent"] == 2 and annette["job_title"] == "Einkauf" and annette["last"] == "2025-05-04",
      "联系人明细:发数/岗位/最后日期")

# ── 名字大小写/空格容错(未试计算)───────────────────────────
r = os_.account_outreach_state([_c("acme corp", ["2025-05-20"])],
                               all_contacts=[{"name": "ACME  Corp"}, {"name": "Other"}], today=TODAY)
check(r["untouched_count"] == 1 and r["untouched_contacts"][0]["name"] == "Other", "归一后 acme 算已试")

# ── core 维护到点 ──────────────────────────────────────────────
check(os_.core_maintenance_due(None, today=TODAY) is True, "从没 touch=到点")
check(os_.core_maintenance_due("2025-05-25", today=TODAY) is False, "7 天前=未到点")
check(os_.core_maintenance_due("2025-03-01", today=TODAY) is True, "3 个月前=到点")

print("✅ test_outreach_state 全部通过")
