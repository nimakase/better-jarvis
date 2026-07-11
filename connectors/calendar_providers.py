"""
内置日历 —— 派生来源 provider（阶段 3）

把各功能里【已有】的日期现算成日历只读条目，不入 calendar_events 表（真源 vs 派生，
见设计文件 §2）。新功能要上日历 = 在自己模块 register_source(...)，日历核心不动；
这里集中注册四个内置来源，导入即注册（随 discover_connectors 加载）。

  - credentials_expiry：证件/银行卡有效期到期
  - documents_expiry  ：保单/合同到期
  - schedules_next_run：定时任务下次运行
  - profile_key_dates ：用户档案里的关键日期（生日/纪念日等，按年循环）

说明：
  - 「工作流周期产出」不单列 provider——工作流自身无独立日期，其周期由定时任务驱动，
    已被 schedules_next_run 覆盖，单列会重复。
  - provider 为同步函数 fn(start, end) -> list[entry]；单个 provider 抛错不影响其余
    （agenda 已对每个 provider try/except）。
"""

from __future__ import annotations

import re
from datetime import datetime, date

from dateutil.parser import isoparse

from core import calendar as _cal


# ── 日期 → 落窗条目 的纯函数助手（可单测，不依赖任何业务模块）──────────────────

def date_in_window(date_str: str, win_start: datetime, win_end: datetime,
                   *, at_hour: int = 0) -> list[datetime]:
    """一次性日期：解析 YYYY-MM-DD（或带时刻），落在 [win_start, win_end] 则返回 [dt]。"""
    if not date_str:
        return []
    try:
        d = isoparse(str(date_str).strip())
    except Exception:
        return []
    if d.hour == 0 and d.minute == 0 and at_hour:
        d = d.replace(hour=at_hour)
    return [d] if win_start <= d <= win_end else []


def yearly_in_window(month: int, day: int, win_start: datetime, win_end: datetime,
                     *, at_hour: int = 9) -> list[datetime]:
    """按年循环的关键日期（生日/纪念日）：逐年构造 month-day，落窗的都返回。"""
    out = []
    for year in range(win_start.year, win_end.year + 1):
        try:
            d = datetime(year, month, day, at_hour)
        except ValueError:
            continue  # 2/29 之类的无效年份跳过
        if win_start <= d <= win_end:
            out.append(d)
    return out


# 从自由文本里抽“关键日期”。保守匹配，宁缺毋滥：
#   1) 完整 ISO：2026-06-30 / 2026/6/30  → 一次性
#   2) 中文/数字月日：3月15日 / 03-15（无年）→ 按年循环
_ISO_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_MD_CN_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")


def extract_dates_from_text(text: str) -> list[dict]:
    """返回 [{kind:'once'|'yearly', y?,m,d}]，供 profile provider 用。"""
    found: list[dict] = []
    for m in _ISO_RE.finditer(text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            found.append({"kind": "once", "y": y, "m": mo, "d": d})
    for m in _MD_CN_RE.finditer(text):
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            found.append({"kind": "yearly", "m": mo, "d": d})
    return found


# ── provider 实现 ───────────────────────────────────────────────────────────────

def _credentials_expiry(start: datetime, end: datetime) -> list[dict]:
    from connectors import vault
    out = []
    for c in vault.list_summary():
        for dt in date_in_window(c.get("expires_at"), start, end, at_hour=9):
            out.append({
                "title": f"{c['alias']}（{c.get('type_label', '证件')}）到期",
                "start": dt.isoformat(), "kind": "expiry",
                "meta": {"alias": c["alias"], "domain": "credential"},
            })
    return out


def _documents_expiry(start: datetime, end: datetime) -> list[dict]:
    from connectors import vault
    out = []
    for d in vault.list_documents_summary():
        for dt in date_in_window(d.get("expires_at"), start, end, at_hour=9):
            out.append({
                "title": f"{d['alias']}（{d.get('type_label', '文档')}）到期",
                "start": dt.isoformat(), "kind": "expiry",
                "meta": {"alias": d["alias"], "domain": "document"},
            })
    return out


def _schedules_next_run(start: datetime, end: datetime) -> list[dict]:
    from core import scheduler
    out = []
    for s in scheduler.list_schedules():
        nr = s.get("next_run")
        if not nr or nr == "—":
            continue
        try:
            dt = isoparse(str(nr)).replace(tzinfo=None)
        except Exception:
            continue
        if start <= dt <= end:
            out.append({
                "title": f"定时任务：{s.get('description') or s['name']}",
                "start": dt.isoformat(), "kind": "task",
                "meta": {"name": s["name"], "cron": s.get("cron", "")},
            })
    return out


def _profile_key_dates(start: datetime, end: datetime) -> list[dict]:
    from core import profile
    out = []
    for f in profile.list_facts():
        text = f.get("text", "")
        for hit in extract_dates_from_text(text):
            if hit["kind"] == "once":
                dts = date_in_window(f"{hit['y']:04d}-{hit['m']:02d}-{hit['d']:02d}",
                                     start, end, at_hour=9)
            else:
                dts = yearly_in_window(hit["m"], hit["d"], start, end)
            for dt in dts:
                out.append({
                    "title": f"档案：{text[:30]}",
                    "start": dt.isoformat(), "kind": "key_date",
                    "meta": {"fact_id": f.get("id")},
                })
    return out


# ── 注册（导入即生效）───────────────────────────────────────────────────────────
_cal.register_source("credentials_expiry", _credentials_expiry)
_cal.register_source("documents_expiry", _documents_expiry)
_cal.register_source("schedules_next_run", _schedules_next_run)
_cal.register_source("profile_key_dates", _profile_key_dates)
