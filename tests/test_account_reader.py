"""account_reader 纯解析层单测(列匹配 + 单元格解析,零 DOM)。

    .venv/bin/python tests/test_account_reader.py
"""
import sys
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from prospecting import account_reader as r   # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ── 列匹配:模拟 HubSpot 真实表头(大写、带括号)──────────────
fake_map = {
    "ACCOUNT NAME": "1",
    "ACCOUNT TYPE": "2",
    "PRIORITY (HOT, WARM & COLD)": "3",
    "Number of Associated Deals": "4",
    "Number of open deals": "5",
    "LAST ACTIVITY DATE": "6",
    "Last Engagement Date": "7",
    "CREATE DATE": "8",
}
cols = r.match_columns(fake_map)
check("匹配 account_name", cols.get("account_name") == "1")
check("匹配 existing_type (ACCOUNT TYPE)", cols.get("existing_type") == "2")
check("匹配 priority (带括号后缀)", cols.get("existing_priority") == "3")
check("匹配 associated deals", cols.get("num_associated_deals") == "4")
check("匹配 open deals", cols.get("num_open_deals") == "5")
check("匹配 last activity", cols.get("last_activity_date") == "6")
check("匹配 last engagement", cols.get("last_engagement_date") == "7")
check("必要列齐全 → 无缺", r.missing_required(cols) == [])

# 缺列时能报出来
partial = r.match_columns({"ACCOUNT NAME": "1", "ACCOUNT TYPE": "2", "PRIORITY": "3"})
miss = r.missing_required(partial)
check("缺 open deals 被识别", "num_open_deals" in miss)
check("缺 last engagement 被识别", "last_engagement_date" in miss)

# priority/type 若同名子串不误伤:'Number of open deals' 不该被当 priority
check("open deals 不误伤成 priority", cols.get("existing_priority") == "3")

# ── 单元格解析 ───────────────────────────────────────
check("parse_type Core", r.parse_type("Core") == "core")
check("parse_type Prospecting", r.parse_type("Prospecting") == "prospecting")
check("parse_type 空 → None", r.parse_type("--") is None)
check("parse_priority '5 (Hot)'", r.parse_priority("5 (Hot)") == "hot")
check("parse_priority '3 (Warm)'", r.parse_priority("3 (Warm)") == "warm")
check("parse_priority '--' → None", r.parse_priority("--") is None)
check("parse_priority 纯数字 1 → cold", r.parse_priority("1") == "cold")
check("parse_priority '0 (dead)' → dead", r.parse_priority("0 (dead)") == "dead")
check("parse_int '2'", r.parse_int_cell("2") == 2)
check("parse_int '--' → 0", r.parse_int_cell("--") == 0)
check("parse_int 空 → 0", r.parse_int_cell("") == 0)

print("\n" + ("❌ 失败: " + str(fails) if fails else "✅ 全绿"))
sys.exit(1 if fails else 0)
