"""prospecting/breeze_outreach.py — 用 Breeze 抽账户的 outreach 历史。

分工(见 docs/客户循环-view管理重设计.md):Breeze 只抽事实(邮件日期/标题/联系人/岗位 + 谁回过信),
轮次/到顶等判断交给 outreach_state(贾维斯确定性算)。

纯函数(build_prompt / parse_response / emails_to_contacts)沙箱可单测;
ask_breeze() 驱动 HubSpot Copilot,选择器待实盘校准。
"""
from __future__ import annotations

import json
import re
from typing import Optional


# ── 提问模板 ──────────────────────────────────────────────────
def _exact_guard(account_name: str, website: Optional[str] = None) -> str:
    """开头的"直接指定 + 禁反问"句(消歧,防重名弹复核卡死)。outreach / deal 两个 prompt 共用。

    ⚠ 措辞刻意【不含】"several companies / disambiguate / which / 问号"等词:这段 prompt 会被
       回显进页面,若含这些词会命中 looks_like_disambiguation 造成【自我误判】(Photron 实盘教训 2026-08-05)。
    """
    site = f' (website: {website})' if website else ''
    return (
        f'The account named "{account_name}"{site} is the exact, already-identified company; '
        f'answer only about this one. If no company is named exactly "{account_name}", return found:false. '
        f'Give the answer directly and do not ask me any question. '
    )


