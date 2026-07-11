"""阶段 5：提醒巡检逻辑测试（before/at、水位去重、补发窗口、重复合并、文案模板）。

stub 临时 sqlite，纯逻辑，不起 scheduler、不发 webpush。
跑法：.venv/bin/python tests/test_calendar_reminders.py
"""
import sys, types, sqlite3, tempfile, os, importlib.util
from datetime import datetime, timedelta
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
core_pkg = types.ModuleType("core"); core_pkg.__path__ = []
sys.modules["core"] = core_pkg
fake_mem = types.ModuleType("core.memory")
def _get_conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c
fake_mem._get_conn = _get_conn
sys.modules["core.memory"] = fake_mem
spec = importlib.util.spec_from_file_location("core.calendar", JARVIS + "/core/calendar.py")
cal = importlib.util.module_from_spec(spec); sys.modules["core.calendar"] = cal
spec.loader.exec_module(cal)

fails = []
def check(n, c): print(("PASS " if c else "FAIL ") + n); (fails.append(n) if not c else None)

NOW = datetime(2026, 6, 23, 10, 0)   # 固定 now 便于断言

# 1. before：会议 10:10，提前 15 分 → 触发 09:55 <= now，应推送
r = cal.create_event("客户电话", (NOW + timedelta(minutes=10)).isoformat(),
                     notify={"type": "before", "minutes": 15})
due = cal.collect_due_reminders(now=NOW)
check("before due fired", any(d["title"] == "客户电话" for d in due))
# 2. 水位去重：同一 now 再扫一次，不再出现
due2 = cal.collect_due_reminders(now=NOW)
check("watermark dedup", not any(d["title"] == "客户电话" for d in due2))

# 3. 未来触发不发：会议明天，提前 15 分 → 触发在未来
cal.create_event("明日会", (NOW + timedelta(days=1)).isoformat(),
                 notify={"type": "before", "minutes": 15})
check("future not due", not any(d["title"] == "明日会" for d in cal.collect_due_reminders(now=NOW)))

# 4. at + 全天：今天全天事件，notify at 09:00，now=10:00 → 触发 09:00<=now，应推送
cal.create_event("保单到期", NOW.date().isoformat(), all_day=True, kind="expiry",
                 notify={"type": "at", "time": "09:00"})
due_at = cal.collect_due_reminders(now=NOW)
check("at-time all-day fired", any(d["title"] == "保单到期" for d in due_at))

# 5. 补发窗口：触发在 5 小时前（grace=2h）→ 不推送，但水位前进（标记）
cal.create_event("早就过了", (NOW - timedelta(hours=5)).isoformat(),
                 notify={"type": "before", "minutes": 0})
out = cal.collect_due_reminders(now=NOW, grace_hours=2)
check("beyond grace not pushed", not any(d["title"] == "早就过了" for d in out))
# 标记后再无补发
check("beyond grace marked (no re-fire)",
      not any(d["title"] == "早就过了" for d in cal.collect_due_reminders(now=NOW + timedelta(minutes=1), grace_hours=2)))

# 6. 重复事件合并：每天 08:00，提前 0 分；停机多日后单次 now → 只出最近一次
cal.create_event("每日站会", "2026-06-01T08:00", rrule="FREQ=DAILY",
                 notify={"type": "before", "minutes": 0})
day_now = datetime(2026, 6, 23, 8, 30)
dd = [d for d in cal.collect_due_reminders(now=day_now) if d["title"] == "每日站会"]
check("recurring coalesced to 1", len(dd) == 1)
check("recurring picks today occ", dd[0]["occ_start"].startswith("2026-06-23"))

# 7. 无 notify 的事件不进巡检
cal.create_event("无提醒事件", NOW.isoformat())
check("no-notify ignored", not any(d["title"] == "无提醒事件" for d in cal.collect_due_reminders(now=NOW + timedelta(hours=1))))

# 8. 文案模板（零 AI）
t, c = cal.format_reminder({"title": "客户电话", "occ_start": "2026-06-23T10:10", "kind": "event", "all_day": False})
check("format title", t == "提醒：客户电话")
check("format content", "即将开始" in c and "2026-06-23 10:10" in c)
t2, c2 = cal.format_reminder({"title": "保单", "occ_start": "2026-06-23T09:00", "kind": "expiry", "all_day": True})
check("format expiry verb", "即将到期" in c2)

os.unlink(DB)
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
