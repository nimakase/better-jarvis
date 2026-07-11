"""派生 provider 测试：纯日期助手 + 注册 + agenda 聚合（全用 stub，不碰真实数据）。

跑法（仓库根目录，已装 python-dateutil）：
    .venv/bin/python tests/test_calendar_providers.py
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
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c
fake_mem._get_conn = _get_conn
sys.modules["core.memory"] = fake_mem

def _load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    m = importlib.util.module_from_spec(spec); sys.modules[modname] = m
    spec.loader.exec_module(m); return m

cal = _load("core.calendar", JARVIS + "/core/calendar.py")

# stub 各业务来源
conn_pkg = types.ModuleType("connectors"); conn_pkg.__path__ = []
sys.modules["connectors"] = conn_pkg
fv = types.ModuleType("connectors.vault")
fv.list_summary = lambda: [{"alias": "招行卡", "type_label": "银行卡", "expires_at": "2026-06-20"}]
fv.list_documents_summary = lambda: [{"alias": "重疾险", "type_label": "保单", "expires_at": "2026-06-25"}]
sys.modules["connectors.vault"] = fv
fs = types.ModuleType("core.scheduler")
fs.list_schedules = lambda: [{"name": "prospect_daily", "description": "潜客日报", "cron": "0 6 * * *", "next_run": "2026-06-18 06:00:00+08:00"}]
sys.modules["core.scheduler"] = fs
fp = types.ModuleType("core.profile")
fp.list_facts = lambda: [{"id": 1, "text": "老婆生日 6月15日"}, {"id": 2, "text": "车险到期 2026-06-22 记得续"}]
sys.modules["core.profile"] = fp

prov = _load("connectors.calendar_providers", JARVIS + "/connectors/calendar_providers.py")

fails = []
def check(n, c): print(("PASS " if c else "FAIL ") + n); (fails.append(n) if not c else None)

ws, we = datetime(2026, 6, 15), datetime(2026, 6, 30, 23, 59, 59)
check("date_in_window hit", len(prov.date_in_window("2026-06-20", ws, we)) == 1)
check("date_in_window miss", len(prov.date_in_window("2026-07-20", ws, we)) == 0)
check("date_in_window bad input", len(prov.date_in_window("xx", ws, we)) == 0)
check("yearly hit", len(prov.yearly_in_window(6, 15, ws, we)) == 1)
check("yearly miss", len(prov.yearly_in_window(1, 1, ws, we)) == 0)
check("extract ISO", any(h["kind"] == "once" for h in prov.extract_dates_from_text("到期 2026-06-22")))
check("extract CN md", any(h["kind"] == "yearly" and h["m"] == 6 and h["d"] == 15 for h in prov.extract_dates_from_text("生日 6月15日")))
check("4 sources registered", {"credentials_expiry", "documents_expiry", "schedules_next_run", "profile_key_dates"}.issubset(set(cal.registered_sources())))

ag = cal.agenda("2026-06-15", "2026-06-30"); titles = [e["title"] for e in ag]
check("cred expiry merged", any("招行卡" in t for t in titles))
check("doc expiry merged", any("重疾险" in t for t in titles))
check("schedule merged", any("潜客日报" in t for t in titles))
check("birthday merged", any("老婆生日" in t for t in titles))
check("car insurance from profile", any("车险" in t for t in titles))
check("all derived flagged", all(e.get("derived") for e in ag))
check("agenda sorted", [e["start"] for e in ag] == sorted(e["start"] for e in ag))

os.unlink(DB)
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