def build_prompt(account_name: str, website: Optional[str] = None) -> str:
    """给 Breeze 的问题:只要事实,固定 JSON 输出。

    ⚠ 必须【单行、无换行】—— 聊天输入框里换行=回车=提前发送(会把 prompt 打断成好几条)。

    开头加【直接指定 + 禁反问】句:重名时 Breeze 会弹"你指 A 还是 B?"复核 → 脚本等不到 JSON
    卡到超时 → 空 → 误判 not_started(= 401 误标诱因之一)。这里明确禁止它反问/消歧,只认精确名;
    有 website 则一并给出,消歧更狠。见 docs/客户循环-view管理重设计.md 附三 B。
    """
    return _exact_guard(account_name, website) + (
        f'For the company account "{account_name}", list every cold-outreach email sent BY '
        f'Alex Test to its contacts (usually 3 emails sharing the same subject line). For each give: '
        f'date, subject, recipient contact name, and the recipient\'s job title if known. Also list '
        f'the names of any contacts at this company who have sent ANY inbound message back to Ned '
        f'(just report the fact that they replied; do not judge whether it is substantive). '
        f'Exclude replies themselves and any email not sent by Ned. Reply with ONLY one-line JSON (no line breaks, '
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


# ── 复核提问检测(供 ask_breeze 安全网:识别重名弹的消歧问句,别被它卡到超时)──────────
_DISAMBIG_PAT = re.compile(
    r"did you mean|do you mean|which (company|one|account|of these)|"
    r"multiple companies|several companies|more than one (company|match|result)|"
    r"could you (confirm|clarify|specify)|please (confirm|clarify|specify|choose|pick)|i found (a few|several|multiple)",
    re.I)
_CLARIFY_IMPERATIVE = re.compile(r"please (confirm|clarify|specify|choose|pick)", re.I)


def looks_like_disambiguation(text: str) -> bool:
    """Breeze 是否在反问消歧(重名时)。判据:命中消歧措辞、没解析出真答案、【且是真问句或明确祈使澄清】。

    ⚠ 关键:真复核是问句('?')或"please specify"这类明确祈使。我们自己的 guard prompt 是祈使句、
       无问号、无 please,会被回显进页面 —— 若只看措辞就会【自我误判】(Photron 教训 2026-08-05),
       故额外要求 '?' 或 please-祈使。识别到真复核时自动澄清一次/带错误退出,不静默返空。
    """
    if not text:
        return False
    if parse_response(text).get("outreach_emails"):
        return False                     # 已经有真答案 → 不是消歧
    if not _DISAMBIG_PAT.search(text):
        return False
    return ("?" in text) or bool(_CLARIFY_IMPERATIVE.search(text))


# ── 问 Breeze:账户 deal 结果计数(HubSpot 没有"赢单数"原生列 → 走 Breeze 真 CRM 工具)─────
def build_deal_prompt(account_name: str, website: Optional[str] = None) -> str:
    """问某账户的 deal 按结果计数(won/lost/open)。用于 Core 分层(T0 多 won / T1 1~2 won)。

    ⚠ 单行、无换行(聊天框换行=发送)。占位符用 <...> 让示例【不是合法 JSON】,免得回显被当答案。
    """
    return _exact_guard(account_name, website) + (
        f'For the company account "{account_name}", count its deals by OUTCOME using the CRM. '
        f'Reply with ONLY one-line JSON (no line breaks, no other text): '
        f'{{"account":"{account_name}","found":true,'
        f'"won":<number of closed-won deals>,"lost":<number of closed-lost deals>,'
        f'"open":<number of open/in-progress deals>}} '
        f'If it has no deals at all, return found:false with won/lost/open all 0.'
    )


def _has_deal_json(text: str) -> bool:
    """文本里是否已出现带 won/lost/open 键的真 JSON(轮询"数据到了"的谓词)。"""
    for obj in _iter_json_objects(_normalize_quotes(text)):
        if isinstance(obj, dict) and any(k in obj for k in ("won", "lost", "open")):
            return True
    return False


def parse_deal_summary(text: str) -> dict:
    """从 Breeze 文本挑出 deal 计数 JSON。返回 {account,found,won,lost,open};没有→found:false 全 0。"""
    def _i(v):
        try:
            return int(v)
        except Exception:
            return 0
    for obj in _iter_json_objects(_normalize_quotes(text)):
        if isinstance(obj, dict) and any(k in obj for k in ("won", "lost", "open")):
            return {"account": obj.get("account", ""),
                    "found": bool(obj.get("found", True)),
                    "won": _i(obj.get("won", 0)), "lost": _i(obj.get("lost", 0)),
                    "open": _i(obj.get("open", 0))}
    return {"account": "", "found": False, "won": 0, "lost": 0, "open": 0}


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


COMPLETION_MARKER = "Thinking complete"     # Breeze 完成推理的标记(比"稳定超时"可靠得多)


def _reset_conversation(frame, page):
    """在已有对话里就点 Back 回到空白输入(=新对话)。返回刷新后的 (frame, inp)。"""
    try:
        back = frame.locator(COPILOT_HEADER).get_by_text("Back", exact=True)
        if back.count() > 0:
            back.first.click(timeout=3000)
            page.wait_for_timeout(1200)
    except Exception:
        pass
    return _visible_input(page)


def _submit(page, frame, inp, text_to_type: str) -> None:
    """把一段文本填进输入框并发送(填 prompt / 填消歧澄清 共用)。"""
    inp.click(timeout=8000)
    page.wait_for_timeout(300)              # 稍等,让面板/输入就绪
    # ⚠ press_sequentially 会【先聚焦 locator 再逐字输入】—— 根除"没聚焦就打字导致首字符丢"的竞态
    #    (Photron 实盘:page.keyboard.type 丢了开头 "Use e" / "T")。逐字带小 delay 让 ProseMirror 跟上。
    typed = False
    try:
        inp.press_sequentially(text_to_type, delay=8, timeout=45000)
        typed = True
    except Exception:
        pass
    if not typed:                          # 兜底:老接口(尽量先聚焦)
        try:
            inp.focus(timeout=2000)
        except Exception:
            pass
        page.wait_for_timeout(150)
        page.keyboard.type(text_to_type)
    page.wait_for_timeout(900)              # 等 ProseMirror 注册文本、发送键可用
    send = frame.locator(COPILOT_SEND).first
    try:
        send.wait_for(state="visible", timeout=4000)
        if send.is_enabled():
            send.click(timeout=4000)
        else:
            page.keyboard.press("Enter")
    except Exception:
        page.keyboard.press("Enter")


def _has_outreach_answer(text: str) -> bool:
    """文本里是否已出现 outreach 答案 JSON —— 【含 found:false 的空答案也算】(genuine 无数据 = 已完成)。
    这样"真没 outreach"靠答案 JSON 认,不靠飘忽的完成标记;Breeze 慢/抖时超时不会被误当成"无数据"。"""
    for obj in _iter_json_objects(_normalize_quotes(text or "")):
        if isinstance(obj, dict) and "outreach_emails" in obj and not _is_echo(obj):
            return True
    return False


def _outreach_ready(text: str) -> bool:
    return _has_outreach_answer(text)


def _send_and_poll(page, frame, inp, prompt: str, timeout_ms: int,
                   account_name: Optional[str] = None, data_ready=None) -> tuple:
    """填 prompt、发送、轮询直到拿到答案 JSON / 完成 / 超时。返回 (原始文本, status)。

    status:
      "ok"            —— 拿到目标答案 JSON(含 found:false 的空答案)或明确完成标记;
      "disambiguation" —— 重名反问、自动澄清一次仍在问;
      "incomplete"    —— 超时还没答案、也没完成标记(多半 Breeze 慢/抖)→ 交上层【重试】,
                         【绝不】当成"真没数据"(那会误标 not_started / 无 deal)。
    """
    data_ready = data_ready or _outreach_ready
    _submit(page, frame, inp, prompt)
    prev, stable_n, waited, text = "", 0, 0, ""
    disambig_retried = False
    while waited < timeout_ms:
        page.wait_for_timeout(2000)
        waited += 2000
        try:
            text = frame.locator("body").inner_text(timeout=3000)
        except Exception:
            text = ""
        stable_n = stable_n + 1 if text == prev else 0
        if data_ready(text):
            return text, "ok"                # 拿到答案 JSON(含 found:false)→ 完成
        if COMPLETION_MARKER in text and stable_n >= 2:
            return text, "ok"                # 明确完成标记 + 稳定
        if account_name and stable_n >= 1 and looks_like_disambiguation(text):
            if not disambig_retried:         # 复核问句 → 自动澄清一次
                disambig_retried = True
                clar = (f'Use exactly "{account_name}". Do not ask again; '
                        f'if there is no exact match, return found:false.')
                _submit(page, frame, inp, clar)
                prev, stable_n, waited = "", 0, 0   # 重置,等新答复(给足新一轮超时)
                continue
            return text, "disambiguation"    # 澄清后还在问 → 认栽,带状态退出
        prev = text
    return text, "incomplete"                # 超时没答案/没完成标记 → 不完整,交上层重试


def _open_panel(page):
    """确保 Copilot 面板开着、输入框可见。返回 (frame, inp);开不出返回 (frame_or_None, None)。"""
    frame, inp = _visible_input(page)
    if inp is None:                          # 面板没开(或被搞乱)→ 点启动按钮,等输入可见
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
    return frame, inp


def _drive_breeze(page, prompt: str, data_ready, account_name: str,
                  timeout_ms: int, retries: int, logger=None) -> tuple:
    """开面板 → 新对话 → 发问 → 轮询;【不完整则重试】(每次重开面板 + 稍等)。返回 (text, status, attempts)。

    status: "ok" / "disambiguation" / "incomplete"(重试用尽仍不完整)/ "panel"(面板开不出)。
    """
    frame, inp = _open_panel(page)
    if inp is None or frame is None:
        return "", "panel", 0
    text, status, attempt = "", "incomplete", 0
    for attempt in range(retries + 1):
        frame, inp = _reset_conversation(frame, page)      # 新对话
        if inp is None:
            frame, inp = _open_panel(page)                 # 面板可能被搞乱 → 重开
            if inp is None:
                break
        text, status = _send_and_poll(page, frame, inp, prompt, timeout_ms,
                                      account_name=account_name, data_ready=data_ready)
        if status in ("ok", "disambiguation"):
            break                            # 拿到答案 / 消歧认栽 → 收
        if logger:
            logger.info("breeze | 第 %d 次不完整(Breeze 慢/抖),重试…", attempt + 1)
        page.wait_for_timeout(1500)          # incomplete → 稍等再来(重开面板重发)
    return text, status, attempt + 1


def ask_breeze(browser, account_name: str, website: Optional[str] = None,
               timeout_ms: int = 300000, retries: int = 2, logger=None) -> dict:
    """驱动 HubSpot Copilot(iframe)问一个账户的 outreach → 解析成结构化。

    每账户起【新对话】;用 "Thinking complete" 判完成(不靠稳定超时瞎猜,避免思考停顿被误判成完成);
    抽到空则**重问一次**(Breeze 偶发失败兜底),两次都空才认定真没有。
    website 有则注入 prompt 帮消歧。重名反问经安全网自动澄清;仍卡则返回带 error="needs_disambiguation"
    (【不是】空 not_started —— 见 401 教训)。返回 {"parsed","contacts","raw","attempts","error"?}。
    """
    if not account_name or str(account_name).strip() in ("", "--"):
        return {"parsed": parse_response(""), "contacts": [], "raw": "", "attempts": 0,
                "error": "invalid_account_name"}   # 导入缺名 → 别问 Breeze
    prompt = build_prompt(account_name, website=website)
    text, status, attempts = _drive_breeze(browser.page, prompt, _outreach_ready,
                                           account_name, timeout_ms, retries, logger)
    if status == "panel":
        return {"parsed": parse_response(""), "contacts": [], "raw": "", "attempts": 0,
                "error": "copilot 面板未打开/输入框不可见(启动按钮选择器?)"}
    parsed = parse_response(text)
    out = {"parsed": parsed, "contacts": emails_to_contacts(parsed), "raw": text, "attempts": attempts}
    # ⚠ status=ok = 已完成(拿到数据 或 完成标记的 genuine 无数据,如 Breeze 用散文说"没找到")→ 无 error,
    #    当 not_started 是对的。只有【超时不完整】或【消歧没解决】才标错误,交编排跳过下轮再来。
    if status == "incomplete":
        out["error"] = "breeze_incomplete"
    elif status == "disambiguation" and not _has_outreach_answer(text):
        out["error"] = "needs_disambiguation"
    if logger:
        logger.info("breeze_outreach | %s | found=%s emails=%d replied=%d attempts=%d status=%s",
                    account_name, parsed.get("found"),
                    len(parsed.get("outreach_emails", [])),
                    len(parsed.get("replied_contacts", [])), attempts, status)
    return out


def ask_deal_summary(browser, account_name: str, website: Optional[str] = None,
                     timeout_ms: int = 300000, retries: int = 2, logger=None) -> dict:
    """驱动 Breeze 数某 Core 账户的 deal(won/lost/open)。HubSpot 无原生"赢单数"列,故走 Breeze。

    won 喂 `account_grading.core_tier`(≥3=T0 / 1~2=T1);复用消歧安全网 + 不完整自动重试。
    返回 {"parsed"(=parse_deal_summary), "raw", "attempts", "error"?}。不完整→error='breeze_incomplete'。
    """
    if not account_name or str(account_name).strip() in ("", "--"):
        return {"parsed": parse_deal_summary(""), "raw": "", "attempts": 0,
                "error": "invalid_account_name"}   # 导入缺名 → 别问 Breeze
    prompt = build_deal_prompt(account_name, website=website)
    text, status, attempts = _drive_breeze(browser.page, prompt, _has_deal_json,
                                           account_name, timeout_ms, retries, logger)
    if status == "panel":
        return {"parsed": parse_deal_summary(""), "raw": "", "attempts": 0,
                "error": "copilot 面板未打开/输入框不可见"}
    parsed = parse_deal_summary(text)
    out = {"parsed": parsed, "raw": text, "attempts": attempts}
    # status=ok = 已完成(含 genuine 无 deal)→ 无 error;只有超时/消歧才标错误(见 ask_breeze 同款注释)。
    if status == "incomplete":
        out["error"] = "breeze_incomplete"
    elif status == "disambiguation" and not _has_deal_json(text):
        out["error"] = "needs_disambiguation"
    if logger:
        logger.info("breeze_deal | %s | found=%s won=%d lost=%d open=%d attempts=%d status=%s",
                    account_name, parsed.get("found"), parsed.get("won"),
                    parsed.get("lost"), parsed.get("open"), attempts, status)
    return out
