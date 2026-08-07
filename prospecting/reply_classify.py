"""prospecting/reply_classify.py — 回复分类(v2 两步:Breeze 抽事实 → 贾维斯 LLM 判类别)。

分工(Ned 定,2026-08-05):
  ① Breeze 只抽【事实】—— 读该账户最新入站回信,返回 {contact, reply_date, reply_text(原文/忠实转述)}。
     【不判类别】(判断不交给 Breeze)。
  ② 贾维斯自己的 LLM 判【类别】—— 读 reply_text,归到 reply_router 的 6 类之一,或 none(OOO/自动回复/无实质)。
     判断权和 6 类逻辑握在贾维斯这边,好调、和"Breeze 抽事实/贾维斯判断"一致。

分层:
  - 纯层(build_extract_prompt/parse_extract/build_classify_prompt/parse_classification/_timing_to_days):零副作用,可单测。
  - 集成层(classify):Breeze 抽事实(breeze.ask)+ 贾维斯 LLM 判(core.llm),需登录浏览器 + LLM,本机验。

约定:classify() 返回 dict ⇒ 真回复(编排置 reply_is_real=True);返回 None ⇒ 无 inbound 或判成 none/OOO
      (编排置 reply_is_real=False → 账户当没回复、回落轮次)。见 outreach_state.account_outreach_state。

⚠ tainting:Breeze 抽的是客户邮件(不可信内容),贾维斯 LLM 也只据此【生成提议】,同回合不自动对外写。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional

from prospecting import reply_router as rr
from prospecting import breeze_outreach as bz   # 复用 _iter_json_objects / _normalize_quotes(纯函数)


# ── 步骤①:Breeze 抽事实(不判类别)────────────────────────────
def build_extract_prompt(account_name: str, website: Optional[str] = None) -> str:
    """让 Breeze 抽某账户最新入站回信的【事实】,单行 JSON。不判类别。含消歧守卫、无问号。"""
    site = f' (website: {website})' if website else ''
    return (
        f'The account named "{account_name}"{site} is the exact, already-identified company; '
        f'answer only about this one and do not ask me any question. '
        f'Find the most recent INBOUND email that any contact at this account sent to us (an email FROM '
        f'them TO us). Include it EVEN IF it is an automated out-of-office / auto-reply / vacation message — '
        f'capture whatever was written, verbatim; do NOT filter it out, and do NOT judge, summarize or '
        f'categorize it. Only exclude our own outbound emails. Reply with ONLY one-line JSON (no line breaks, '
        f'no other text): {{"account":"{account_name}","found":true,"contact":"<name or ->",'
        f'"reply_date":"<YYYY-MM-DD or ->","reply_text":"<verbatim text of what they wrote>"}} '
        f'If there is genuinely NO inbound email from this account at all, return found:false.'
    )


def parse_extract(text: str) -> Optional[dict]:
    """解析 Breeze 抽的事实 JSON → {contact, reply_date, reply_text};无回信/无正文 → None。"""
    for obj in bz._iter_json_objects(bz._normalize_quotes(text or "")):
        if not (isinstance(obj, dict) and ("reply_text" in obj or "found" in obj)):
            continue
        if obj.get("found") is False:
            return None
        rt = _dash(obj.get("reply_text"))
        if not rt:
            return None
        return {"contact": _dash(obj.get("contact")), "reply_date": _dash(obj.get("reply_date")),
                "reply_text": rt}
    return None


# ── 步骤②:贾维斯 LLM 判 6 类 ─────────────────────────────────
# Breeze 必须从这 6 类里选一个(或 none=非真回复)。这段现在是【贾维斯 LLM】的分类指引。
_CATEGORY_GUIDE = f"""Pick exactly ONE category id from this list:
- {rr.LIST_RELEVANT}: they sent an excess/surplus PARTS list of board-level electronic components (what we broker).
- {rr.LIST_IRRELEVANT}: they sent a list, but it's finished goods / not board-level components (e.g. headphones, power banks, cables).
- {rr.INTERESTED_LATER}: interested but nothing to sell right now — no excess yet (may mention a future time), OR blocked on an NDA/internal process, OR they already have a channel/partner. Estimate WHEN to follow up and put it in STOCK_TIMING; put the reason (and any competitor named) in SUMMARY.
- {rr.EXPLICIT_NO}: explicitly not interested / do not contact.
- {rr.REFERRAL}: they refer you to another person or department.
- {rr.PLEASANTRY}: content-free acknowledgement ("thanks", "will check", "forwarded internally").
- none: not a real reply — an automated out-of-office / auto-reply / vacation responder ("I am away", "limited access to messages"), a bounce, or otherwise no substantive human message. Treat these as none."""


def build_classify_prompt(reply_text: str, account_name: str = "") -> str:
    """贾维斯 LLM 的分类 prompt:读 reply_text,按类别输出固定 KEY:value 行(供 parse_classification 解析)。"""
    acct = account_name or "an account"
    return (
        f'A contact at {acct} sent us this inbound reply:\n\n"""{reply_text}"""\n\n'
        f'{_CATEGORY_GUIDE}\n\n'
        f'Then output EXACTLY these lines and NOTHING else (no tables, no prose, no markdown). Use "-" if unknown:\n'
        f'CATEGORY: <one id from the list above>\n'
        f'SUMMARY: <one short English sentence of what they said>\n'
        f'STOCK_TIMING: <when they will have excess, verbatim, e.g. "Q4 2026" / "after March" / -, or ->\n'
        f'COMPETITOR: <competitor/partner name if they mentioned one, or ->'
    )


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
    """把 LLM 的 KEY:value 输出解析成结构化 dict;category=none 或解析不出 → None(非真回复/无动作)。

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


