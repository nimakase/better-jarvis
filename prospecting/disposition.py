"""prospecting/disposition.py — 处置解读器(秘书逻辑)。

Ned 在 Bitable「处置」格里随手写自由文本 → 贾维斯 LLM 读懂【意图】(固定集)+ 整理成一句话。
带业务上下文(business_context)判得准。这是 Ned ↔ 贾维斯的【通用沟通渠道】:一个 note 顶一堆写死的 if。

安全:意图只驱动【贾维斯侧安全动作】(压 FYI / 暂停催 / 出提议 / 标 list 质量 / 清队列 / 纯记录);
      【绝不】因一句备注去写 HubSpot 的 Account Type / owner、或做不可逆的事——那些照旧走提议 / Ned 手动。

分层:纯层(build_prompt / parse)可单测;interpret() 调 core.llm(线程 + 新事件循环,失败→当纯备注兜底)。
"""
from __future__ import annotations

from typing import Optional

# 意图集(与 docs/业务逻辑与工作流程.md §10 对齐)
PROTECT = "protect"            # 保护/别动这家
SWITCH = "switch_contact"      # 换人试试
SNOOZE = "snooze"             # 暂停/在谈/稍后(可带时限)
DROP = "drop"                 # 放弃/别再碰
LIST_QUALITY = "list_quality"  # 料好/料差
DONE = "done"                 # 已处理
NOTE = "note"                 # 纯备注,无动作
UNCLEAR = "unclear"           # 看不懂
INTENTS = {PROTECT, SWITCH, SNOOZE, DROP, LIST_QUALITY, DONE, NOTE, UNCLEAR}

_GUIDE = f"""从这些意图里选【恰好一个】id:
- {PROTECT}: 保护/留住这家,别动它(如"保护性标core"、"别动这家")。
- {SWITCH}: 换个联系人/部门试(如"换人试Y"、"找采购Z")。
- {SNOOZE}: 先暂停/搁着,可能到某时间(如"在谈别提醒"、"下季度再说")。
- {DROP}: 放弃这家(如"放弃"、"别再碰")。
- {LIST_QUALITY}: 对他们 list 料质量的判断(如"料好"、"料差没法做")。
- {DONE}: 已经处理好了(如"已处理"、"搞定了")。
- {NOTE}: 纯备注,不含要贾维斯做什么。
- {UNCLEAR}: 看不出他想要什么。"""


def build_prompt(note: str, account_context: str = "") -> str:
    """给贾维斯 LLM 的处置解读 prompt:带业务上下文 + 账户情况 + Ned 的备注,输出固定 KEY:value。"""
    try:
        from prospecting.business_context import business_context
        ctx = business_context()
    except Exception:
        ctx = ""
    head = (f"业务背景:\n{ctx}\n\n" if ctx else "")
    return (
        f"{head}账户当前情况:{account_context or '(无)'}\n\n"
        f"Ned 在客户循环驾驶舱给这个账户写的处置备注:\n\"\"\"{note}\"\"\"\n\n"
        f"{_GUIDE}\n\n"
        f"只输出这两行,别的都不要:\n"
        f"INTENT: <一个 id>\n"
        f"SUMMARY: <一句中文:Ned 想让贾维斯做什么>"
    )


def parse(text: str) -> dict:
    """解析 LLM 的 KEY:value → {intent, summary}。认不出 intent → unclear。"""
    vals = {}
    for line in (text or "").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip().upper()
            if k in ("INTENT", "SUMMARY"):
                vals[k] = v.strip()
    intent = (vals.get("INTENT") or "").strip().lower()
    if intent not in INTENTS:
        intent = UNCLEAR
    return {"intent": intent, "summary": (vals.get("SUMMARY") or "").strip() or None}


async def _llm(note: str, account_context: str = "") -> str:
    client = None
    try:
        import config
        from core.llm import get_client
        client = get_client()
        model = getattr(config, "CLAUDE_MODEL_LIGHT", None) or config.CLAUDE_MODEL
        resp = await client.chat.completions.create(
            model=model, max_tokens=150, timeout=30,
            messages=[
                {"role": "system", "content": "你是 Ned 的销售秘书,读懂他给账户写的处置备注、判他想让贾维斯做什么。严格按格式输出。"},
                {"role": "user", "content": build_prompt(note, account_context)},
            ],
        )
        return resp.choices[0].message.content or ""
    except Exception:
        return ""
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


def interpret(note: str, account_context: str = "") -> Optional[dict]:
    """读懂 Ned 的处置备注 → {intent, summary}。空备注→None;LLM 不可用→当纯备注(不乱动)。

    同步封装(独立线程 + 新事件循环,兼容 playwright/asyncio 上下文,同 reply_classify)。
    """
    if not (note or "").strip():
        return None
    import asyncio
    import concurrent.futures

    def _run():
        return asyncio.run(_llm(note, account_context))

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            raw = ex.submit(_run).result(timeout=45)
    except Exception:
        return {"intent": NOTE, "summary": None}      # LLM 不可用 → 当纯备注,别乱动
    return parse(raw)
