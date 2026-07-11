"""阶段 4 回归：休假迁移 + is_paused 改读日历 + availability 委托 + 轨道暂停不受影响。

stub 掉 core.memory（临时 db）与 config（临时 DATA_DIR），跑真实 calendar/delivery/availability。
跑法：.venv/bin/python tests/test_delivery_rest_migration.py
"""
import sys, types, sqlite3, tempfile, os, json, importlib.util
from datetime import date, timedelta
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

TMP = tempfile.mkdtemp()
DB = os.path.join(TMP, "memory.db")

# stub core 包 + core.memory + config
core_pkg = types.ModuleType("core"); core_pkg.__path__ = []
sys.modules["core"] = core_pkg
fake_mem = types.ModuleType("core.memory")
def _get_conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c
fake_mem._get_conn = _get_conn
sys.modules["core.memory"] = fake_mem; core_pkg.memory = fake_mem
fake_cfg = types.ModuleType("config"); fake_cfg.DATA_DIR = Path(TMP)
sys.modules["config"] = fake_cfg

def _load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    m = importlib.util.module_from_spec(spec); sys.modules[modname] = m
    spec.loader.exec_module(m); return m

cal = _load("core.calendar", JARVIS + "/core/calendar.py"); core_pkg.calendar = cal
deliv = _load("core.delivery", JARVIS + "/core/delivery.py"); core_pkg.delivery = deliv
av = _load("core.availability", JARVIS + "/core/availability.py"); core_pkg.availability = av

STATE = deliv._STATE_PATH
fails = []
def check(n, c): print(("PASS " if c else "FAIL ") + n); (fails.append(n) if not c else None)

today = date.today()
d_in = lambda n: (today + timedelta(days=n)).isoformat()

# ── 1. calendar rest API ──
r = cal.add_rest(d_in(-1), d_in(2), "年假")
check("add_rest ok", r["ok"])
rests = cal.list_rests()
check("list_rests shape", len(rests) == 1 and rests[0]["reason"] == "年假" and rests[0]["confirmed"] is False)
check("active_rest covers today", cal.active_rest(today.isoformat()) is not None)
check("active_rest miss far future", cal.active_rest(d_in(99)) is None)

# ── 2. delivery.is_paused 读日历 ──
paused, reason = deliv.is_paused("report")
check("is_paused True during rest", paused and "休假" in reason)
paused2, _ = deliv.is_paused("report", as_of=d_in(99))
check("is_paused False outside rest", not paused2)

# ── 3. deliver 闸门压制 ──
got = {"n": 0}
res = deliv.deliver("report", "t", "c", channels={"webpush": lambda *_: got.__setitem__("n", got["n"]+1)})
check("deliver suppressed during rest", res["delivered"] is False and got["n"] == 0)

# ── 4. availability 委托日历 ──
check("av.list_rest delegates", len(av.list_rest()) == 1)
rid = cal.list_rests()[0]["id"]
check("av.confirm_rest", av.confirm_rest(rid) and cal.list_rests()[0]["confirmed"] is True)

# ── 5. delivery.add_rest_period / clear 委托 ──
deliv.add_rest_period(d_in(10), d_in(12), "出差")
check("add_rest_period via delivery", len(cal.list_rests()) == 2)
deliv.clear_rest_periods()
check("clear_rest_periods", len(cal.list_rests()) == 0)

# ── 6. 迁移：旧 rest_periods → 日历，幂等 ──
old_state = {"tracks": {}, "rest_periods": [
    {"id": "abc", "start": d_in(-1), "end": d_in(1), "reason": "迁移测试", "confirmed": True},
]}
STATE.write_text(json.dumps(old_state), encoding="utf-8")
moved = deliv.migrate_rest_to_calendar()
check("migrate moved 1", moved == 1)
mr = cal.list_rests()
check("migrated rest in calendar + confirmed", len(mr) == 1 and mr[0]["confirmed"] is True)
check("old rest_periods emptied", json.loads(STATE.read_text())["rest_periods"] == [])
check("migrate idempotent (2nd run=0)", deliv.migrate_rest_to_calendar() == 0)

# ── 7. 轨道暂停（开关）不受休假迁移影响，仍在 delivery 状态 ──
cal.clear_rests()
deliv.pause_track("prospect", until=None, reason="手动停")
p, why = deliv.is_paused("prospect")
check("track pause still works", p and "手动停" in why)
check("other track unaffected", not deliv.is_paused("report")[0])

import shutil; shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
