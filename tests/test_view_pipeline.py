"""tests/test_view_pipeline.py — view 管理管线纯逻辑单测。
跑:python -m tests.test_view_pipeline"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["JARVIS_CUSTOMER_LOOP_DIR"] = tempfile.mkdtemp()  # 隔离 store 目录

from prospecting import breeze_outreach as bz       # noqa: E402
from prospecting import view_writer                  # noqa: E402
from prospecting import outreach_store               # noqa: E402
from prospecting import view_manager                 # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ── parse_response:从夹散文的文本里挑 JSON ────────────────────
breeze_text = """Filtering Ned's sends
I'm listing all outbound emails on this account.

{"account":"Insta Elektro","found":true,"outreach_emails":[
 {"date":"2025-04-08","subject":"Can We Buy Your Excess","to_contact":"Annette Giesbrecht","job_title":"Purchasing"},
 {"date":"2025-04-10","subject":"Can We Buy Your Excess","to_contact":"Annette Giesbrecht","job_title":"Purchasing"},
 {"date":"2025-04-14","subject":"Can We Buy Your Excess","to_contact":"Annette Giesbrecht","job_title":"Purchasing"}
],"replied_contacts":["Sascha Friedrich"]}

Let me know if you need more."""
p = bz.parse_response(breeze_text)
check(p["account"] == "Insta Elektro", "解析出 account")
check(p["found"] is True and len(p["outreach_emails"]) == 3, "解析出 3 封")
check(p["replied_contacts"] == ["Sascha Friedrich"], "解析出 replied_contacts")

# 无 JSON → found:false
check(bz.parse_response("sorry, I couldn't find anything")["found"] is False, "无 JSON = found:false")
check(bz.parse_response("")["found"] is False, "空文本 = found:false")

# 字符串里带花括号不该干扰
tricky = '{"account":"X","outreach_emails":[{"subject":"a {weird} subj","to_contact":"Bob","date":"2025-01-01"}],"replied_contacts":[]}'
pt = bz.parse_response(tricky)
check(len(pt["outreach_emails"]) == 1 and pt["outreach_emails"][0]["to_contact"] == "Bob", "花括号在字符串里不干扰")

# prompt 回显模板(date=YYYY-MM-DD)要跳过,取后面的真答案
echo_then_real = (
    '示例 {"account":"X","found":true,"outreach_emails":[{"date":"YYYY-MM-DD","subject":"","to_contact":"","job_title":""}],"replied_contacts":[]} '
    'working… {"account":"X","found":true,"outreach_emails":[{"date":"2025-05-01","subject":"S","to_contact":"Bob","job_title":"CEO"}],"replied_contacts":["Bob"]}')
pe = bz.parse_response(echo_then_real)
check(len(pe["outreach_emails"]) == 1 and pe["outreach_emails"][0]["to_contact"] == "Bob", "跳过回显取真答案")
check(pe["replied_contacts"] == ["Bob"], "真答案的 replied")
only_echo = '{"account":"X","found":true,"outreach_emails":[{"date":"YYYY-MM-DD","subject":"","to_contact":"","job_title":""}],"replied_contacts":[]}'
check(bz.parse_response(only_echo)["found"] is False, "只有回显 = found:false")

# Breeze 答案用弯引号(smart quotes)→ 归一后要能解析
smart = ('Thinking complete '
         '{\u201caccount\u201d:\u201cInsta\u201d,\u201cfound\u201d:true,'
         '\u201coutreach_emails\u201d:[{\u201cdate\u201d:\u201c2025-04-08\u201d,\u201csubject\u201d:\u201cS\u201d,'
         '\u201cto_contact\u201d:\u201cAnnette\u201d,\u201cjob_title\u201d:\u201cPurchasing\u201d}],'
         '\u201creplied_contacts\u201d:[]}')
ps = bz.parse_response(smart)
check(ps["found"] is True and len(ps["outreach_emails"]) == 1, "弯引号答案归一后可解析")
check(ps["outreach_emails"][0]["to_contact"] == "Annette", "弯引号答案字段正确")

# ── emails_to_contacts:按联系人聚合 + 岗位 + 回复 ──────────────
contacts = bz.emails_to_contacts(p)
check(len(contacts) == 1, "3 封同联系人 → 聚成 1 个")
a = contacts[0]
check(a["name"] == "Annette Giesbrecht" and len(a["sent_dates"]) == 3, "聚合日期")
check(a["job_title"] == "Purchasing", "岗位保留")
check(a["replied"] is False, "Annette 没回")
# 回复标记
p2 = dict(p); p2["replied_contacts"] = ["Annette Giesbrecht"]
check(bz.emails_to_contacts(p2)[0]["replied"] is True, "replied_contacts 命中 → replied")

# ── view_writer.format_value_list:去空去重保序 ────────────────
out = view_writer.format_value_list(["A Corp", " A Corp ", "", "B Ltd", "a corp"])
check(out == "A Corp\nB Ltd", f"去重去空保序, got {out!r}")

# ── outreach_store:存/取/按 view 汇总 ─────────────────────────
outreach_store.upsert_account_state("Acme Inc", {"state": "pending", "view": "待处理·换人或放弃"})
outreach_store.upsert_account_state("Beta LLC", {"state": "not_started", "view": "未开发"})
outreach_store.upsert_account_state("Gamma Co", {"state": "pending", "view": "待处理·换人或放弃"})
check(outreach_store.get_account_state("acme inc")["view"] == "待处理·换人或放弃", "按归一名取回")
by = outreach_store.accounts_by_view()
check(sorted(by["待处理·换人或放弃"]) == ["Acme Inc", "Gamma Co"], f"按 view 汇总, got {by.get('待处理·换人或放弃')}")
check(by["未开发"] == ["Beta LLC"], "未开发段")

# ── view_manager.compute_segments(假 breeze_ask + roster;简化状态)──
from datetime import date as _date          # noqa: E402
TODAY = _date(2025, 6, 1)

def fake_ask(acct):
    data = {
        "Acct待处理": [   # 最后一封 >14 天、没回
            {"name": "c1", "sent_dates": ["2025-05-01"], "replied": False},
            {"name": "c2", "sent_dates": ["2025-05-03"], "replied": False},
        ],
        "Acct开发中": [{"name": "c1", "sent_dates": ["2025-05-28"], "replied": False}],  # 近期
        "Acct未开发": [],
        "Acct回复": [{"name": "c1", "sent_dates": ["2025-05-01"], "replied": True}],
    }
    return data.get(acct, [])

def fake_roster(acct):
    return [{"name": "c1"}, {"name": "c2"}, {"name": "c3"}]  # c3 没试过

segs = view_manager.compute_segments(["Acct待处理", "Acct开发中", "Acct未开发", "Acct回复"],
                                     fake_ask, roster_of=fake_roster, persist=True, today=TODAY)
check("Acct待处理" in segs["待处理·换人或放弃"], f"待处理归位, got {segs}")
check("Acct开发中" in segs["开发中"], "开发中归位")
check("Acct未开发" in segs["未开发"], "未开发归位")
check("Acct回复" in segs["已回复·待跟进"], "回复归位")
# 持久化 + 未试联系人算对
st = outreach_store.get_account_state("Acct待处理")
check(st["untouched_count"] == 1 and st["untouched_contacts"][0]["name"] == "c3", "未试 c3")

# ── view_manager.apply_segments(假 view_write + url 映射)───────
written = {}
def fake_write(url, names, apply):
    written[url] = (sorted(names), apply)
    return {"ok": True, "count": len(names), "applied": apply}

url_map = {"待处理·换人或放弃": "http://view/pending", "未开发": "http://view/new"}  # 没配"已回复"
res = view_manager.apply_segments(segs, lambda v: url_map.get(v), fake_write, apply=True)
check(res["待处理·换人或放弃"]["ok"] and written["http://view/pending"][1] is True, "待处理写入")
check(res["已回复·待跟进"]["ok"] is False and "未配置" in res["已回复·待跟进"]["reason"], "未配置 view 跳过并说明")

# ── view_manager.split_accounts:按 deal 分 prospecting/core ────
recs = [
    {"account_name": "CoreCo", "num_associated_deals": 2, "num_open_deals": 0, "last_activity_date": "2025-03-01"},
    {"account_name": "ProspectCo", "num_associated_deals": 0, "num_open_deals": 0, "last_engagement_date": "2025-05-20"},
    {"account_name": "", "num_associated_deals": 0},   # 无名 → 跳过
]
pros, core = view_manager.split_accounts(recs)
check(pros == ["ProspectCo"], f"prospecting 分对, got {pros}")
check(len(core) == 1 and core[0]["name"] == "CoreCo" and core[0]["last_activity"] == "2025-03-01",
      f"core 分对+带 last_activity, got {core}")

# ── view_manager.core_maintenance_segment:>2月没 touch ─────────
core3 = [{"name": "Old", "last_activity": "2025-03-01"},     # 3 月前 → 到点
         {"name": "Fresh", "last_activity": "2025-05-25"},    # 7 天前 → 未到
         {"name": "Never", "last_activity": None}]            # 从没 → 到点
due = view_manager.core_maintenance_segment(core3, today=TODAY)
check(sorted(due) == ["Never", "Old"], f"维护到点分对, got {due}")

print("✅ test_view_pipeline 全部通过")
