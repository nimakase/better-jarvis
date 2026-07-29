"""prospecting/reply_classify.py — 用 Breeze 读某账户最新入站回信并分类(回复路②)。

分两层:
  - 纯层:build_prompt(约束 Breeze 只吐固定 KEY:value 行)+ parse_classification(解析)+
    _timing_to_days(把"Q4 2026/明年3月"这类估成天数)。零依赖、可单测。
  - 集成层:classify(page, account) —— breeze.ask(prompt) → 解析。需登录浏览器,本机验。

⚠ Breeze 读的是客户邮件(不可信内容)→ 本步是 tainting。分类结果只用于【生成提议】进日报
   待确认(见 reply_router / 护栏合规方案 a),绝不同回合自动对外写。

分类结果 → reply_router.route() → 提议。两者拼起来是"读回信→分级→提议"整条。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from prospecting import reply_router as rr

# Breeze 必须从这 8 类里选一个(或 none=无入站回信)
_CATEGORY_GUIDE = f"""Pick exactly ONE category id from this list:
- {rr.LIST_RELEVANT}: they sent an excess/surplus PARTS list of board-level electronic components (what we broker).
- {rr.LIST_IRRELEVANT}: they sent a list, but it's finished goods / not board-level components (e.g. headphones, power banks, cables).
- {rr.INTERESTED_NO_STOCK}: interested, but no excess right now (they may mention a future time when they'll have some).
- {rr.STUCK_NDA}: interested, but blocked on an NDA / internal approval / process.
- {rr.HAS_CHANNEL}: they already have a channel/partner for their excess (may name a competitor).
- {rr.EXPLICIT_NO}: explicitly not interested / do not contact.
- {rr.REFERRAL}: they refer you to another person or department.
- {rr.PLEASANTRY}: content-free acknowledgement ("thanks", "will check", "forwarded internally").
- none: there is no inbound reply from this account's contacts."""


def build_prompt(account_name: str) -> str:
    """约束 Breeze:读该账户最新入站回信,按类别输出【固定 KEY:value 行】,不要表格/散文。"""
    return f"""Look at the account "{account_name}" and its contacts' most recent INBOUND email reply to us
(only messages they sent to us; ignore our outbound emails and automated logs).

{_CATEGORY_GUIDE}

Then output EXACTLY these lines and NOTHING else (no tables, no prose, no markdown). Use "-" if unknown:
CATEGORY: <one id from the list above>
SUMMARY: <one short English sentence of what they said>
CONTACT: <the contact's name, or ->
REPLY_DATE: <the reply date as YYYY-MM-DD, or ->
STOCK_TIMING: <when they'll have excess, verbatim, e.g. "Q4 2026" / "after March" / -, or ->
COMPETITOR: <competitor/partner name if they mentioned one, or ->"""


# ── 解析(纯)────────────────────────────────────────────
_KEYS = ("CATEGORY", "SUMMARY", "CONTACT", "REPLY_DATE", "STOCK_TIMING", "COMPETITOR")


def _dash(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    return None if v in ("", "-", "--", "n/a", "none", "unknown") else v


def _timing_to_days(text: Optional[str], today: Optional[date] = None) -> Optional[int]:
    """把"Q4 2026 / March 2027 / 2026-11"这类估成"距今天数"。认不出返回 None(路由用默认 90)。"""
    t = _dash(text)
    if t is None:
        return None
    today = today or datetime.now(timezone.utc).date()
    import re
    low = t.lower()
    target = None

    # 自然语言常见说法(先于日期解析)
    m = re.search(r"in\s+(\d+)\s+(day|week|month)", low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = {"day": 1, "week": 7, "month": 30}[unit]
        d = n * mult
        return d if d > 0 else None
    if re.search(r"end of (the )?year|year[- ]?end", low):
        target = date(today.year, 12, 1)
        if target <= today:
            target = date(today.year + 1, 12, 1)
    elif re.search(r"next year", low):
        target = date(today.year + 1, 1, 1)
    elif re.search(r"next quarter", low):
        target = today.fromordinal(today.toordinal() + 90)

    # 季度 → 该季度首月
    q = re.search(r"[Qq]\s*([1-4])\D*(\d{4})", t) if target is None else None
    if q:
        month = {1: 1, 2: 4, 3: 7, 4: 10}[int(q.group(1))]
        try:
            target = date(int(q.group(2)), month, 1)
        except Exception:
            target = None
    if target is None:
        try:
            from dateutil import parser as _dtparser
            target = _dtparser.parse(t, fuzzy=True, default=datetime(today.year, today.month, 1)).date()
        except Exception:
            return None
    days = (target - today).days
    return days if days > 0 else None   # 已过去/当下 → 用默认周期,不设负数


def parse_classification(text: str, today: Optional[date] = None) -> Optional[dict]:
    """把 Breeze 的 KEY:value 输出解析成结构化 dict;category=none 或解析不出 → None(无动作)。

    返回 {category, summary, contact, reply_date, stock_timing, stock_wake_days, competitor}。
    """
    vals = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip().upper()
        if k in _KEYS:
            vals[k] = v.strip()

    cat = _dash(vals.get("CATEGORY"))
    if not cat:
        return None
    cat = cat.lower()
    if cat == "none" or cat not in rr.CATEGORIES:
        return None

    return {
        "category": cat,
        "summary": _dash(vals.get("SUMMARY")),
        "contact": _dash(vals.get("CONTACT")),
        "reply_date": _dash(vals.get("REPLY_DATE")),
        "stock_timing": _dash(vals.get("STOCK_TIMING")),
        "stock_wake_days": _timing_to_days(vals.get("STOCK_TIMING"), today),
        "competitor": _dash(vals.get("COMPETITOR")),
    }


# ── 集成层(需登录浏览器;本机验)────────────────────────
def classify(page, account_name: str, logger=None, timeout_s: float = 60) -> Optional[dict]:
    """驱动 Breeze 读该账户最新回信并分类。返回结构化 dict 或 None(无回信/解析失败)。

    ⚠ tainting:结果只喂"生成提议",不得同回合自动对外写。
    """
    from prospecting import breeze
    res = breeze.ask(page, build_prompt(account_name), logger=logger, timeout_s=timeout_s)
    return parse_classification(res.get("text", ""))
