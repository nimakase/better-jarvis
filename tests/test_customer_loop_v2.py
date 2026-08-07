"""tests/test_customer_loop_v2.py — v2 纯逻辑:Core 分层 + Breeze 消歧 prompt + reader 新列。
跑:python -m tests.test_customer_loop_v2"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospecting import account_grading as ag      # noqa: E402
from prospecting import breeze_outreach as bz       # noqa: E402
from prospecting import account_reader as ar        # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ── Core 分层 core_tier ────────────────────────────────────────
check(ag.core_tier(3) == "T0", "3 won = T0")
check(ag.core_tier(5) == "T0", "5 won = T0")
check(ag.core_tier(1) == "T1" and ag.core_tier(2) == "T1", "1~2 won = T1")
check(ag.core_tier(0, open_deals=1) == "active", "0 won 有进行中 = active")
check(ag.core_tier(0, 0, list_quality="good") == "T2", "0 won lost 料好 = T2")
check(ag.core_tier(0, 0, list_quality="weak") == "not_core", "料弱 = not_core(L5)")
check(ag.core_tier(0, 0, list_quality="junk") == "not_core", "料差 = not_core(L6)")
check(ag.core_tier(0, 0) == "review", "0 won 无进行中 未判 list = review")
check(ag.core_tier(None, None) == "review", "None 容错 = review")

# ── Breeze build_prompt:消歧 + 单行 ────────────────────────────
p = bz.build_prompt("Insta Elektro")
check("\n" not in p, "prompt 必须单行(无换行)")
check('exactly "Insta Elektro"' in p, "含精确名约束")
check("do not ask me any question" in p, "含禁反问句")
check("found:false" in p, "无精确匹配则 found:false")
check("Insta Elektro" in p and "outreach_emails" in p, "含账户名 + 抽取要求")
check(bz.looks_like_disambiguation(p) is False, "自身 outreach prompt 不被误判为消歧(Photron 回归)")
pw = bz.build_prompt("Acme", website="acme.com")
check("(website: acme.com)" in pw, "有 website 则注入")
check("(website:" not in p, "无 website 不注入")
check("do not judge" in p, "outreach prompt 只报事实、不判真假(OOO 判断归 reply_classify)")

# ── looks_like_disambiguation:识别复核问句 ─────────────────────
check(bz.looks_like_disambiguation("Did you mean Insta Elektro GmbH or Insta Elektro AG?") is True, "did you mean = 消歧")
check(bz.looks_like_disambiguation("I found several companies. Which one did you mean?") is True, "several/which = 消歧")
check(bz.looks_like_disambiguation("Could you clarify which company?") is True, "could you clarify = 消歧")
real = '{"account":"X","found":true,"outreach_emails":[{"date":"2025-05-01","subject":"S","to_contact":"Bob","job_title":""}],"replied_contacts":[]}'
check(bz.looks_like_disambiguation("Which one? " + real) is False, "已有真答案 → 不算消歧")
check(bz.looks_like_disambiguation("Here are the outreach emails I found.") is False, "普通无问句 → 非消歧")
check(bz.looks_like_disambiguation("") is False, "空 → 非消歧")

# ── account_reader:新列匹配 + 解析 ────────────────────────────
colmap = {
    "Account name": "0", "Account type": "1", "Priority": "2",
    "Number of associated deals": "3", "Number of open deals": "4",
    "Number of closed won deals": "5", "Last activity date": "6",
    "Last engagement date": "7", "Decay stage": "8", "Company domain name": "9",
}
cols = ar.match_columns(colmap)
check(ar.missing_required(cols) == [], f"必填列齐, got missing {ar.missing_required(cols)}")
for f in ("num_won_deals", "decay_stage", "company_domain"):
    check(f in cols, f"新列 {f} 匹配到")

raw = {"account_name": "Acme", "existing_type": "Core", "existing_priority": "--",
       "num_associated_deals": "3", "num_open_deals": "0", "num_won_deals": "2",
       "last_activity_date": "2025-05-01", "last_engagement_date": "--",
       "decay_stage": "Final warning", "company_domain": "acme.com", "create_date": "--"}
parsed = ar._parse_raw(raw)
check(parsed["num_won_deals"] == 2, "won 解析")
check(parsed["decay_stage"] == "Final warning", "decay_stage 解析")
check(parsed["company_domain"] == "acme.com", "domain 解析")
check(ar._parse_raw({"decay_stage": "--", "company_domain": ""})["decay_stage"] is None, "-- / 空 → None")

# ── Breeze deal 计数:prompt 消歧 + 解析 + 喂 core_tier ─────────
dp = bz.build_deal_prompt("Insta Elektro")
check("\n" not in dp, "deal prompt 单行")
check('exactly "Insta Elektro"' in dp, "deal prompt 含精确名守卫")
check(bz.looks_like_disambiguation(dp) is False, "deal prompt 自身不被误判(Photron 回归)")
check('"won":' in dp and '"lost":' in dp and '"open":' in dp, "deal prompt 含 won/lost/open")
check("(website: x.com)" in bz.build_deal_prompt("Y", website="x.com"), "deal prompt 注入 domain")
check(bz.parse_deal_summary(dp)["found"] is False, "deal prompt 自身回显(<>占位非JSON)→ found:false")

ds = bz.parse_deal_summary('noise {"account":"Insta","found":true,"won":3,"lost":1,"open":0} tail')
check(ds["won"] == 3 and ds["lost"] == 1 and ds["open"] == 0 and ds["found"] is True, "deal 解析 won/lost/open")
check(bz.parse_deal_summary("no json here")["found"] is False, "无 JSON → found:false")
check(bz._has_deal_json('x {"won":2} y') is True and bz._has_deal_json("nope") is False, "_has_deal_json")
check(ag.core_tier(bz.parse_deal_summary('{"won":3}')["won"]) == "T0", "won=3 → T0")
check(ag.core_tier(bz.parse_deal_summary('{"won":1}')["won"]) == "T1", "won=1 → T1")

print("✅ test_customer_loop_v2 全部通过")
