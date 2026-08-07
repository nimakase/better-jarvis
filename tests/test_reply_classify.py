"""回复分类的纯层单测(prompt 约束 + 解析 + 时间估算)。

    .venv/bin/python tests/test_reply_classify.py
"""
import sys
from datetime import date
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from prospecting import reply_classify as rc   # noqa: E402
from prospecting import reply_router as rr     # noqa: E402

TODAY = date(2026, 7, 26)
fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# 步骤①:Breeze 抽事实 prompt —— 只要事实、不判类别、含消歧守卫
ep = rc.build_extract_prompt("Acme Corp")
check("extract 单行", "\n" not in ep)
check("extract 含账户名 + reply_text 字段", "Acme Corp" in ep and "reply_text" in ep)
check("extract 只抽事实不判类别", "do NOT judge" in ep or "do not judge" in ep)
check("extract 明确纳入 OOO/自动回复(不预过滤)", "out-of-office" in ep and "auto-reply" in ep)
check("extract 含精确名守卫、无问号", 'named "Acme Corp"' in ep and "?" not in ep)
check("extract 无回信 → found:false", "found:false" in ep)

# parse_extract:解析 Breeze 的事实 JSON
fx = rc.parse_extract('noise {"account":"Acme","found":true,"contact":"Alice","reply_date":"2026-07-20","reply_text":"No stock until Q4"} tail')
check("parse_extract 出 reply_text", fx and fx["reply_text"] == "No stock until Q4" and fx["contact"] == "Alice")
check("parse_extract found:false → None", rc.parse_extract('{"found":false}') is None)
check("parse_extract 空正文 → None", rc.parse_extract('{"found":true,"reply_text":"-"}') is None)

# 步骤②:贾维斯 LLM 分类 prompt —— 含 6 类 + reply_text + none(OOO)指引
cp = rc.build_classify_prompt("Sorry, not interested.", "Acme Corp")
check("classify 含类别 id", rr.INTERESTED_LATER in cp and rr.EXPLICIT_NO in cp)
check("classify 要 CATEGORY 行", "CATEGORY:" in cp)
check("classify 含 reply_text", "not interested" in cp)
check("classify 把 OOO 当 none", "out-of-office" in cp)

# 解析:标准输出
sample = """CATEGORY: interested_later
SUMMARY: They have no surplus right now but expect some in Q4.
CONTACT: Alice Wang
REPLY_DATE: 2026-07-20
STOCK_TIMING: Q4 2026
COMPETITOR: -"""
d = rc.parse_classification(sample, today=TODAY)
check("解析 category", d["category"] == rr.INTERESTED_LATER)
check("解析 contact", d["contact"] == "Alice Wang")
check("解析 competitor '-'→None", d["competitor"] is None)
check("Q4 2026 估成正天数", isinstance(d["stock_wake_days"], int) and d["stock_wake_days"] > 30)

# 明确不做
d2 = rc.parse_classification("CATEGORY: explicit_no\nSUMMARY: Not interested.\nCONTACT: -", today=TODAY)
check("解析 explicit_no", d2["category"] == rr.EXPLICIT_NO)
check("无 timing → wake_days None", d2["stock_wake_days"] is None)

# 无回信 / 未知类别 → None
check("category none → None", rc.parse_classification("CATEGORY: none") is None)
check("未知类别 → None", rc.parse_classification("CATEGORY: bogus") is None)
check("空文本 → None", rc.parse_classification("") is None)

# 时间估算
check("timing '-' → None", rc._timing_to_days("-", TODAY) is None)
check("timing 过去 → None", rc._timing_to_days("Q1 2025", TODAY) is None)
check("timing 'March 2027' → 正天数",
      isinstance(rc._timing_to_days("March 2027", TODAY), int) and rc._timing_to_days("March 2027", TODAY) > 180)
# 自然语言时间
check("'in the end of the year' → 正天数(~年底)",
      100 < (rc._timing_to_days("in the end of the year", TODAY) or 0) < 200)
check("'in 2 months' → ~60", rc._timing_to_days("in 2 months", TODAY) == 60)
check("'next year' → 正天数", (rc._timing_to_days("next year", TODAY) or 0) > 120)
check("'next quarter' → ~90", rc._timing_to_days("next quarter", TODAY) == 90)

# 分类结果能喂 router
d3 = rc.parse_classification(sample, today=TODAY)
prop = rr.route(d3["category"], stock_wake_days=d3["stock_wake_days"], account_name="Acme")
check("分类→路由:暂无货用估算的唤醒天数",
      prop["task"]["wake_days"] == d3["stock_wake_days"])

print("\n" + ("❌ 失败: " + str(fails) if fails else "✅ 全绿"))
sys.exit(1 if fails else 0)
