"""prospecting/cold_start.py — 冷启动回填编排(④)。

把 ①②③ 串起来:导航到全字段视图 → 读全书分级对账(account_reader.grade_all)→
拆写入计划(account_grading.build_write_plan)→ dry-run 出全量报告 / apply 遍历自动集写回。

原则(见 docs/客户循环与Breeze设计方案.md 第九节):
  - 全量、按 create-date 视图顺序分页读(稳定,不漏不重)。
  - **只做 priority/tier 对账**;不往 HubSpot 喷历史 Note(效用低+扎眼)。
  - 自动集(fill_blank/type_promote/priority_mismatch)可写;manual_demote/skip_dead 只列不写。
  - type_promote 要写两处:先 type=core,再 priority。
  - 写入应用时机 = 第一次正式夜间循环;搭建期用 dry-run 看全量计划,apply 可加 limit 小批试。
  - 轮数(Breeze 数)是另一条较慢的 pass,不在此(保持冷启动快、纯字段)。

【效应】apply 会触发多次 write_external(经 account_writer)。注册成工具时按此声明。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from prospecting import account_grading as grading
from prospecting import account_reader as reader
from prospecting import account_writer as writer


def _store_dir() -> Path:
    try:
        import config
        d = config.DATA_DIR / "customer_loop"
    except Exception:
        d = Path.home() / ".jarvis" / "customer_loop"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _core_no_deal_review(plan: dict) -> list:
    """把所有 Core-无-deal 候选(真死号 + 暂放)汇成一份中文复核清单,最死的排前。

    降级是手动的:机器分不出"你心里的垃圾"(如 TECHNICOLOR/Aaronia 有近期活动被判暂放,
    但你认为该清)。所以给全清单 + 日期,你自己挑着降。排序:最后活动越久(或从无)越靠前。
    """
    from prospecting.account_grading import _to_date
    from datetime import date as _date

    items = []
    for it in plan.get("manual_demote", []):
        items.append((it, "真死号(老+长期无活动)"))
    for it in plan.get("recent_hold", []):
        items.append((it, "暂放(新建或近期有活动)"))

    def _sort_key(pair):
        it, _ = pair
        d = _to_date(it.get("last_activity"))
        return d or _date.min      # 无活动 → 最死,排最前

    items.sort(key=_sort_key)
    return [{
        "账户": it.get("account_name"),
        "分类": tag,
        "创建日期": it.get("create_date") or "—",
        "最后活动": it.get("last_activity") or "从无",
    } for it, tag in items]


def _save_plan(report: dict, plan: dict) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = _store_dir() / f"coldstart_plan_{stamp}.json"
    slim = {
        "generated_at": stamp,
        "total": report.get("total"),
        "bucket_counts": report.get("counts"),
        "plan_counts": plan.get("counts"),
        "auto": [{"account": x.get("account_name"), "write": x.get("write"),
                  "from": x.get("existing"), "to": x.get("computed")} for x in plan["auto"]],
        "manual_demote": [{"account": x.get("account_name"), "from": x.get("existing"),
                           "to": x.get("computed")} for x in plan["manual_demote"]],
        "recent_hold": [x.get("account_name") for x in plan.get("recent_hold", [])],
        # ★ 中文复核清单:所有 Core-无-deal(真死号+暂放),带日期、最死排前,供 Ned 手动挑降
        "复核清单_core无deal": _core_no_deal_review(plan),
    }
    path.write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _apply_auto(browser, plan: dict, logger, limit: Optional[int], view_url: Optional[str] = None) -> dict:
    """遍历自动集写回。type_promote 先写 type=core 再写 priority。返回 {ok,failed}。"""
    ok, failed = [], []
    items = plan["auto"][:limit] if limit else plan["auto"]
    total = len(items)
    for i, it in enumerate(items, 1):
        acct = it.get("account_name", "")
        wr = it.get("write") or {}
        if logger:                       # 逐个进度:失败也可见,不再"静默什么都不冒"
            logger.info("apply %d/%d → %s %s", i, total, acct, wr)
        steps_ok, reason = True, None
        if wr.get("type"):   # 升级:先落 type
            r = writer.set_property(browser, acct, "type", wr["type"], view_url=view_url, apply=True, logger=logger)
            steps_ok = r.get("ok")
            reason = r.get("reason")
        if wr.get("priority") and steps_ok:
            r = writer.set_property(browser, acct, "priority", wr["priority"], view_url=view_url, apply=True, logger=logger)
            steps_ok = r.get("ok")
            reason = r.get("reason")
        if steps_ok:
            ok.append(acct)
        else:
            failed.append({"account": acct, "write": wr, "reason": reason})
            if logger:
                logger.warning("apply 失败 %s:%s", acct, reason)
    return {"ok": ok, "failed": failed}


def run(browser, view_url: str, apply: bool = False, limit: Optional[int] = None,
        max_pages: Optional[int] = None, logger: Optional[logging.Logger] = None) -> dict:
    """冷启动主流程。apply=False 只出计划(不写);apply=True 写自动集(可 limit 小批)。"""
    browser.page.goto(view_url, wait_until="domcontentloaded")
    browser.page.wait_for_timeout(2500)

    report = reader.grade_all(browser, max_pages=max_pages)
    plan = grading.build_write_plan(report)
    plan_path = _save_plan(report, plan)

    summary = {
        "total": report["total"],
        "expected_total": report.get("expected_total"),
        "complete": report.get("complete", True),
        "bucket_counts": report["counts"],
        "plan_counts": plan["counts"],
        "plan_file": str(plan_path),
        "manual_demote": [x.get("account_name") for x in plan["manual_demote"]],
        "recent_hold": [x.get("account_name") for x in plan.get("recent_hold", [])],
        "review_count": len(plan.get("manual_demote", [])) + len(plan.get("recent_hold", [])),
        "applied": None,
    }
    if apply:
        # 读取不完整(漏读)时【拒绝写入】——宁可不写,不拿残缺数据改库。
        if not summary["complete"]:
            summary["applied"] = {"ok": [], "failed": [],
                                  "skipped": f"读取不完整(读到 {report['total']}/"
                                             f"{report.get('expected_total')}),拒绝写入,请重跑"}
        else:
            summary["applied"] = _apply_auto(browser, plan, logger, limit, view_url=view_url)
    return summary
