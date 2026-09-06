#!/usr/bin/env python3
"""
scripts/hubspot_company_export.py — 独立工具:爬取 HubSpot 公司列表视图,
导出 公司名 / 域名 / 关联 deal 数 到 CSV。

从 prospecting/hubspot_worker.py + prospecting/account_reader.py 里已经实盘
验证过的表格读取原语(虚拟滚动重叠采集 + 翻页 + 列名匹配)抽出来改写成一个
不依赖 prospecting 包的独立脚本,只需要 playwright。

复用 Jarvis 已经登录过的 Chrome persistent profile(与 prospecting 那套用同一个
目录:<系统数据目录>/Jarvis/hubspot/chrome_profile),优先 headless 直接进;
如果探测到未登录,会弹出一个有头 Chrome 窗口,等待最多 5 分钟手动登录一次。

用法:
    .venv/bin/python scripts/hubspot_company_export.py \
        --view-url "https://app.hubspot.com/contacts/9311334/objects/0-2/views/61027793/list" \
        --out workspace/results/hubspot_companies.csv

结果按页持续 flush 到 --out,过程日志写到同名 .log 文件,中途意外中断也保留
已经爬到的部分(下次可以直接重跑,已有的 CSV 会被新一轮完整覆盖——这版不做
断点续跑,量级和运行时长决定了没必要,失败了整份重跑更简单可靠)。
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import platform
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional


# ── 路径解析(与 config.DATA_DIR 逻辑一致,不导入 config 以保持完全独立)──
def _data_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home())) / "Jarvis"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Jarvis"
    return Path.home() / ".jarvis"


PROFILE_DIR = _data_dir() / "hubspot" / "chrome_profile"

# ── HubSpot 选择器(抄自 prospecting/hubspot_worker.py 已验证过的常量)────
TABLE_SELECTOR = "table[data-test-id='framework-data-table']"
ROW_SELECTOR = "tbody tr[data-test-id^='row-']"
HEADER_SELECTOR = "thead th[data-column-index]"
SEARCH_INPUT_SELECTOR = "input[data-test-id='crm-object-table-search-bar'][role='search']"
STEALTH_CHROMIUM_ARGS = ["--disable-blink-features=AutomationControlled"]

SCROLL_JS = """() => {
  const tbl = document.querySelector("table[data-test-id='framework-data-table']");
  let el = tbl;
  while (el && el.scrollHeight <= el.clientHeight + 2) el = el.parentElement;
  if (el) { el.scrollTop = el.scrollTop + Math.max(Math.floor(el.clientHeight*0.6), 150); return true; }
  window.scrollBy(0, 500); return false;
}"""

COUNT_JS = r"""() => {
  const t = document.body.innerText || '';
  let m = t.match(/of\s+([\d,]+)\b/i) || t.match(/([\d,]+)\s+records?\b/i);
  return m ? parseInt(m[1].replace(/,/g,''),10) : null;
}"""

# 逻辑字段 -> 表头候选(小写子串匹配,容忍不同措辞)
COLUMN_CANDIDATES: Dict[str, List[str]] = {
    "company_name": ["company name", "account name"],
    "domain": ["company domain name", "domain name", "website url", "domain"],
    "num_associated_deals": ["number of associated deals", "associated deals"],
}
REQUIRED_FIELDS = ["company_name", "domain", "num_associated_deals"]

MAX_PAGES_SAFETY_CAP = 500   # 硬上限保护,正常几千家几十页就够


def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())


def _norm_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _replace_punct(text: str) -> str:
    return re.sub(r"[\-_/,.]", " ", text or "")


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("hubspot_company_export")
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    sh = logging.StreamHandler()
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


class Browser:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.playwright = None
        self.context = None
        self.page = None
        self.column_index_map: Dict[str, str] = {}

    def launch(self, headless: bool) -> None:
        from playwright.sync_api import sync_playwright
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        last_exc = None
        for attempt in range(1, 4):
            pw = sync_playwright().start()
            try:
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    channel="chrome",
                    headless=headless,
                    ignore_default_args=["--enable-automation"],
                    args=STEALTH_CHROMIUM_ARGS,
                    viewport={"width": 1440, "height": 960},
                )
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.set_default_timeout(30000)
                page.set_default_navigation_timeout(60000)
                self.playwright, self.context, self.page = pw, ctx, page
                return
            except Exception as e:
                last_exc = e
                try:
                    pw.stop()
                except Exception:
                    pass
                if attempt < 3:
                    time.sleep(2.5 * attempt)
        raise RuntimeError(f"启动 Chrome 失败(profile 可能被别的工具占用): {last_exc}")

    def close(self) -> None:
        for obj, fn in ((self.context, "close"), (self.playwright, "stop")):
            try:
                if obj is not None:
                    getattr(obj, fn)()
            except Exception:
                pass
        self.context = self.playwright = self.page = None

    def detect_auth_state(self) -> str:
        try:
            url = (self.page.url or "").lower()
        except Exception:
            url = ""
        if "/login" in url:
            return "login_page"
        try:
            search_count = self.page.locator(SEARCH_INPUT_SELECTOR).count()
            table_count = self.page.locator(TABLE_SELECTOR).count()
        except Exception:
            return "unknown"
        if search_count > 0 and (table_count > 0 or self._has_empty_state()):
            return "ok"
        return "unknown"

    def _has_empty_state(self) -> bool:
        try:
            return self.page.locator(
                "text=/No results|No companies|No matching records/i").count() > 0
        except Exception:
            return False

    def wait_for_auth_ready(self, timeout_seconds: int) -> str:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            state = self.detect_auth_state()
            if state in ("ok", "login_page"):
                return state
            time.sleep(0.5)
        return "unknown"

    def table(self):
        return self.page.locator(TABLE_SELECTOR).first

    def rows(self):
        return self.table().locator(ROW_SELECTOR)

    def _label_key(self, text: str) -> str:
        return _norm_spaces(_replace_punct(text.lower()))

    def refresh_column_index_map(self) -> None:
        headers = self.table().locator(HEADER_SELECTOR)
        count = headers.count()
        mapping: Dict[str, str] = {}
        for i in range(count):
            th = headers.nth(i)
            try:
                idx = th.get_attribute("data-column-index")
                label = th.inner_text(timeout=600)
            except Exception:
                continue
            if idx and label:
                mapping[self._label_key(label)] = idx
        self.column_index_map = mapping


def match_columns(column_index_map: Dict[str, str]) -> Dict[str, str]:
    norm_map = {_norm_label(k): v for k, v in column_index_map.items()}
    out: Dict[str, str] = {}
    for field, candidates in COLUMN_CANDIDATES.items():
        for nlabel, idx in norm_map.items():
            if any(_norm_label(c) in nlabel for c in candidates):
                out[field] = idx
                break
    return out


def wait_for_table_ready(page, timeout_ms: int = 30000) -> bool:
    try:
        page.locator(TABLE_SELECTOR).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        pass
    waited = 0
    while waited < timeout_ms:
        try:
            cnt = page.evaluate(COUNT_JS)
        except Exception:
            cnt = None
        try:
            rows = page.locator(f"{TABLE_SELECTOR} tbody tr").count()
        except Exception:
            rows = 0
        if cnt or rows > 0:
            page.wait_for_timeout(1200)
            return True
        page.wait_for_timeout(500)
        waited += 500
    return False


def _cell_text(row, idx: str) -> str:
    try:
        loc = row.locator(f"td[data-column-index='{idx}']").first
        if loc.count() == 0:
            return ""
        return (loc.inner_text(timeout=800) or "").strip()
    except Exception:
        return ""


def _row_id(row) -> str:
    try:
        return row.get_attribute("data-test-id") or ""
    except Exception:
        return ""


def _clean(text: str) -> Optional[str]:
    t = (text or "").strip()
    return None if t in ("", "--") else t


def parse_int_cell(text: str) -> int:
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else 0


def _read_raw(row, cols: Dict[str, str]) -> Dict[str, str]:
    return {f: _cell_text(row, idx) for f, idx in cols.items()}


def _row_complete(raw: Dict[str, str], cols: Dict[str, str]) -> bool:
    # 关键列任一还是空串 "" 说明这行还在渲染,跳过靠下一轮重叠滚动补——
    # "--"/"0" 是 HubSpot 真实的空值渲染,不算未完成。
    return all(raw.get(f, "") != "" for f in cols)


def _harvest(browser: Browser, cols: Dict[str, str], acc: Dict[str, dict]) -> None:
    rows = browser.rows()
    for i in range(rows.count()):
        row = rows.nth(i)
        rid = _row_id(row)
        if not rid or rid in acc:
            continue
        raw = _read_raw(row, cols)
        if not _row_complete(raw, cols):
            continue
        name = _clean(raw.get("company_name", ""))
        if not name:
            continue
        acc[rid] = {
            "company_name": name,
            "domain": _clean(raw.get("domain", "")) or "",
            "num_associated_deals": parse_int_cell(raw.get("num_associated_deals", "")),
        }


def read_page(browser: Browser, cols: Dict[str, str]) -> List[dict]:
    acc: Dict[str, dict] = {}
    stagnant = 0
    for _ in range(240):
        _harvest(browser, cols, acc)
        prev = len(acc)
        browser.page.evaluate(SCROLL_JS)
        browser.page.wait_for_timeout(650)
        _harvest(browser, cols, acc)
        if len(acc) == prev:
            stagnant += 1
            if stagnant >= 4:
                break
        else:
            stagnant = 0
    return list(acc.values())


def goto_next_page(browser: Browser) -> bool:
    page = browser.page
    for sel in ('button:has-text("Next"):not([disabled])',
                '[aria-label="Next"]:not([disabled])',
                'button[data-test-id="pagination-next"]:not([disabled])'):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_enabled():
                loc.click(timeout=4000)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


def _acquire_session(view_url: str, logger: logging.Logger) -> Browser:
    browser = Browser(logger)
    try:
        browser.launch(headless=True)
        browser.page.goto(view_url, wait_until="domcontentloaded")
        state = browser.wait_for_auth_ready(timeout_seconds=45)
        if state == "ok":
            logger.info("headless 已登录,直接进入视图")
            return browser
        logger.info("headless 探测到未登录(state=%s),切换有头窗口等待手动登录", state)
    except Exception as e:
        logger.warning("headless 阶段失败: %s", e)
    browser.close()

    time.sleep(2.5)
    browser = Browser(logger)
    browser.launch(headless=False)
    browser.page.goto(view_url, wait_until="domcontentloaded")
    try:
        browser.page.bring_to_front()
    except Exception:
        pass
    logger.info("已打开有头浏览器窗口,等待手动登录(上限 300 秒)")
    deadline = time.time() + 300
    while time.time() < deadline:
        if browser.detect_auth_state() == "ok":
            logger.info("检测到登录成功")
            return browser
        time.sleep(2)
    browser.close()
    raise RuntimeError("已等待 300 秒仍未检测到登录成功")


def crawl(view_url: str, out_path: Path, logger: logging.Logger,
          max_pages: Optional[int] = None) -> dict:
    browser = _acquire_session(view_url, logger)
    try:
        logger.info("已进入视图,等待表格渲染...")
        wait_for_table_ready(browser.page)
        browser.refresh_column_index_map()
        cols = match_columns(browser.column_index_map)
        logger.info("识别到列: %s", cols)
        logger.info("视图原始表头: %s", list(browser.column_index_map.keys()))
        missing = [f for f in REQUIRED_FIELDS if f not in cols]
        if missing:
            raise RuntimeError(
                f"视图缺少必要列: {missing};已识别到的列: {list(cols.keys())}"
                f";请去 HubSpot 该视图点 Edit columns 把这些列加上再重跑。")

        try:
            expected = browser.page.evaluate(COUNT_JS)
        except Exception:
            expected = None
        logger.info("页面显示总记录数: %s", expected)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["company_name", "domain", "num_associated_deals"]
        seen_names: set = set()
        total = 0
        page_cap = max_pages if max_pages is not None else MAX_PAGES_SAFETY_CAP
        with out_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            page_no = 0
            while True:
                page_rows = read_page(browser, cols)
                new_rows = [r for r in page_rows if r["company_name"] not in seen_names]
                for r in new_rows:
                    seen_names.add(r["company_name"])
                    writer.writerow(r)
                f.flush()
                total += len(new_rows)
                page_no += 1
                logger.info("第 %d 页读完,本页新增 %d 条,累计 %d 条", page_no, len(new_rows), total)
                if page_no >= page_cap:
                    logger.warning("达到页数上限 %d,提前停止(可能没读全)", page_cap)
                    break
                if not goto_next_page(browser):
                    logger.info("没有下一页了,读取结束")
                    break

        return {"total": total, "expected_total": expected, "out_path": str(out_path)}
    finally:
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="爬取 HubSpot 公司列表视图,导出 company_name/domain/num_associated_deals 到 CSV")
    ap.add_argument("--view-url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()

    out_path = Path(args.out)
    log_path = out_path.with_suffix(".log")
    logger = setup_logger(log_path)
    logger.info("开始爬取: %s", args.view_url)
    try:
        result = crawl(args.view_url, out_path, logger, max_pages=args.max_pages)
        logger.info("完成: %s", result)
        print("DONE", result)
    except Exception as e:
        logger.exception("爬取失败: %s", e)
        print("FAILED", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
