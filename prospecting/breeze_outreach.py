"""prospecting/breeze_outreach.py — 用 Breeze 抽账户的 outreach 历史。

分工(见 docs/客户循环-view管理重设计.md):Breeze 只抽事实(邮件日期/标题/联系人/岗位 + 谁回过信),
轮次/到顶等判断交给 outreach_state(贾维斯确定性算)。

纯函数(build_prompt / parse_response / emails_to_contacts)沙箱可单测;
ask_breeze() 驱动 HubSpot Copilot,选择器待实盘校准。
"""
from __future__ import annotations

import json
import re


# ── 提问模板 ──────────────────────────────────────────────────
def build_prompt(account_name: str) -> str:
    """给 Breeze 的问题:只要事实,固定 JSON 输出。

    ⚠ 必须【单行、无换行】—— 聊天输入框里换行=回车=提前发送(会把 prompt 打断成好几条)。
    """
    return (
        f'For the company account "{account_name}", list every cold-outreach email sent BY '
        f'Alex Test to its contacts (usually 3 emails sharing the same subject line). For each give: '
        f'date, subject, recipient contact name, and the recipient\'s job title if known. Also list '
        f'the names of any contacts at this company who have REPLIED to Ned. Exclude replies '
        f'themselves and any email not sent by Ned. Reply with ONLY one-line JSON (no line breaks, '
        f'no other text) in this shape: '
        f'{{"account":"{account_name}","found":true,'
        f'"outreach_emails":[{{"date":"YYYY-MM-DD","subject":"","to_contact":"","job_title":""}}],'
        f'"replied_contacts":[]}} '
        f'If none, return found:false with empty arrays.'
    )


