"""回复路编排单测(变化检测 + 提议编排 + 已处理去重)。注入 classify/processed,脱浏览器。

    .venv/bin/python tests/test_reply_path.py
"""
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

os.environ["JARVIS_CUSTOMER_LOOP_DIR"] = tempfile.mkdtemp(prefix="rp_")

from prospecting import reply_path as rp        # noqa: E402
from prospecting import reply_router as rr      # noqa: E402
from prospecting import customer_loop_store as store  # noqa: E402

TODAY = date(2026, 7, 26)
fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ── 变化检测:只挑最近有 engagement 的 ──────────────────
recent = (TODAY - timedelta(days=1)).isoformat()
old = (TODAY - timedelta(days=30)).isoformat()
records = [
    {"account_name": "Fresh", "last_engagement_date": recent},
    {"account_name": "Stale", "last_engagement_date": old},
    {"account_name": "Never", "last_engagement_date": None},
]
cands = rp.find_reply_candidates(records, today=TODAY)
check("候选含最近 engagement 的", "Fresh" in cands)
check("候选不含旧的", "Stale" not in cands)
check("候选不含从无的", "Never" not in cands)

# ── 提议编排:none 跳过、有回复出提议 ──────────────────
def fake_classify(acct):
    if acct == "Fresh":
        return {"category": rr.INTERESTED_NO_STOCK, "reply_date": "2026-07-25",
                "stock_wake_days": 120, "summary": "no stock, Q4"}
    return None   # 其余无回信

props = rp.build_proposals(["Fresh", "Other"], fake_classify, today=TODAY)
check("有回复→1 条提议", len(props) == 1)
check("提议账户正确", props[0]["account"] == "Fresh")
check("提议是暂无货、带 120 天唤醒",
      props[0]["proposal"]["task"]["wake_days"] == 120)

# 回信新鲜度闸:陈年回信(近期开信带出来)→ 跳过
def classify_old_reply(acct):
    return {"category": rr.REFERRAL, "reply_date": "2025-08-14", "summary": "old"}
check("陈年回信被跳过(不出提议)",
      rp.build_proposals(["Fresh"], classify_old_reply, today=TODAY) == [])
def classify_recent_reply(acct):
    return {"category": rr.REFERRAL, "reply_date": (TODAY - timedelta(days=5)).isoformat(), "summary": "new"}
check("近期回信保留",
      len(rp.build_proposals(["Fresh"], classify_recent_reply, today=TODAY)) == 1)

# ── 已处理去重:同一条回信不重复提 ────────────────────
def always_processed(acct, rdate):
    return True

check("已处理的回信被跳过",
      rp.build_proposals(["Fresh"], fake_classify, is_processed=always_processed) == [])

# ── run():真 store 跟踪 + 存提议 ──────────────────────
res1 = rp.run(records, fake_classify, today=TODAY)
check("run 首次出 1 提议", len(res1["proposals"]) == 1)
check("提议已入待确认库", len(store.get_pending_proposals()) == 1)
res2 = rp.run(records, fake_classify, today=TODAY)   # 再跑一次
check("run 二次不重复(回信已处理)", len(res2["proposals"]) == 0)

print("\n" + ("❌ 失败: " + str(fails) if fails else "✅ 全绿"))
sys.exit(1 if fails else 0)
