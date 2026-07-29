"""prospecting/account_reader.py — 从 HubSpot 账户列表读出分级所需字段(②b)。

复用 HubSpotBrowser 的表读取原语(column_index_map + rows() + td[data-column-index]),
把每行映射成 account_grading.classify() 的输入 + 现有 type/priority(供对账)。

分两层:
  - 纯解析(match_columns / parse_*):零 DOM,沙箱可单测。
  - DOM 读取(read_page / read_all / grade_all):吃 HubSpotBrowser,需本机实测。

【只读】。写回(对账后落值)是 ③,不在这里。

依赖前提:当前视图需含这些列(缺哪列会明确报出来让 Ned 去 Edit columns 加):
  Account name / Account type / Priority / Number of Associated Deals /
  Number of open deals / Last activity date / Last Engagement Date
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from prospecting import account_grading as grading

# 逻辑字段 → 表头 label 候选(小写子串匹配,容忍 HubSpot 的具体措辞)
COLUMN_CANDIDATES: Dict[str, List[str]] = {
    "account_name": ["account name"],
    "existing_type": ["account type"],
    "existing_priority": ["priority"],
    "num_associated_deals": ["number of associated deals", "associated deals"],
    "num_open_deals": ["number of open deals", "open deals"],
    "last_activity_date": ["last activity date", "last activity"],
    "last_engagement_date": ["last engagement date", "last engagement"],
    "create_date": ["create date"],   # 可选:分批游标用
}

# classify + reconcile 真正需要的(缺了没法分级)
REQUIRED_FIELDS = [
    "existing_type", "existing_priority",
    "num_associated_deals", "num_open_deals",
    "last_activity_date", "last_engagement_date",
]


def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())


def match_columns(column_index_map: Dict[str, str]) -> Dict[str, str]:
    """把 HubSpotBrowser.column_index_map(label→idx)匹配成 逻辑字段→idx。

    column_index_map 的 key 可能是原始 label,也可能已归一;这里两头都归一后子串匹配。
    返回只含匹配到的字段;调用方用 missing_required() 检查缺漏。
    """
    norm_map = {_norm_label(k): v for k, v in column_index_map.items()}
    out: Dict[str, str] = {}
    for field, candidates in COLUMN_CANDIDATES.items():
        for nlabel, idx in norm_map.items():
            if any(_norm_label(c) in nlabel for c in candidates):
                out[field] = idx
                break
    return out


def missing_required(cols: Dict[str, str]) -> List[str]:
    return [f for f in REQUIRED_FIELDS if f not in cols]


# ── 单元格解析(纯)────────────────────────────────────────────
def parse_type(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    if "core" in t:
        return "core"
    if "prospect" in t:
        return "prospecting"
    return None


def parse_priority(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    if not t or t == "--":
        return None
    for label in ("hot", "warm", "cold", "dead"):
        if label in t:
            return label
    # 兜底:纯数字 → 反查 5/3/1/0
    m = re.search(r"\d+", t)
    if m:
        val = int(m.group())
        for label, v in grading.PRIORITY_VALUE.items():
            if v == val:
                return label
    return None


def parse_int_cell(text: str) -> int:
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else 0


# ── DOM 读取(需 HubSpotBrowser;本机实测)──────────────────────
def _cell_text(row, idx: str) -> str:
    try:
        loc = row.locator(f"td[data-column-index='{idx}']").first
        if loc.count() == 0:
            return ""
        return (loc.inner_text(timeout=800) or "").strip()
    except Exception:
        return ""


# 判"渲染完整"要看的列:HubSpot 真空值渲染成 "--"/"0",没渲染好才是空串 ""。
# 任一为空串 → 行还在加载,跳过、靠重叠滚动下一轮再读。account_name 不列入(有账户本就无名)。
_COMPLETE_FIELDS = ["existing_type", "existing_priority", "num_associated_deals",
                    "num_open_deals", "last_activity_date", "last_engagement_date", "create_date"]


def _read_raw(row, cols: Dict[str, str]) -> Dict[str, str]:
    """一次读出该行所有列的原始文本(供完整性判定 + 解析共用,避免重复读单元格)。"""
    return {f: _cell_text(row, idx) for f, idx in cols.items()}


def _row_complete(raw: Dict[str, str], cols: Dict[str, str]) -> bool:
    return all(raw.get(f, "") != "" for f in _COMPLETE_FIELDS if f in cols)


def _parse_raw(raw: Dict[str, str]) -> dict:
    return {
        "account_name": raw.get("account_name", ""),
        "existing_type": parse_type(raw.get("existing_type", "")),
        "existing_priority": parse_priority(raw.get("existing_priority", "")),
        "num_associated_deals": parse_int_cell(raw.get("num_associated_deals", "")),
        "num_open_deals": parse_int_cell(raw.get("num_open_deals", "")),
        "last_activity_date": raw.get("last_activity_date") or None,
        "last_engagement_date": raw.get("last_engagement_date") or None,
        "create_date": raw.get("create_date") or None,
    }


def _row_id(row) -> str:
    try:
        return row.get_attribute("data-test-id") or ""
    except Exception:
        return ""


# 滚动表格的滚动容器(HubSpot 表格【虚拟滚动】,DOM 一次只渲染约 50 行)。
# 每次只滚 60% 视高 → 相邻两屏重叠,任一行至少进两次视野,给它一次"已渲染完再被读"的机会。
_SCROLL_JS = """() => {
  const tbl = document.querySelector("table[data-test-id='framework-data-table']");
  let el = tbl;
  while (el && el.scrollHeight <= el.clientHeight + 2) el = el.parentElement;
  if (el) { el.scrollTop = el.scrollTop + Math.max(Math.floor(el.clientHeight*0.6), 150); return true; }
  window.scrollBy(0, 500); return false;
}"""

# 读页面上的记录总数,用于读完对账、发现漏读。多形态兜底:"535 records" / "1-100 of 535"。
_COUNT_JS = r"""() => {
  const t = document.body.innerText || '';
  let m = t.match(/of\s+([\d,]+)\b/i) || t.match(/([\d,]+)\s+records?\b/i);
  return m ? parseInt(m[1].replace(/,/g,''),10) : null;
}"""


def _harvest(browser, cols, acc: dict) -> None:
    """收当前视野里【渲染完整】的新行(关键列无空串);半渲染的跳过,靠 60% 重叠滚动下轮再读。"""
    rows = browser.rows()
    for i in range(rows.count()):
        row = rows.nth(i)
        rid = _row_id(row)
        if not rid or rid in acc:
            continue
        raw = _read_raw(row, cols)
        if _row_complete(raw, cols):
            acc[rid] = _parse_raw(raw)


def read_page(browser, cols: Dict[str, str]) -> List[dict]:
    """读当前页所有行(虚拟滚动:重叠滚动 + 只收渲染完整的行 + 去重,滚到底不再增长即止)。"""
    acc: Dict[str, dict] = {}
    stagnant = 0
    for _ in range(240):   # 上限保护(100/页,重叠滚动步子小,给足次数)
        _harvest(browser, cols, acc)
        prev = len(acc)
        browser.page.evaluate(_SCROLL_JS)
        browser.page.wait_for_timeout(650)   # 等行渲染
        _harvest(browser, cols, acc)
        if len(acc) == prev:
            stagnant += 1
            if stagnant >= 4:      # 连续 4 次重叠滚动无新行 → 到底了
                break
        else:
            stagnant = 0
    return list(acc.values())


def _goto_next_page(browser) -> bool:
    """点「Next」翻下一页;没有可点的下一页返回 False。⚠ 分页选择器需本机核。"""
    page = browser.page
    for sel in ('button:has-text("Next"):not([disabled])',
                '[aria-label="Next"]:not([disabled])',
                'button[data-test-id="pagination-next"]:not([disabled])'):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_enabled():
                loc.click(timeout=4000)
                page.wait_for_timeout(1200)   # 等新页渲染
                return True
        except Exception:
            continue
    return False


def read_all(browser, max_pages: Optional[int] = None) -> List[dict]:
    """翻页读全量。max_pages=None 读到底;给个数字可限页(首次本机验证建议先限 1-2 页)。"""
    browser.refresh_column_index_map(with_retry=True, context="account_reader")
    cols = match_columns(browser.column_index_map)
    miss = missing_required(cols)
    if miss:
        raise RuntimeError(
            "当前视图缺少必要列,去 HubSpot 用 Edit columns 加上再跑。缺:"
            + ", ".join(miss)
            + f"（已识别到的列:{list(cols.keys())}）"
        )
    try:
        expected = browser.page.evaluate(_COUNT_JS)   # 页面上的 "X records" 总数
    except Exception:
        expected = None
    all_rows: List[dict] = []
    page_no = 0
    while True:
        all_rows.extend(read_page(browser, cols))
        page_no += 1
        if max_pages is not None and page_no >= max_pages:
            break
        if not _goto_next_page(browser):
            break
    return {"rows": all_rows, "expected_total": expected, "read_total": len(all_rows)}


def grade_all(browser, max_pages: Optional[int] = None) -> dict:
    """读全量 → 逐账户 classify + reconcile → 汇成对账报告(不写)。含读取完整性对账。"""
    res = read_all(browser, max_pages=max_pages)
    records = res["rows"]
    buckets: Dict[str, list] = {
        grading.BUCKET_MATCH: [], grading.BUCKET_FILL_BLANK: [],
        grading.BUCKET_PRIORITY_MISMATCH: [],
        grading.BUCKET_TYPE_PROMOTE: [], grading.BUCKET_TYPE_DEMOTE: [],
        grading.BUCKET_TYPE_DEMOTE_RECENT: [],
        grading.BUCKET_SKIP_DEAD: [],
    }
    for rec in records:
        computed = grading.classify(rec)
        existing = {"type": rec["existing_type"], "priority": rec["existing_priority"]}
        r = grading.reconcile(computed, existing, created=rec.get("create_date"),
                              last_activity=rec.get("last_activity_date"))
        r["account_name"] = rec.get("account_name", "")
        r["create_date"] = rec.get("create_date")
        r["last_activity"] = rec.get("last_activity_date")
        buckets[r["bucket"]].append(r)
    expected = res.get("expected_total")
    complete = (max_pages is not None) or (expected is None) or (len(records) >= expected)
    return {
        "total": len(records),
        "counts": {k: len(v) for k, v in buckets.items()},
        "buckets": buckets,
        "expected_total": expected,     # 页面 "X records" 总数(全量时应等于 total)
        "complete": complete,           # False = 有漏读,别拿去写(重跑)
        "records": records,             # 原始读取记录(回复路变化检测要用 last_engagement)
    }
