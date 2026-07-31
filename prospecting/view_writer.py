"""prospecting/view_writer.py — 名单法写 HubSpot 私有 view 成员。

机制(2026-07-31 实盘走通并抓准选择器,见 docs/客户循环-view管理重设计.md §2):
  前提:该 view 已建好一个 "Account name / contains exactly" 过滤器(Jarvis 只更新它的值)。
  流程:开 Advanced filters(右侧面板)→ 点值旁铅笔 → "Edit values" 文本框(每行一个名,fill 会先清空)
        → Set values → 关面板 → 点 save-view-buttons__save 存 view。

format_value_list 纯函数可单测;set_view_membership 驱动浏览器(选择器已实盘校准)。

⚠ 精度:"contains exactly" 是子串匹配(如 "Insta Elektro" 会命中 "Insta Elektro GmbH")。
   要整名精确,建议把过滤器操作符改成 "is equal to any of"(同一个铅笔/批量输入)。
"""
from __future__ import annotations


def format_value_list(names) -> str:
    """把账户名列表变成"每行一个值"的文本框内容:去空、去重(保序)、每行一个。"""
    seen, out = set(), []
    for n in names or []:
        n = " ".join(str(n or "").split()).strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return "\n".join(out)


# ── 浏览器驱动 —— 选择器实盘校准(2026-07-31)────────────────────
PANEL = '[data-selenium-test="fr-viewer-panel"]'
ADV_FILTERS_BTN = '[data-selenium-test="FiltersBar-advancedFilters"]:visible'
ADD_FILTER_BTN = '[data-selenium-test="fr-viewer-add-filter-btn"]'
PENCIL = '.multi-value-editor-btn button'          # 值旁的铅笔(图标按钮,无 test-id,靠父级 class)
SET_VALUES_BTN = 'button:has-text("Set values")'
CANCEL_BTN = 'button:has-text("Cancel")'
SAVE_VIEW_BTN = '[data-test-id="save-view-buttons__save"]'


def set_view_membership(browser, view_url: str, account_names, apply: bool = False, logger=None) -> dict:
    """把某 view 的成员设成 account_names(更新其 Account-name-contains-exactly 过滤器的值)。

    apply=False:填好值就 Cancel(dry-run,不改 view);apply=True:Set values + 存 view。
    前提:view 里已有一个 Account name 过滤器(有铅笔);没有则返回提示(需先建好,见 setup_view_filter)。
    返回 {"ok","view_url","count","applied","reason"}。
    """
    names = [n for n in (account_names or []) if str(n or "").strip()]
    payload = format_value_list(names)
    try:
        page = browser.page
        page.goto(view_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        page.locator(ADV_FILTERS_BTN).first.click(timeout=6000)   # 开右侧筛选面板
        page.wait_for_timeout(1200)

        # 面板里已存的过滤器默认是【折叠摘要态】(无铅笔)→ 点摘要展开成可编辑态,铅笔才出现。
        pencil = page.locator(f"{PANEL} {PENCIL}")
        if pencil.count() == 0:
            try:
                page.locator(PANEL).get_by_text("Account name", exact=False).first.click(timeout=4000)
                page.wait_for_timeout(1000)
            except Exception:
                pass
            pencil = page.locator(f"{PANEL} {PENCIL}")
        if pencil.count() == 0:
            return {"ok": False, "view_url": view_url, "count": len(names), "applied": apply,
                    "reason": "展开后仍无铅笔:该 view 可能没有 Account name 过滤器,先用 setup_view_filter 建好。"}
        pencil.first.click(timeout=6000)
        page.wait_for_timeout(800)

        ta = page.locator("textarea:visible").last
        ta.click(timeout=5000)
        ta.fill(payload)                      # fill 先清空再填 → 整表替换成新名单
        page.wait_for_timeout(300)

        if not apply:
            try:
                page.locator(CANCEL_BTN).first.click(timeout=3000)
            except Exception:
                pass
            if logger:
                logger.info("view_writer | dry-run | %s | %d 个账户(未 Set values)", view_url, len(names))
            return {"ok": True, "view_url": view_url, "count": len(names),
                    "applied": False, "reason": "dry-run(未 Set values)"}

        page.locator(SET_VALUES_BTN).first.click(timeout=5000)
        page.wait_for_timeout(1000)
        page.keyboard.press("Escape")         # 关筛选面板,露出 save 按钮(面板会盖住它)
        page.wait_for_timeout(500)
        page.locator(SAVE_VIEW_BTN).first.click(timeout=6000)
        page.wait_for_timeout(1200)
        if logger:
            logger.info("view_writer | APPLIED+SAVED | %s | %d 个账户", view_url, len(names))
        return {"ok": True, "view_url": view_url, "count": len(names), "applied": True}
    except Exception as e:
        return {"ok": False, "view_url": view_url, "count": len(names),
                "applied": apply, "reason": f"{type(e).__name__}: {e}"}


def setup_view_filter(browser, view_url: str, operator: str = "contains exactly",
                      apply: bool = False, logger=None) -> dict:
    """一次性给一个空 view 建好 Account name 过滤器(Add filter → Account name → 选操作符)。

    建完后成员由 set_view_membership 更新。operator 建议用 "is equal to any of"(整名精确)。
    ⚠ 选择器:Add filter=fr-viewer-add-filter-btn;属性搜索框/选项、操作符项按文案点。apply=True 才存 view。
    """
    try:
        page = browser.page
        page.goto(view_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        page.locator(ADV_FILTERS_BTN).first.click(timeout=6000)
        page.wait_for_timeout(1000)
        page.locator(ADD_FILTER_BTN).first.click(timeout=6000)
        page.wait_for_timeout(800)
        box = page.locator('input[placeholder*="Search" i]:visible').last
        box.click(timeout=5000)
        box.fill("Account name")
        page.wait_for_timeout(800)
        page.get_by_text("Account name", exact=True).last.click(timeout=5000)
        page.wait_for_timeout(800)
        # 改操作符
        page.locator(f'{PANEL} [role="button"], {PANEL} button').filter(
            has_text="is equal to any of").first.click(timeout=4000)
        page.wait_for_timeout(500)
        page.get_by_text(operator, exact=True).first.click(timeout=4000)
        page.wait_for_timeout(500)
        if apply:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            page.locator(SAVE_VIEW_BTN).first.click(timeout=6000)
            page.wait_for_timeout(1000)
        return {"ok": True, "view_url": view_url, "operator": operator, "applied": apply}
    except Exception as e:
        return {"ok": False, "view_url": view_url, "reason": f"{type(e).__name__}: {e}"}
