"""prospecting/reply_path.py — 回复路编排(变化检测 → Breeze 分类 → 路由提议)。

设计见 docs/客户循环与Breeze设计方案.md 第五节②。流程:
  1) 变化检测:找"最近有 engagement(可能有新回信)"的账户——一小撮,别全书扫;
  2) 对每个:Breeze 读最新回信分类(reply_classify.classify);
  3) 非 none 且【这条回信没处理过】→ reply_router.route 出提议;
  4) 提议存进"待确认"库(save_proposals),进日报等 Ned 批准才写。

【护栏】Breeze 读客户邮件=不可信。本模块只【生成 + 存提议(写本机)】,绝不对外写——
真写发生在 Ned 批准的干净新回合。故不碰 trust.py(污染闸天然满足)。

classify_fn / is_processed / mark_processed 都可注入 → 纯编排逻辑可脱离浏览器单测。
"""
from __future__ import annotations

from typing import Callable, List, Optional

from prospecting import reply_router
from prospecting.account_grading import _age_days

# 只看最近 N 天有 engagement 的账户当"可能有新回信"的候选(夜跑每天一次,窗口略大于一天兜底)
CHANGE_WINDOW_DAYS = 3
# 回信新鲜度闸:engagement(开信/点击)近 ≠ 回复近。只对回信日期在此天数内的出提议,
# 否则是"陈年回信被近期开信带出来"(如 DTDS 去年的回信),不是新回复,跳过。
REPLY_RECENCY_DAYS = 30


def find_reply_candidates(records: List[dict], today=None,
                          window_days: int = CHANGE_WINDOW_DAYS) -> List[str]:
    """从全书读取结果里,挑出"最近有 engagement"的账户名(回复候选)。

    records: account_reader.grade_all 读到的行(含 account_name / last_engagement_date)。
    engagement 含开信/点击/回信,over-select 没关系——下一步 Breeze 会把非回信判 none 滤掉。
    """
    out = []
    for r in records:
        age = _age_days(r.get("last_engagement_date"), today)
        if age is not None and age <= window_days and r.get("account_name"):
            out.append(r["account_name"])
    return out


def build_proposals(candidates: List[str], classify_fn: Callable[[str], Optional[dict]],
                    is_processed: Optional[Callable[[str, object], bool]] = None,
                    today=None) -> List[dict]:
    """对候选账户逐个分类 → 出提议(跳过 none、陈年回信、已处理过的)。

    classify_fn(account) -> 分类 dict 或 None(无回信)。
    is_processed(account, reply_date) -> bool;None 则默认都没处理过(测试用)。
    返回 [{account, classification, proposal}]。不写任何东西。
    """
    from datetime import datetime, timezone
    today = today or datetime.now(timezone.utc).date()
    proposals = []
    for acct in candidates:
        cls = classify_fn(acct)
        if not cls:                      # none / 无回信 → 无动作
            continue
        reply_date = cls.get("reply_date")
        age = _age_days(reply_date, today)
        if age is not None and age > REPLY_RECENCY_DAYS:
            continue                     # 陈年回信被近期开信带出来 → 不是新回复,跳过
        if is_processed and is_processed(acct, reply_date):
            continue                     # 这条回信已提过,别重复
        prop = reply_router.route(cls["category"],
                                  stock_wake_days=cls.get("stock_wake_days"),
                                  account_name=acct)
        if not reply_router.is_actionable(prop):
            continue                     # 纯客套等无可执行内容 → 不进清单
        proposals.append({"account": acct, "classification": cls, "proposal": prop})
    return proposals


def apply_proposals(browser, view_url: str, apply: bool = False, logger=None) -> dict:
    """批准→落地待确认提议(在 Ned 批准的干净回合调用)。

    只写【可删/可逆或可下沉】的:建 Task(动作+到期,摘要主存贾维斯库不进 CRM)+ 设 priority。
    Sequence Opt-Out(explicit_no 才有,需单独验 bulk-edit)和 your_action → 归"你手动"清单。
    apply=False 只走流程不真写;apply=True 写完清空待确认库。effect=write_external。
    """
    from prospecting import customer_loop_store as store
    from prospecting import account_writer as writer

    pend = store.get_pending_proposals()
    tasks_done, prio_done, manual, failed = [], [], [], []
    for item in pend:
        acct = item.get("account", "")
        prop = item.get("proposal", {})
        if prop.get("task"):
            r = writer.create_task(browser, acct, prop["task"]["title"],
                                   prop["task"]["wake_days"], view_url, apply=apply, logger=logger)
            (tasks_done if r.get("ok") else failed).append(
                acct if r.get("ok") else {"account": acct, "what": "task", "reason": r.get("reason")})
        if prop.get("priority"):
            r = writer.set_property(browser, acct, "priority", prop["priority"],
                                    view_url=view_url, apply=apply, logger=logger)
            (prio_done if r.get("ok") else failed).append(
                acct if r.get("ok") else {"account": acct, "what": "priority", "reason": r.get("reason")})
        if prop.get("sequence_opt_out"):
            manual.append({"account": acct, "do": "set Sequence Opt-Out（暂手动,待验 bulk-edit）"})
        if prop.get("your_action"):
            manual.append({"account": acct, "do": prop["your_action"]})

    if apply:
        store.archive_proposals(pend)   # 先归档(留给回测),再清空待确认库
        store.clear_proposals()
    return {"applied": apply, "proposals": len(pend),
            "tasks": len(tasks_done), "priorities": len(prio_done),
            "manual": manual, "failed": failed}


def run(records: List[dict], classify_fn: Callable[[str], Optional[dict]],
        today=None) -> dict:
    """整条回复路(用真 store 跟踪已处理 + 存提议)。返回 {candidates, proposals}。"""
    from prospecting import customer_loop_store as store

    candidates = find_reply_candidates(records, today=today)
    proposals = build_proposals(candidates, classify_fn,
                                is_processed=store.is_reply_processed, today=today)
    for p in proposals:
        store.mark_reply_processed(p["account"], p["classification"].get("reply_date"))
    if proposals:
        store.save_proposals(proposals)
    return {"candidates": len(candidates), "proposals": proposals}
