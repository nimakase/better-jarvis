"""prospecting/account_writer.py — 对账式写回(③)。走 HubSpot 的 bulk-edit 弹窗写属性。

设计见 docs/客户循环与Breeze设计方案.md 第六节(写回机制 + 护栏合规)。要点:
  - 【效应=write_external】。将来注册成 jarvis 工具时【必须显式声明 effect=write_external】,
    绝不能漏标(默认 write_local 会偷跳外部写与污染两道闸)。降级+Opt-Out 视为 irreversible。
  - 默认 dry_run:只走到弹窗、填好值,然后 Cancel,不点 Update;apply=True 才真写。
  - 每次写(含 dry-run)落一条 jarvis 自己的日志(CRM 外),用于 footprint 区分 +
    将来"只覆盖自己写的"。
  - dead 保护、桶划分在 account_grading;这里只负责"把某账户某属性设成某值"。
  - 应用时机 = 第一次正式夜间循环;搭建期用单账户模式先验证写入机制。

写入 DOM 落点(2026-07-26 实盘勘察确认,bulk-edit 单行):
  搜索框     input[data-test-id='crm-object-table-search-bar']
  行         tbody tr[data-test-id^='row-'] 内的 input[type=checkbox]
  选属性     [data-test-id='bulk-edit-property-select'](点开后搜属性名、点选项)
  值下拉     [data-test-id^='property-input-'](选项 role=option,文本 "5 (Hot)"/"3 (Warm)"/"1 (Cold)")
  保存       [data-test-id='bulk-actions-edit-modal-save']
  取消       button 文本 "Cancel"
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import re as _re

from prospecting import account_grading as grading

# ── Note / Task 写入落点(2026-07-26 实盘勘察)──────────────────
_PORTAL = __import__("os").environ.get("HUBSPOT_PORTAL_ID", "9311334")
_RECORD_URL = f"https://app.hubspot.com/contacts/{_PORTAL}/record/0-2/{{id}}"
NOTE_EDITOR = '[data-test-id="rte-content"]'   # 主文档;Breeze 的同名在 iframe,page.locator 不匹配
NOTE_SAVE = '[data-test-id="activity-creator-window-footer-save-button"]'
TASK_TITLE = 'textarea[placeholder="Enter your task"]'

# 唤醒天数 → HubSpot 日期下拉的相对预设文本(近似即可,避开日历)
_WAKE_PRESETS = [(3, "In 3 business days"), (7, "In 1 week"), (14, "In 2 weeks"),
                 (30, "In 1 month"), (60, "In 2 months"), (90, "In 3 months"),
                 (180, "In 6 months")]


def _wake_preset(days: int) -> str:
    return min(_WAKE_PRESETS, key=lambda t: abs(t[0] - int(days or 90)))[1]


def _record_id(browser, account_name: str, view_url: str):
    """搜账户,从名字链接取 record id。"""
    page = browser.page
    page.goto(view_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1800)
    box = page.locator(SEARCH_INPUT).first
    box.click(timeout=6000)
    box.fill("")
    box.fill(account_name)
    page.wait_for_timeout(1500)
    rows = page.locator(ROW)
    if rows.count() < 1:
        return None
    try:
        href = rows.first.locator("a[href*='/record/']").first.get_attribute("href", timeout=4000)
    except Exception:
        return None
    m = _re.search(r"/record/0-2/(\d+)", href or "")
    return m.group(1) if m else None


def write_note(browser, account_name: str, text: str, view_url: str,
               apply: bool = False, logger=None) -> dict:
    """在账户记录页写一条 Note。apply=False 只填不存(dry-run)。effect=write_external。"""
    rid = _record_id(browser, account_name, view_url)
    if not rid:
        return {"ok": False, "account": account_name, "reason": "找不到记录 id"}
    page = browser.page
    page.goto(_RECORD_URL.format(id=rid) + "?interaction=note", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    try:
        ed = page.locator(NOTE_EDITOR).first
        ed.click(timeout=8000)
        ed.fill(text)
        if apply:
            page.locator(NOTE_SAVE).click(timeout=6000)
            page.wait_for_timeout(1500)
        else:
            page.keyboard.press("Escape")   # dry-run 不存
        record_write(account_name, "note", text[:60], applied=apply, dry_run=not apply)
        _log(logger, "info", "write_note | %s | %s", account_name, "APPLIED" if apply else "dry-run")
        return {"ok": True, "account": account_name, "field": "note", "applied": apply}
    except Exception as e:
        return {"ok": False, "account": account_name, "reason": f"{type(e).__name__}: {e}"}


def create_task(browser, account_name: str, title: str, wake_days: int, view_url: str,
                apply: bool = False, logger=None) -> dict:
    """在账户记录页建一个 To-do Task,到期=最接近 wake_days 的预设。apply=False 只填不建。"""
    rid = _record_id(browser, account_name, view_url)
    if not rid:
        return {"ok": False, "account": account_name, "reason": "找不到记录 id"}
    page = browser.page
    page.goto(_RECORD_URL.format(id=rid) + "?interaction=task", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    try:
        page.locator(TASK_TITLE).first.fill(title)
        # 打开日期下拉(控件文本含 "business day")→ 选预设
        try:
            page.get_by_text(_re.compile("business day", _re.I)).first.click(timeout=4000)
            page.wait_for_timeout(400)
            page.get_by_text(_wake_preset(wake_days), exact=False).first.click(timeout=4000)
        except Exception as de:
            _log(logger, "warning", "create_task 设日期失败(用默认):%s", de)
        if apply:
            page.get_by_role("button", name=_re.compile(r"^Create$")).first.click(timeout=6000)
            page.wait_for_timeout(1500)
        else:
            page.keyboard.press("Escape")
        record_write(account_name, "task", f"{title} ({_wake_preset(wake_days)})",
                     applied=apply, dry_run=not apply)
        _log(logger, "info", "create_task | %s | %s", account_name, "APPLIED" if apply else "dry-run")
        return {"ok": True, "account": account_name, "field": "task", "applied": apply}
    except Exception as e:
        return {"ok": False, "account": account_name, "reason": f"{type(e).__name__}: {e}"}

# ── 写入 DOM 选择器(实盘验过)────────────────────────────────
SEARCH_INPUT = "input[data-test-id='crm-object-table-search-bar']"
ROW = "tbody tr[data-test-id^='row-']"
ROW_CHECKBOX = "input[type='checkbox']"
BULK_EDIT_BTN = 'button:has-text("Edit")'
PROP_SELECT = '[data-test-id="bulk-edit-property-select"]'
VALUE_INPUT = '[data-test-id^="property-input-"]'
SAVE_BTN = '[data-test-id="bulk-actions-edit-modal-save"]'
CANCEL_BTN = 'button:has-text("Cancel")'

# HubSpot 属性显示名(bulk-edit 属性搜索用)
PROP_LABEL = {"priority": "Priority", "type": "Account type"}
# 值选项的可见文本(值下拉里按文本点)
PRIORITY_OPTION = {"hot": "5 (Hot)", "warm": "3 (Warm)", "cold": "1 (Cold)"}
TYPE_OPTION = {"core": "Core", "prospecting": "Prospecting"}

# ── 危险值护栏(2026-07-26)────────────────────────────────────
# HubSpot 侧有公司工作流 "Account & Contact reset if cold or dead":账户 priority 一旦被
# 设成 cold/dead 就会触发它,把账户+联系人挪去营销账户、夺走 owner —— 不可逆。该工作流停用
# 前,jarvis【绝不自动写 cold/dead】,这类账户一律挂起交人工。工作流确认停用后,把
# BLOCK_COLD_DEAD_WRITE 设为 False 即可恢复。相关:见 grading 的 dead 保护、reply/nightly。
BLOCK_COLD_DEAD_WRITE = True
DANGEROUS_PRIORITY = {"cold", "dead"}


def _is_blocked_priority(field: str, label) -> bool:
    return (BLOCK_COLD_DEAD_WRITE and field == "priority"
            and str(label).strip().lower() in DANGEROUS_PRIORITY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_dir() -> Path:
    try:
        import config
        d = config.DATA_DIR / "customer_loop"
    except Exception:
        d = Path.home() / ".jarvis" / "customer_loop"
    d.mkdir(parents=True, exist_ok=True)
    return d


def record_write(account: str, field: str, value, applied: bool, dry_run: bool) -> None:
    """把一次写(含 dry-run)追加进 jarvis 自己的日志(CRM 外)。

    支撑:① 低调足迹的 jarvis-vs-Ned 区分;② 将来"只覆盖自己写的"(现值≠上次自己写的→退让)。
    """
    line = {"ts": _now(), "account": account, "field": field,
            "value": value, "applied": applied, "dry_run": dry_run}
    with (_log_dir() / "write_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _log(logger, level, msg, *a):
    if logger is not None:
        getattr(logger, level)(msg, *a)


def _search_and_select_one(browser, account_name: str):
    """用搜索框过滤到某账户,按【账户名精确匹配】勾选那一行。

    搜索可能命中多行(前缀/子串/母子公司),旧的"count==1 才动"会在这种情况直接放弃。
    改为在结果里按名字精确匹配定位目标行;精确命中优先,没有精确命中时仅当恰好一行才退回旧行为。
    返回 (ok: bool, candidates: list[str])。失败时 candidates 是搜到的候选全名,便于照抄。
    """
    page = browser.page
    box = page.locator(SEARCH_INPUT).first
    box.click(timeout=6000)
    box.fill("")
    box.fill(account_name)
    page.wait_for_timeout(1500)   # 等表格过滤
    rows = page.locator(ROW)
    n = rows.count()
    if n < 1:
        return False, []
    want = account_name.strip().lower()
    target, names = None, []
    for i in range(n):
        row = rows.nth(i)
        try:
            raw = (row.locator("a[href*='/record/']").first.inner_text(timeout=3000) or "").strip()
        except Exception:
            raw = ""
        names.append(raw)
        if raw.lower() == want:
            target = row
            break
    if target is None:
        if n != 1:      # 无精确命中且不止一行 → 宁可不动,别选错账户;把候选名带回去
            return False, names
        target = rows.first
    # 原生 checkbox 被样式 div 盖住,普通 click 会被拦;用 check(force) 直接设选中态。
    cb = target.locator(ROW_CHECKBOX).first
    try:
        cb.check(force=True, timeout=4000)
    except Exception:
        # 兜底:点它的包裹 label(视觉复选框)
        target.locator("label").first.click(timeout=4000)
    return True, []


def _open_bulk_edit(browser) -> None:
    browser.page.locator(BULK_EDIT_BTN).first.click(timeout=5000)
    browser.page.wait_for_timeout(600)


def _pick_property(browser, prop_label: str) -> None:
    page = browser.page
    page.locator(PROP_SELECT).click(timeout=5000)
    page.wait_for_timeout(300)
    # 属性下拉里有搜索框(打开后的可见输入框)
    search = page.locator('input[type="search"], input[placeholder="Search"]').last
    search.fill(prop_label)
    page.wait_for_timeout(500)
    page.get_by_text(prop_label, exact=False).last.click(timeout=5000)
    page.wait_for_timeout(400)


def _clear_value(browser) -> None:
    """把值下拉清空:选列表里的空选项(通常是第一项、无文本)。成败最终以 toast 为准。"""
    page = browser.page
    page.locator(VALUE_INPUT).last.click(timeout=5000)
    page.wait_for_timeout(400)
    try:
        page.get_by_role("option").first.click(timeout=3000)   # 顶部空选项 = 清空
    except Exception:
        page.keyboard.press("Escape")
    page.wait_for_timeout(300)


def _pick_value(browser, option_text: str) -> None:
    page = browser.page
    page.locator(VALUE_INPUT).last.click(timeout=5000)
    page.wait_for_timeout(300)
    page.get_by_role("option", name=option_text, exact=False).first.click(timeout=5000)
    page.wait_for_timeout(300)


def _finish(browser, apply: bool) -> str:
    """收尾。apply=True 点 Save,等弹窗关闭并读成功 toast(返回 toast 文本);dry-run 点 Cancel(返回"")。

    关键:视图属性不实时刷新,别回读行的显示值判成败;弹窗关闭 = 已提交,toast = 成功确认。
    """
    page = browser.page
    if not apply:
        page.locator(CANCEL_BTN).first.click(timeout=5000)
        page.wait_for_timeout(400)
        return ""
    page.locator(SAVE_BTN).click(timeout=6000)
    # 等 bulk-edit 弹窗的 Save 按钮消失 = 保存已提交
    try:
        page.locator(SAVE_BTN).wait_for(state="detached", timeout=8000)
    except Exception:
        pass
    # 读成功 toast:只认 "Saved changes" 文案。顶部那条 "new CRM experience" 促销 banner
    # 也是 role=alert,用泛化选择器会误抓它(实盘教训),所以按文案精确定位。
    toast = ""
    try:
        m = page.get_by_text(_re.compile(r"Saved changes", _re.I)).first
        if m.count() and m.is_visible():
            toast = (m.inner_text(timeout=1500) or "").strip()
    except Exception:
        pass
    page.wait_for_timeout(800)
    return toast


def set_property(browser, account_name: str, field: str, label: str,
                 view_url: Optional[str] = None,
                 apply: bool = False, logger: Optional[logging.Logger] = None) -> dict:
    """把某账户的 priority 或 type 设成 label。

    field: "priority"(label ∈ hot/warm/cold)或 "type"(label ∈ core/prospecting)。
    view_url: 传了就在选行前先重新导航回列表页,保证【干净起点】——多账户连写时,上一个
              账户存完弹窗/toast/选中态可能没清干净,不重导航会污染下一个账户的搜索选行。
    apply=False(默认):走到填好值就 Cancel,不落库(dry-run,验流程)。
    apply=True:点 Update 真写。
    返回 {"ok","account","field","label","applied","toast","reason"}。

    ⚠ 效应=write_external;注册工具时必须声明。type→prospecting 的降级请勿走这里自动化
      (归人工;且需配 Sequence Opt-Out)。
    """
    field = field.lower()
    # ⛔ 危险值护栏:cold/dead 会触发 HubSpot "reset if cold or dead" 工作流(挪走账户、夺 owner、
    #    不可逆)。该工作流停用前一律拦截,不碰浏览器,交人工。见 BLOCK_COLD_DEAD_WRITE。
    if _is_blocked_priority(field, label):
        _log(logger, "warning",
             "account_writer | %s priority=%s 已拦截:reset-if-cold-or-dead 工作流仍在运行",
             account_name, label)
        return {"ok": False, "account": account_name, "field": field, "label": label,
                "applied": False, "blocked": True,
                "reason": "已拦截:priority=cold/dead 会触发 HubSpot 'Account & Contact reset "
                          "if cold or dead' 工作流(挪走账户+联系人、夺 owner、不可逆);"
                          "该工作流停用前归人工"}
    if field == "priority":
        opt = PRIORITY_OPTION.get(label)
    elif field == "type":
        opt = TYPE_OPTION.get(label)
    else:
        return {"ok": False, "reason": f"未知字段 {field}"}
    if opt is None:
        return {"ok": False, "reason": f"{field} 无此值 {label}"}

    try:
        if view_url:      # 干净起点:每次写前回到列表页
            browser.page.goto(view_url, wait_until="domcontentloaded")
            browser.page.wait_for_timeout(2000)
        sel_ok, cands = _search_and_select_one(browser, account_name)
        if not sel_ok:
            hint = f";搜到的候选:{cands}(照抄全名重试)" if cands else "(0 条结果)"
            return {"ok": False, "account": account_name, "reason": f"搜索未精确命中该账户{hint}"}
        _open_bulk_edit(browser)
        _pick_property(browser, PROP_LABEL[field])
        _pick_value(browser, opt)
        toast = _finish(browser, apply)
        record_write(account_name, field, label, applied=apply, dry_run=not apply)
        _log(logger, "info", "account_writer | %s %s=%s | %s%s",
             account_name, field, label, "APPLIED" if apply else "dry-run(cancel)",
             f" | toast={toast}" if toast else "")
        return {"ok": True, "account": account_name, "field": field,
                "label": label, "applied": apply, "toast": toast}
    except Exception as e:
        # 出错尽量把弹窗关掉,避免卡住后续
        try:
            browser.page.locator(CANCEL_BTN).first.click(timeout=2000)
        except Exception:
            pass
        return {"ok": False, "account": account_name, "reason": f"{type(e).__name__}: {e}"}
    finally:
        try:  # 清掉搜索框,恢复列表
            browser.page.locator(SEARCH_INPUT).first.fill("")
            browser.page.wait_for_timeout(500)
        except Exception:
            pass


def clear_property(browser, account_name: str, field: str, view_url: Optional[str] = None,
                   apply: bool = False, logger: Optional[logging.Logger] = None) -> dict:
    """把某账户的 priority 或 type 清空(设回"无")。参数/干净起点语义同 set_property。

    主要用于探针/测试的还原。⚠ 效应=write_external。
    """
    field = field.lower()
    if field not in ("priority", "type"):
        return {"ok": False, "reason": f"未知字段 {field}"}
    try:
        if view_url:
            browser.page.goto(view_url, wait_until="domcontentloaded")
            browser.page.wait_for_timeout(2000)
        sel_ok, cands = _search_and_select_one(browser, account_name)
        if not sel_ok:
            hint = f";搜到的候选:{cands}(照抄全名重试)" if cands else "(0 条结果)"
            return {"ok": False, "account": account_name, "reason": f"搜索未精确命中该账户{hint}"}
        _open_bulk_edit(browser)
        _pick_property(browser, PROP_LABEL[field])
        _clear_value(browser)
        toast = _finish(browser, apply)
        record_write(account_name, field, "(cleared)", applied=apply, dry_run=not apply)
        _log(logger, "info", "account_writer | %s %s=CLEARED | %s%s",
             account_name, field, "APPLIED" if apply else "dry-run(cancel)",
             f" | toast={toast}" if toast else "")
        return {"ok": True, "account": account_name, "field": field,
                "label": None, "applied": apply, "toast": toast}
    except Exception as e:
        try:
            browser.page.locator(CANCEL_BTN).first.click(timeout=2000)
        except Exception:
            pass
        return {"ok": False, "account": account_name, "reason": f"{type(e).__name__}: {e}"}
    finally:
        try:
            browser.page.locator(SEARCH_INPUT).first.fill("")
            browser.page.wait_for_timeout(500)
        except Exception:
            pass
