"""内置日历核心测试：rrule 子集 + CRUD + agenda 区间展开。

用临时 sqlite stub 掉 core.memory._get_conn，因此不碰真实 memory.db、不需要 keyring。
跑法（仓库根目录，已装 python-dateutil）：
    .venv/bin/python tests/test_calendar.py
"""
import sys, types, sqlite3, tempfile, os, importlib.util
from datetime import datetime
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
core_pkg = types.ModuleType("core"); core_pkg.__path__ = []
sys.modules["core"] = core_pkg
fake_mem = types.ModuleType("core.memory")
def _get_conn():
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row; return conn
fake_mem._get_conn = _get_conn
sys.modules["core.memory"] = fake_mem

spec = importlib.util.spec_from_file_location("core.calendar", JARVIS + "/core/calendar.py")
cal = importlib.util.module_from_spec(spec); sys.modules["core.calendar"] = cal
spec.loader.exec_module(cal)

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# 1. RRULE 子集校验
check("validate weekly TU ok", cal.validate_rrule("FREQ=WEEKLY;BYDAY=TU")[0])
check("validate yearly ok", cal.validate_rrule("FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=15")[0])
check("validate -1FR ok", cal.validate_rrule("FREQ=MONTHLY;BYDAY=-1FR")[0])
check("reject BYSETPOS", not cal.validate_rrule("FREQ=MONTHLY;BYSETPOS=-1")[0])
check("reject HOURLY freq", not cal.validate_rrule("FREQ=HOURLY")[0])
check("reject COUNT+UNTIL", not cal.validate_rrule("FREQ=DAILY;COUNT=3;UNTIL=20261231")[0])
check("reject no FREQ", not cal.validate_rrule("INTERVAL=2")[0])

# 2. CRUD
r = cal.create_event("单次会议", "2026-06-25T10:00")
check("create single ok", r["ok"]); eid = r["event"]["id"]
check("get_event", cal.get_event(eid)["title"] == "单次会议")
check("update ok", cal.update_event(eid, title="改名会议")["ok"] and cal.get_event(eid)["title"] == "改名会议")
check("reject bad rrule", not cal.create_event("x", "2026-06-25", rrule="FREQ=HOURLY")["ok"])

# 3. 区间展开：每周二，6月窗口 → 2,9,16,23,30 = 5 次
cal.create_event("每周二例会", "2026-06-02T09:00", rrule="FREQ=WEEKLY;BYDAY=TU")
tue = [e for e in cal.agenda("2026-06-01", "2026-06-30") if e["title"] == "每周二例会"]
check("weekly TU -> 5 in June", len(tue) == 5)
check("all Tuesdays", all(datetime.fromisoformat(e["start"]).weekday() == 1 for e in tue))

# 4. 无限重复 + 窄窗口（证明走 between，没全量展开）
cal.create_event("每日打卡", "2026-01-01T08:00", rrule="FREQ=DAILY")
check("daily 3-day window -> 3", len([e for e in cal.agenda("2026-06-10", "2026-06-12") if e["title"] == "每日打卡"]) == 3)

# 5. 每月最后一个周五
cal.create_event("月末对账", "2026-01-30T17:00", rrule="FREQ=MONTHLY;BYDAY=-1FR")
last = [e for e in cal.agenda("2026-06-01", "2026-08-31") if e["title"] == "月末对账"]
check("last-Friday -> 3", len(last) == 3 and all(datetime.fromisoformat(e["start"]).weekday() == 4 for e in last))

# 6. COUNT 截断
cal.create_event("限次", "2026-06-01T10:00", rrule="FREQ=DAILY;COUNT=3")
check("COUNT=3 stops at 3", len([e for e in cal.agenda("2026-06-01", "2026-12-31") if e["title"] == "限次"]) == 3)

# 7. 排序 + delete + build_block
s = [e["start"] for e in cal.agenda("2026-06-01", "2026-06-30")]
check("agenda sorted", s == sorted(s))
check("delete ok", cal.delete_event(eid)["ok"])
check("delete missing -> not ok", not cal.delete_event("nope")["ok"])
check("build_block non-empty", "近期日程" in cal.build_block(days=3650))

os.unlink(DB)
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