# ── 贾维斯 LLM 调用(集成;失败一律 None → 无提议,fail-safe)────────
async def _llm_classify_text(reply_text: str, account_name: str = "") -> str:
    """调贾维斯自己的 LLM 判类别,返回原始 KEY:value 文本(失败返回空串)。

    ⚠ 必须在协程内 await client.close() —— get_client 每次新建 AsyncOpenAI,不关的话 httpx 连接池
       会在 asyncio.run 关闭事件循环【之后】才被 GC 清理 → 'Event loop is closed' 噪音(实盘教训)。
    """
    client = None
    try:
        import config
        from core.llm import get_client
        client = get_client()
        model = getattr(config, "CLAUDE_MODEL_LIGHT", None) or config.CLAUDE_MODEL
        try:
            from prospecting.business_context import business_context
            ctx = business_context()
        except Exception:
            ctx = ""
        system = "You classify inbound B2B sales-email replies. Follow the instructions exactly."
        if ctx:
            system += "\n\n业务背景(判类别时参考):\n" + ctx
        resp = await client.chat.completions.create(
            model=model, max_tokens=200, timeout=30,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": build_classify_prompt(reply_text, account_name)},
            ],
        )
        return (resp.choices[0].message.content or "")
    except Exception:
        return ""
    finally:
        if client is not None:
            try:
                await client.close()             # 在循环内关连接池,避免 loop 关闭后 GC 报错
            except Exception:
                pass


def classify_reply_text(reply_text: str, account_name: str = "", today: Optional[date] = None) -> Optional[dict]:
    """贾维斯 LLM 判 reply_text → 结构化分类 dict 或 None(none/OOO/失败)。

    同步封装:在【独立线程 + 全新事件循环】里跑 asyncio。这样无论调用方是否已在事件循环里
    (如 playwright 驱动浏览器的上下文)都能安全跑,不撞 "asyncio.run() cannot be called from a
    running event loop"(Welotec 实盘教训:原来直接 asyncio.run 被吞成 None,LLM 根本没调)。
    """
    if not (reply_text or "").strip():
        return None
    import asyncio
    import concurrent.futures

    def _runner():
        return asyncio.run(_llm_classify_text(reply_text, account_name))

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            raw = ex.submit(_runner).result(timeout=60)
    except Exception:
        return None
    return parse_classification(raw, today)


# ── 集成层(需登录浏览器 + LLM;本机验)────────────────────
def classify(page, account_name: str, logger=None, timeout_s: float = 180,
             website: Optional[str] = None) -> Optional[dict]:
    """两步:Breeze 抽回信事实 → 贾维斯 LLM 判类别。返回分类 dict(真回复)或 None(无 inbound / none / OOO)。

    ⚠ tainting:结果只喂"生成提议",不得同回合自动对外写。
    """
    from prospecting import breeze
    res = breeze.ask(page, build_extract_prompt(account_name, website), logger=logger, timeout_s=timeout_s)
    fact = parse_extract(res.get("text", ""))
    if not fact:
        return None                       # 无 inbound
    verdict = classify_reply_text(fact["reply_text"], account_name)
    if not verdict:
        return None                       # 判成 none/OOO 或 LLM 不可用 → 非真回复
    verdict["contact"] = verdict.get("contact") or fact.get("contact")
    verdict["reply_date"] = verdict.get("reply_date") or fact.get("reply_date")
    verdict["reply_text"] = fact["reply_text"]
    return verdict
