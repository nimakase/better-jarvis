"""tests/test_outreach_state.py — 冷开发状态(v2:轮次 + 双时钟)单测。跑:python -m tests.test_outreach_state"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospecting import outreach_state as os_  # noqa: E402

TODAY = date(2025, 6, 1)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _c(name, dates, replied=False, title=None):
    return {"name": name, "job_title": title, "sent_dates": dates, "replied": replied}


# 三轮(~2 月一轮)的发送日期:Jan / Mar / May 三簇
THREE_ROUNDS = ["2025-01-06", "2025-01-08", "2025-03-07", "2025-03-09", "2025-05-01", "2025-05-03"]

# ── not_started:没发过 ────────────────────────────────────────
r = os_.account_outreach_state([], today=TODAY)
check(r["state"] == "not_started" and r["view"] == "未开发", f"空=未开发, got {r['state']}")
check(r["rounds_done"] == 0 and r["last_outreach"] is None, "空:0 轮无日期")
r = os_.account_outreach_state([_c("A", [])], today=TODAY)
check(r["state"] == "not_started", "有联系人但没发过=未开发")

# ── sequencing:还在跑、距上封 ≤60 天(在节奏上,别动)──────────
r = os_.account_outreach_state([_c("A", ["2025-05-25"])], today=TODAY)  # 7 天前,1 轮
check(r["state"] == "sequencing" and r["view"] == "开发中", f"近期发过=开发中, got {r['state']}")
check(r["rounds_done"] == 1 and r["next_round_due"] is False, "1 轮 / 不催发")

# ── round_due:还在跑、距上封 >60 天(该发下一轮)────────────────
r = os_.account_outreach_state([_c("A", ["2025-03-20"])], today=TODAY)  # 73 天前,1 轮
check(r["state"] == "round_due" and r["view"] == "开发中", f">60天且轮未满=round_due, got {r['state']}")
check(r["next_round_due"] is True and r["rounds_done"] == 1, "催发标记 + 仍 1 轮")

# ── exhausted:三轮跑完、没回(决策点)─────────────────────────
r = os_.account_outreach_state([_c("A", THREE_ROUNDS)], today=TODAY)
check(r["state"] == "exhausted" and r["view"] == "待处理·换人或放弃", f"三轮完=待处理, got {r['state']}")
check(r["rounds_done"] == 3 and r["last_outreach"] == "2025-05-03" and r["days_since_last"] == 29,
      f"3 轮 / 末封 / 天数, got {r['rounds_done']},{r['last_outreach']},{r['days_since_last']}")

# 三轮但跨多个联系人分摊(账户级聚合日期,仍算 3 轮)
split = [_c("c1", ["2025-01-06", "2025-03-07"]), _c("c2", ["2025-01-08", "2025-03-09", "2025-05-01", "2025-05-03"])]
check(os_.account_outreach_state(split, today=TODAY)["rounds_done"] == 3, "跨联系人聚合仍 3 轮")

# ── inbound 回来:交给 reply_classify 定局(reply_is_real)──────────
# 默认(未判)→ reply_pending(待分类),不直接算已回复
r = os_.account_outreach_state([_c("A", THREE_ROUNDS, replied=True)], today=TODAY)
check(r["state"] == "reply_pending" and r["view"] == "待分类回复" and r["replied_contacts"] == ["A"],
      f"inbound 未判 → reply_pending, got {r['state']}")
# 判成真回复 → replied(压过一切)
r = os_.account_outreach_state([_c("A", THREE_ROUNDS, replied=True)], reply_is_real=True, today=TODAY)
check(r["state"] == "replied" and r["view"] == "已回复·待跟进", "判真 → 已回复")
# 判成非真回复(OOO/none)→ 当没回复,回落轮次(三轮 → exhausted)
r = os_.account_outreach_state([_c("A", THREE_ROUNDS, replied=True)], reply_is_real=False, today=TODAY)
check(r["state"] == "exhausted", f"判假(OOO)→ 回落轮次 exhausted, got {r['state']}")

# ── 公司 auto-sequence 剔除:大空档的系统轮不计进 Ned 轮次 ────────
LATE = date(2025, 12, 1)
with_system = THREE_ROUNDS + ["2025-10-15", "2025-10-17"]  # 距 5/3 达 165 天 >120 → 系统轮
ra = os_.analyze_rounds(os_._dates(with_system))
check(ra["ned_rounds"] == 3 and ra["has_system"] is True, f"系统轮剔除:ned=3 有系统, got {ra}")
check(ra["last_ned"].isoformat() == "2025-05-03", "last_ned 取 Ned 最后一轮,非系统轮")
r = os_.account_outreach_state([_c("A", with_system)], today=LATE)
check(r["state"] == "exhausted" and r["rounds_done"] == 3 and r["has_system_sequence"] is True,
      "含系统轮仍判 3 轮 exhausted + 标记 has_system")

# ── decay_stage 原样透传 ───────────────────────────────────────
r = os_.account_outreach_state([_c("A", ["2025-05-25"])], decay_stage="Final warning", today=TODAY)
check(r["decay_stage"] == "Final warning", "decay_stage 透传")

# ── 联系人明细 + 岗位 + 未试联系人(不参与判定)──────────────────
contacts = [_c("Annette", ["2025-05-02", "2025-05-04"], title="Einkauf"), _c("Sascha", ["2025-05-02"])]
roster = [{"name": "Annette"}, {"name": "Sascha"}, {"name": "New CEO"}]
r = os_.account_outreach_state(contacts, all_contacts=roster, today=TODAY)
check(r["untouched_count"] == 1 and r["untouched_contacts"][0]["name"] == "New CEO", "未试=New CEO")
annette = next(t for t in r["tried_contacts"] if t["name"] == "Annette")
check(annette["n_sent"] == 2 and annette["job_title"] == "Einkauf" and annette["last"] == "2025-05-04",
      "联系人明细:发数/岗位/最后日期")

# 名字大小写/空格容错(未试计算)
r = os_.account_outreach_state([_c("acme corp", ["2025-05-20"])],
                               all_contacts=[{"name": "ACME  Corp"}, {"name": "Other"}], today=TODAY)
check(r["untouched_count"] == 1 and r["untouched_contacts"][0]["name"] == "Other", "归一后 acme 算已试")

# ── core 维护到点:默认 60 + 分层 T0 45 / T1 90 / T2 60 ──────────
check(os_.core_maintenance_due(None, today=TODAY) is True, "从没 touch=到点")
check(os_.core_maintenance_due("2025-05-25", today=TODAY) is False, "默认 60:7 天=未到")
check(os_.core_maintenance_due("2025-03-01", today=TODAY) is True, "默认 60:92 天=到点")
check(os_.core_maintenance_due("2025-04-20", tier="T0", today=TODAY) is False, "T0 45:42 天=未到")
check(os_.core_maintenance_due("2025-04-10", tier="T0", today=TODAY) is True, "T0 45:52 天=到点")
check(os_.core_maintenance_due("2025-03-20", tier="T1", today=TODAY) is False, "T1 90:73 天=未到")
check(os_.core_maintenance_due("2025-02-01", tier="T1", today=TODAY) is True, "T1 90:120 天=到点")
check(os_.core_maintenance_due("2025-04-15", tier="T2", today=TODAY) is False, "T2 60:47 天=未到")
check(os_.core_maintenance_due("2025-03-15", tier="T2", today=TODAY) is True, "T2 60:78 天=到点")

# ── derive_decay_stage:Decay Stage 缺失时从 Last Activity 推导 ──
check(os_.derive_decay_stage(None, today=TODAY) is None, "无活动日期 → None")
check(os_.derive_decay_stage((TODAY - timedelta(days=30)).isoformat(), today=TODAY) is None, "30 天 → 安全 None")
check(os_.derive_decay_stage((TODAY - timedelta(days=150)).isoformat(), today=TODAY) == "in_decay", "150 天 → in_decay")
check(os_.derive_decay_stage((TODAY - timedelta(days=190)).isoformat(), today=TODAY) == "final_warning", "190 天 → final_warning")
check(os_.derive_decay_stage((TODAY - timedelta(days=210)).isoformat(), today=TODAY) == "reassigned", "210 天 → reassigned")

print("✅ test_outreach_state 全部通过")