# ── 解析 Breeze 返回文本 → JSON 对象 ──────────────────────────
def _iter_json_objects(text: str):
    """从可能夹着散文的文本里,逐个吐出能 json.loads 的顶层 {..} 对象。"""
    s = text or ""
    i, n = 0, len(s)
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < n:
            ch = s[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = s[i:j + 1]
                        try:
                            yield json.loads(chunk)
                        except Exception:
                            pass
                        i = j
                        break
            j += 1
        i += 1


_REAL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Breeze 渲染答案时会把 JSON 美化成【弯引号】(“ ” ‘ ’),json.loads 会崩 → 解析前归一成直引号。
_SMART_QUOTES = {
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
}


def _normalize_quotes(s: str) -> str:
    return "".join(_SMART_QUOTES.get(ch, ch) for ch in (s or ""))


def _is_echo(obj: dict) -> bool:
    """是不是 prompt 里那段示例模板的回显(date=YYYY-MM-DD、字段空)——要跳过,别当答案。"""
    emails = obj.get("outreach_emails") or []
    if not emails:
        return False  # 空数组是合法的 found:false,不算回显
    for e in emails:
        d = str(e.get("date") or "")
        if _REAL_DATE.match(d):      # 有真实日期 → 是真答案
            return False
    return True                      # 有邮件但全是占位 → 回显


def parse_response(text: str) -> dict:
    """从 Breeze 回复里挑出真正的答案 JSON(含 outreach_emails 键);跳过 prompt 回显模板,取最后一个真对象。"""
    best = None
    for obj in _iter_json_objects(_normalize_quotes(text)):
        if not (isinstance(obj, dict) and "outreach_emails" in obj):
            continue
        if not isinstance(obj.get("outreach_emails"), list):
            obj["outreach_emails"] = []
        if not isinstance(obj.get("replied_contacts"), list):
            obj["replied_contacts"] = []
        obj.setdefault("account", "")
        if _is_echo(obj):
            continue
        best = obj                   # 保留最后一个真对象(答案在回显之后)
    if best is not None:
        best.setdefault("found", bool(best.get("outreach_emails")))
        return best
    return {"account": "", "found": False, "outreach_emails": [], "replied_contacts": []}


# ── 邮件列表 → 按联系人聚合(outreach_state 的输入)──────────────
def _norm(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def emails_to_contacts(parsed: dict) -> list:
    """把 outreach_emails 按联系人聚合成 [{name,job_title,sent_dates,replied}](喂 outreach_state)。"""
    replied = {_norm(x) for x in (parsed.get("replied_contacts") or [])}
    by: dict = {}
    for e in (parsed.get("outreach_emails") or []):
        name = (e.get("to_contact") or "").strip()
        if not name:
            continue
        key = _norm(name)
        rec = by.setdefault(key, {"name": name, "job_title": None, "sent_dates": [], "replied": False})
        d = (e.get("date") or "").strip()
        if d:
            rec["sent_dates"].append(d)
        if not rec["job_title"] and (e.get("job_title") or "").strip():
            rec["job_title"] = e["job_title"].strip()
        if key in replied:
            rec["replied"] = True
    for rec in by.values():
        rec["sent_dates"].sort()
    return list(by.values())


# ── 浏览器驱动(HubSpot Copilot)—— 选择器实盘校准(2026-07-31)────
# Copilot 在 iframe(url 含 chatspot-widget-ui/chat)里:
#   启动(主frame): data-test-id="hs-global-toolbar-copilot-list-item"
#   输入(iframe内): data-test-id="prose-mirror-chat-input"(ProseMirror contenteditable)
#   发送(iframe内): data-test-id="chat-send-button"
#   回复也渲染在该 iframe → 读取读 frame 文本,靠 parse_response 挑出唯一含 outreach_emails 的 JSON。
COPILOT_LAUNCHER = '[data-test-id="hs-global-toolbar-copilot-list-item"]'
COPILOT_FRAME_HINT = "chatspot-widget-ui"
COPILOT_INPUT = '[data-test-id="prose-mirror-chat-input"]'
COPILOT_SEND = '[data-test-id="chat-send-button"]'
COPILOT_HEADER = '[data-test-id="chat-header"]'    # 在对话内时,头部有 "Back" 文本;点它回到空白输入=新对话


def _copilot_frame(page):
    for f in page.frames:
        if COPILOT_FRAME_HINT in (f.url or ""):
            return f
    return None


def _visible_input(page):
    """返回 (frame, input_locator) 当 Copilot 输入框可见(=面板已开);否则 (frame_or_None, None)。"""
    frame = _copilot_frame(page)
    if frame is None:
        return None, None
    inp = frame.locator(COPILOT_INPUT).first
    try:
        if inp.count() > 0 and inp.is_visible():
            return frame, inp
    except Exception:
        pass
    return frame, None


def ask_breeze(browser, account_name: str, timeout_ms: int = 180000, logger=None) -> dict:
    """驱动 HubSpot Copilot(iframe)问一个账户的 outreach → 解析成结构化。

    开面板(若未开)→ 等 iframe → frame 内填 prompt、发送 → 轮询 frame 文本直到出现含
    outreach_emails 的 JSON 且稳定 → parse_response → emails_to_contacts。返回 {"parsed","contacts","raw"}。
    """
    page = browser.page
    prompt = build_prompt(account_name)

    # ⚠ chatspot iframe 面板关着也在 DOM 里,不能靠 iframe 在不在判断面板开没开 —— 要看【输入框可见】。
    frame, inp = _visible_input(page)
    if inp is None:                         # 面板没开(或输入不可见)→ 点启动按钮
        try:
            page.locator(COPILOT_LAUNCHER).first.click(timeout=6000)
        except Exception:
            pass
        for _ in range(24):
            page.wait_for_timeout(500)
            frame, inp = _visible_input(page)
            if inp is not None:
                page.wait_for_timeout(600)
                break
    if inp is None or frame is None:
        return {"parsed": parse_response(""), "contacts": [], "raw": "",
                "error": "copilot 面板未打开/输入框不可见(启动按钮选择器?)"}

    # ★ 每个账户起【新对话】:若在已有对话里(chat-header 有 "Back")→ 点 Back 回到空白输入。
    #   否则历史会越堆越长、Breeze 被上下文串台,且扫文本会读到旧账户的 JSON。
    try:
        back = frame.locator(COPILOT_HEADER).get_by_text("Back", exact=True)
        if back.count() > 0:
            back.first.click(timeout=3000)
            page.wait_for_timeout(1200)
            frame, inp2 = _visible_input(page)
            inp = inp2 or inp
    except Exception:
        pass

    inp.click(timeout=8000)
    page.keyboard.type(prompt)              # 单行 prompt,不会中途触发发送;Back 已给空输入,无需再清
    page.wait_for_timeout(900)              # 等 ProseMirror 注册文本、发送键变可用
    send = frame.locator(COPILOT_SEND).first
    try:
        send.wait_for(state="visible", timeout=4000)
        if send.is_enabled():
            send.click(timeout=4000)
        else:
            page.keyboard.press("Enter")
    except Exception:
        page.keyboard.press("Enter")

    prev, stable, waited, parsed = "", 0, 0, None
    text = ""
    while waited < timeout_ms:
        page.wait_for_timeout(2000)
        waited += 2000
        try:
            text = frame.locator("body").inner_text(timeout=3000)
        except Exception:
            text = ""
        stable = stable + 1 if text == prev else 0
        cand = parse_response(text)          # 已跳过 prompt 回显模板
        if cand.get("outreach_emails") and text == prev:
            parsed = cand                    # 拿到真数据且稳定 → 立刻返回(正路)
            break
        # 兜底(仅给"真没 outreach"的账户):要稳定够久 + 等够久,才认定完成。
        # 阈值放宽是为了熬过 Breeze 开始输出前的"思考停顿"(否则会把停顿误判成完成)。
        if stable >= 12 and waited >= 45000:
            parsed = cand
            break
        prev = text
    parsed = parsed or parse_response(text)
    if logger:
        logger.info("breeze_outreach | %s | found=%s emails=%d replied=%d",
                    account_name, parsed.get("found"),
                    len(parsed.get("outreach_emails", [])), len(parsed.get("replied_contacts", [])))
    return {"parsed": parsed, "contacts": emails_to_contacts(parsed), "raw": text}
