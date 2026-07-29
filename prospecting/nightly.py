"""prospecting/nightly.py — 夜间循环编排骨架(⑤)。

首跑:冷启动对账计划全量应用(cold_start.run apply)+ 标记「已应用」。
之后:增量——只碰"昨天动过的"(变化检测 + 分层)。当前增量为【骨架/桩】,
待回复路由器 + 三层落地后填充;稳态写入用 store.should_auto_write 守卫防误伤人手改动。

【效应】apply 会触发多次 write_external。注册成 jarvis 工具/工作流时必须按此声明;
且 controller / 工具注册属 PROTECTED,需 Ned 人工审——本模块只是「逻辑在外」的胶水。
"""
from __future__ import annotations

import logging
from typing import Optional

from prospecting import cold_start
from prospecting import customer_loop_store as store


def run(browser, view_url: str, apply: bool = False,
        logger: Optional[logging.Logger] = None) -> dict:
    """夜间循环入口。首跑走冷启动,之后走增量。"""
    if not store.is_coldstart_applied():
        summary = cold_start.run(browser, view_url, apply=apply, logger=logger)
        if apply:
            store.mark_coldstart_applied(summary.get("plan_counts"))
        store.record_run("coldstart", {"applied": apply, "counts": summary.get("plan_counts")})
        return {"phase": "coldstart", "applied": apply, "summary": summary}

    result = _incremental(browser, view_url, logger, apply=apply)
    store.record_run("incremental", result)
    return {"phase": "incremental", **result}


def _incremental(browser, view_url: str, logger, apply: bool) -> dict:
    """增量夜间作业。已实现【稳态 priority 维护】;回复路/诊断留后续。

    稳态 priority 维护:重算全书(约 55s,便宜),只更新"变了 + 贾维斯自己写过(或仍空白)"
    的 priority——经 store.should_auto_write 守卫,人手改过的一律退让不动。冷启动已把大多数
    刷成 match,所以每晚真正要写的只是"昨天后热度变了的"那几个。

    TODO(留后续、多依赖回复路由器 + 三层 + PROTECTED 污染闸):
       - 回复路由器:Breeze 读新回信 → 分级 → 拟 Note/Task 进日报「待确认」(方案 a);
       - ongoing deal 往来总结、closed-won 复购、sequence 轮数诊断。
    """
    from prospecting import account_grading as grading
    from prospecting import account_reader as reader
    from prospecting import account_writer as writer

    browser.page.goto(view_url, wait_until="domcontentloaded")
    browser.page.wait_for_timeout(2500)

    report = reader.grade_all(browser)
    if not report.get("complete", True):
        # 读取不完整 → 不写,免得拿残缺数据改库。
        return {"note": "读取不完整,跳过本次写入(重跑)", "changed": 0,
                "read": f"{report['total']}/{report.get('expected_total')}", "applied": apply}
    # ── 池成员变动检测:完整读取后,比对当前池 vs 上次快照,揪出被 reassign/reset 工作流
    #    悄悄挪走的账户(left)和新进池的(entered),列进夜报;再落新快照供下次比对。
    _pool_now = [r.get("account_name", "") for r in report.get("records", [])]
    pool_diff = store.diff_pool(_pool_now)
    store.save_pool_snapshot(_pool_now)

    plan = grading.build_write_plan(report)

    updated, flagged, held = [], [], []
    for it in plan.get("auto", []):
        wr = it.get("write") or {}
        if "priority" not in wr:      # 稳态只维护 priority;type 升级/降级另论
            continue
        acct = it.get("account_name", "")
        current = (it.get("existing") or {}).get("priority")
        computed = wr["priority"]
        # ⛔ cold/dead 会触发 HubSpot "reset if cold or dead" 工作流(挪走账户、不可逆)→ 不自动写,
        #    挂起交人工。writer 里还有一道硬护栏兜底;这里提前挡下,报告里明确列出。
        if writer._is_blocked_priority("priority", computed):
            held.append({"账户": acct, "规则算的": computed, "现值": current,
                         "原因": "cold/dead 触发 reset 工作流,挂起交人工"})
            continue
        if store.should_auto_write(acct, "priority", current):
            ok = True
            if apply:
                r = writer.set_property(browser, acct, "priority", computed,
                                        view_url=view_url, apply=True, logger=logger)
                ok = bool(r.get("ok"))
                if not ok:
                    flagged.append({"账户": acct, "规则算的": computed,
                                    "写入失败": r.get("reason")})
            if ok:
                updated.append(acct)
        else:
            # 守卫拦下(你手动设过、且与算出的不一致)→ 不改,只【提出分歧】给你看。
            # 这是配合:尊重你的值,但不当没看见——摆到报告里,realign 与否你定。
            flagged.append({"账户": acct, "你设的": current, "规则算的": computed})

    # ── 回复层:找"最近有回信"的候选 → Breeze 分类 → 路由出提议 → 存待确认(不写)──
    from prospecting import reply_path, reply_classify
    reply_res = {"candidates": 0, "proposals": []}
    try:
        def _classify(acct):
            return reply_classify.classify(browser.page, acct, logger=logger)
        reply_res = reply_path.run(report.get("records", []), _classify)
    except Exception as e:      # 回复层失败不该拖垮整个夜跑
        if logger:
            logger.warning("回复层失败(跳过):%s", e)

    return {
        "note": "稳态 priority 维护 + 回复层",
        "changed": len(updated),
        "updated": updated[:50],
        "分歧提示": flagged,          # 你手动值 ≠ 规则值:只提示不改
        "flagged_count": len(flagged),
        "挂起_cold_dead": held,       # ⛔ 算到 cold/dead 的:护栏拦下,交人工(reset 工作流未停用前)
        "held_count": len(held),
        "池_离开": pool_diff.get("left", []),      # 上次在、这次没了:被 reassign/reset 挪走 or 手动放弃
        "池_进入": pool_diff.get("entered", []),   # 新进池
        "池大小": pool_diff.get("curr_count"),
        "reply_candidates": reply_res.get("candidates", 0),
        "new_proposals": len(reply_res.get("proposals", [])),
        "applied": apply,
    }
