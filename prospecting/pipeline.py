"""
潜客流水线 · 富化与排序层（Prospect pipeline enrichment + ranking）

把潜客「富记录」逐条过 HubSpot 匹配 → 派生 crm_state → 算 weight=意向×CRM系数
→ 排序（已认领置底）→ 输出富 xlsx。

契约见 intel/prospect_pipeline_contract.md。

匹配函数可注入（match_fn）：
  - 生产：make_hubspot_match_fn(browser) 包住爬虫内核 process_one。
  - 测试：传一个 mock，返回 {"status":..., "owner":...}，无需浏览器。

本模块只用标准库；写 xlsx 时才惰性 import openpyxl。
"""
from __future__ import annotations

from typing import Callable, Optional

# CRM 系数（contract §4）
CRM_MULTIPLIER = {
    "new": 1.0,        # HubSpot 里没有 → 全新潜客
    "unowned": 0.7,    # 在 CRM 但没人认领
    "review": 0.6,     # 多重命中，待人工核
    "owned": 0.3,      # 已被销售认领 —— 保留但压底
    "unknown": 0.5,    # 匹配未能确定
}

# 意向分 → 等级阈值（contract §4，可调）
TIER_A_MIN = 8.0
TIER_B_MIN = 3.0

# 无命中信号时的意向基线
INTENT_BASELINE = 1.0

MatchFn = Callable[[str, str], dict]


# ────────────────────────── 派生与计算 ──────────────────────────

def derive_crm_state(status: Optional[str], owner: Optional[str]) -> str:
    """把 matcher 返回的 status/owner 映射成 crm_state（contract §3）。"""
    owner = (owner or "").strip()
    if status == "matched":
        return "owned" if owner else "unowned"
    if status == "matched_owner_empty":
        return "unowned"
    if status == "no_match":
        return "new"
    if status == "multiple_exact_matches":
        return "review"
    return "unknown"


def intent_tier(score: float) -> str:
    if score >= TIER_A_MIN:
        return "A"
    if score >= TIER_B_MIN:
        return "B"
    return "C"


def compute_weight(intent_score: float, crm_state: str) -> float:
    return round(intent_score * CRM_MULTIPLIER.get(crm_state, 0.5), 3)


# ────────────────────────── 富化主流程 ──────────────────────────

# 最终富记录的列顺序（contract §2）
COLUMNS = [
    "rank", "weight", "intent_tier", "intent_score", "crm_state",
    "hubspot_status", "hubspot_owner", "track",
    "company_name", "website", "country", "components",
    "surplus_signals", "seen_before", "contact_rationale", "sources",
]


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return str(value)


def enrich_records(records: list[dict], match_fn: MatchFn) -> list[dict]:
    """逐条匹配 + 富化 + 排序。返回排好序、带 rank 的富记录列表。

    输入 record 至少含 company_name；website/domain 用于匹配；
    可含 intent_score / surplus_signals / track / components / 等（来自生成层）。
    """
    enriched: list[dict] = []
    for r in records:
        company = (r.get("company_name") or "").strip()
        domain = (r.get("website") or r.get("domain") or "").strip()

        try:
            m = match_fn(company, domain) or {}
        except Exception as exc:  # 单条匹配失败不应中断整批
            m = {"status": "error", "owner": "", "error": str(exc)}

        status = m.get("status")
        owner = m.get("owner") or ""
        crm = derive_crm_state(status, owner)

        intent = r.get("intent_score")
        if intent is None:
            intent = INTENT_BASELINE
        intent = round(float(intent), 2)

        out = dict(r)
        out.update({
            "hubspot_status": status,
            "hubspot_owner": owner,
            "crm_state": crm,
            "intent_score": intent,
            "intent_tier": intent_tier(intent),
            "weight": compute_weight(intent, crm),
        })
        enriched.append(out)

    # 排序：已认领整体置底；各组内按 weight 降序（contract §4「保留但压底」）
    enriched.sort(key=lambda x: (1 if x["crm_state"] == "owned" else 0, -x["weight"]))
    for i, rec in enumerate(enriched, 1):
        rec["rank"] = i
    return enriched


# ────────────────────────── 输出富 xlsx ──────────────────────────

def write_xlsx(records: list[dict], path: str) -> str:
    """把富记录写成 xlsx（列见 COLUMNS）。已认领行浅灰底以示压底。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "prospects"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1A3A5C")
    owned_fill = PatternFill("solid", fgColor="EEEEEE")

    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(vertical="center")

    for rec in records:
        ws.append([_as_text(rec.get(col)) for col in COLUMNS])
        if rec.get("crm_state") == "owned":
            for c in ws[ws.max_row]:
                c.fill = owned_fill

    widths = {"company_name": 28, "website": 22, "contact_rationale": 50,
              "surplus_signals": 34, "sources": 30, "components": 18, "country": 14}
    for i, col in enumerate(COLUMNS, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = widths.get(col, 12)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    wb.save(path)
    return path


# ────────────────────────── 生产：包住爬虫内核 ──────────────────────────

def make_hubspot_match_fn(browser, logger=None) -> MatchFn:
    """生产用：把已 start() 的 HubSpotBrowser 包成 match_fn。

    惰性 import hubspot_worker（依赖 playwright/pandas），测试路径不触发。
    """
    from prospecting import hubspot_worker as w

    def match(company: str, domain: str) -> dict:
        outcome = w.process_one(browser, company, domain)
        return {
            "status": outcome.status,
            "owner": outcome.owner,
            "company_search_result_count": outcome.company_search_result_count,
            "domain_search_result_count": outcome.domain_search_result_count,
        }

    return match
