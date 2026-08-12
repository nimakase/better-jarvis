"""connectors/customer_loop_tools.py — 客户循环夜间工作流注册(OPEN,自动加载)。

把 `prospecting/view_manager.run_view_cycle`(v2)注册成一个【detach 工作流】,由 core/registry
自动 import 而登记(无需改 main.py、不碰 PROTECTED)。

2026-08-09:从 v1(`prospecting.nightly.run`,直接写 HubSpot priority/type 属性)切到 v2
(`view_manager.run_view_cycle`,名单法写私有 view 成员 + Bitable,不再碰 HubSpot 属性)——
v1 那套自动写 Account 属性越界了(Ned 的分工是:贾维斯维护 view 名单 + Bitable,
Account Type 由 Ned 手动设),v2 拉回这条边界。v1 的 nightly.run/cold_start.run/
account_writer.set_property 从此不再被本工作流调用(= 断电退休,先不删,后话)。
调度里挂的名字仍是 customer_loop_nightly,不用改调度,改完自动生效。

线程模型照 prospecting/workflows.make_hubspot_runtime:单线程 executor 独占浏览器全生命周期,
async step 用 run_in_executor 派活——因为 Playwright 的 sync API 不能在事件循环线程里跑。

护栏:
  - **安全默认 dry-run**:实际写入(view 成员 + Bitable)受环境变量 JARVIS_CUSTOMER_LOOP_APPLY
    控制(未设/非 1 = 只出计划不写)。
  - 视图 URL 从 JARVIS_GRADE_VIEW_URL 取(全字段源视图,读全书用;各段私有 view 走
    prospecting.view_config.VIEW_URL_MAP)。
  - 冷启动(首次全量,465 账户 ×2.5 分钟 ≈ 十几小时)不走本 detach 夜间工作流——一晚跑不完;
    冷启动是一次性手动/分批 run_view_cycle(limit=..., apply=...)灌 store,灌满后本工作流
    才是"稳态增量"(resume=True,只处理 store 里没有的新账户)。
  - 定时未在此建;由 Ned 用 scheduler 明示挂 customer_loop_nightly。

本文件只做注册与线程编排(逻辑在外);状态判定/view 归段/写回逻辑都在 prospecting/*。
"""
from __future__ import annotations

import os
from pathlib import Path

from core import workflow as wf
from core import workflow_registry as wr


# Ned 的「全字段视图」(portal 9311334)。env JARVIS_GRADE_VIEW_URL 可覆盖。
# 2026-08-11:换成新建的 view 69611232(去掉了 Priority 列,加了 Account Owner
# 供潜客工作流未来合并停靠点用;旧 view 68742792 停用,不再维护)。
DEFAULT_VIEW_URL = "https://app.hubspot.com/contacts/9311334/objects/0-2/views/69611232/list"


def _apply_enabled() -> bool:
    # 优先读 prospecting.settings(pydantic 从 .env 读进 settings,不进 os.environ);
    # os.environ 兜底(shell export)。2026-08-12:从 config.CUSTOMER_LOOP_APPLY 迁到这里
    # (诊断见项目记忆 jarvis-architecture-migration-plan ②),.env 变量名不变。
    try:
        from prospecting import settings as cl_settings
        if cl_settings.CUSTOMER_LOOP_APPLY:
            return True
    except Exception:
        pass
    return os.environ.get("JARVIS_CUSTOMER_LOOP_APPLY", "").strip() in ("1", "true", "yes")


def _view_url() -> str:
    # 2026-08-12:从 config.GRADE_VIEW_URL 迁到 prospecting.settings(同上)。
    try:
        from prospecting import settings as cl_settings
        if cl_settings.GRADE_VIEW_URL:
            return cl_settings.GRADE_VIEW_URL
    except Exception:
        pass
    return os.environ.get("JARVIS_GRADE_VIEW_URL", "").strip() or DEFAULT_VIEW_URL


def make_customer_loop_runtime(view_url: str, apply: bool):
    """返回 (run_async, close):浏览器全生命周期锁在单线程 executor 里跑 run_view_cycle(v2)。"""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from prospecting import hubspot_worker as w
    from prospecting import hubspot_session as hs
    from prospecting import view_manager
    from prospecting.view_config import VIEW_URL_MAP

    try:
        import config
        _dd = config.DATA_DIR
    except Exception:
        _dd = Path.home() / ".jarvis"
    paths = w.resolve_paths(_dd / "hubspot")
    w.ensure_directories(paths)
    logger = w.setup_logger(paths.log_file)
    browser = w.HubSpotBrowser(paths, logger)
    session_holder: dict = {"session": None}

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="customer-loop-browser")

    def _run_sync() -> dict:
        # 无人值守:不弹登录窗口;未登录则明确报错(由 notify 如实交付)。
        try:
            session = hs.acquire(paths, logger, view_url, headed_login=False)
        except Exception as e:
            logger.warning("customer_loop: 拿不到已登录会话(%s)", e)
            return {"error": f"未登录或会话不可用:{e}"}
        session_holder["session"] = session
        session.attach(browser)
        browser.run_mode = "background"
        try:
            return view_manager.run_view_cycle(
                browser, VIEW_URL_MAP, grade_view_url=view_url, apply=apply, logger=logger)
        except Exception as e:
            logger.warning("customer_loop: run_view_cycle 失败(%s)", e)
            return {"error": f"run_view_cycle 失败:{e}"}

    async def run_step(ctx: dict) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, _run_sync)

    def _close_sync() -> None:
        s = session_holder.get("session")
        if s is not None:
            s.close()
            session_holder["session"] = None

    def close() -> None:
        try:
            executor.submit(_close_sync).result(timeout=30)
        except Exception as e:
            logger.warning("customer_loop close 失败(可能泄漏进程):%s", e)
        executor.shutdown(wait=False)

    return run_step, close


def _incremental_insights_note(res: dict) -> str:
    """任务 #21：对增量夜跑结果做一次通用再分析，补上容易被现有文案漏掉的数字
    （held_count/池成员变动此前压根没进通知文本）。不需要懂具体业务规则，纯粹是
    "这个数字非零就该被看见"这类通用判断——拆成独立函数是为了能脱离整套浏览器
    runtime 单测（notify() 本身嵌在 _build_customer_loop_nightly() 里，构造它会
    触发真实 Playwright/HubSpot 依赖）。返回空串表示没有可补充的观察。"""
    from core import insight as _insight

    insight_data = {
        **res,
        "池_离开_数量": len(res.get("池_离开") or []),
        "池_进入_数量": len(res.get("池_进入") or []),
    }
    ins = _insight.analyze(insight_data, thresholds=[
        {"field": "held_count", "gt": 0,
         "message": "{value} 个账户因算到 cold/dead 会触发不可逆的 HubSpot "
                    "reset 工作流，已被护栏挂起未自动写，等待人工复核",
         "severity": "warning"},
        {"field": "池_离开_数量", "gt": 0,
         "message": "{value} 个账户从池里消失了(可能被工作流 reassign/reset "
                    "挪走，也可能是手动放弃)，值得确认是否符合预期",
         "severity": "notice"},
        {"field": "池_进入_数量", "gt": 0,
         "message": "{value} 个新账户进了池", "severity": "info"},
    ])
    return _insight.render(ins)


def _render_cycle_notification(res: dict) -> tuple[str, str]:
    """把 view_manager.run_view_cycle(v2) 的结果 dict 渲成夜报文案 + 严重度。

    拆成独立函数(同 _incremental_insights_note 的理由):notify() 本身嵌在
    _build_customer_loop_nightly() 里，构造它会触发真实 Playwright/HubSpot 依赖，
    拆出来才能脱离浏览器 runtime 单测。返回 (content, severity)。
    """
    if res.get("error"):
        return f"客户循环夜间作业未完成:{res['error']}", "high"
    if res.get("skipped"):
        return (f"客户循环:本次跳过——{res['skipped']}"
                f"(读到 {res.get('read', '?')})。"), "high"

    mode = "已写" if res.get("applied_mode") == "APPLY" else "dry-run(未写)"
    segs = res.get("cumulative_segments") or {}
    seg_text = "、".join(f"{k} {v}" for k, v in segs.items()) or "（无）"
    parts = [
        f"客户循环·增量 {mode}。本轮处理 prospecting "
        f"{res.get('processed_prospecting', 0)}/{res.get('prospecting_total', 0)}、"
        f"core {res.get('processed_core', 0)}/{res.get('core_total', 0)}。",
        f"累计各段:{seg_text}。",
    ]
    if res.get("reply_proposals"):
        parts.append(f"{res['reply_proposals']} 条回复待你确认路由建议(customer_loop_store)。")
    if res.get("core_due"):
        parts.append(f"{res['core_due']} 个 Core 账户到维护提醒点。")
    errs = res.get("breeze_errors") or []
    if errs:
        parts.append(f"{len(errs)} 个账户 Breeze 查询失败,已跳过(下次重试)。")
    fyi = res.get("core_no_deal_fyi") or []
    if fyi:
        parts.append(f"{len(fyi)} 个 Core 账户没有 deal 且你还没批注,确认是否符合预期。")
    dispo = res.get("dispositions_understood") or []
    if dispo:
        parts.append(f"读懂了 {len(dispo)} 条你写的处置备注。")
    if res.get("nameless_count"):
        parts.append(f"{res['nameless_count']} 个账户导入缺名,已跳过、需你补名或重导。")
    bitable = res.get("bitable")
    if isinstance(bitable, dict) and bitable.get("error"):
        parts.append(f"⚠ Bitable 写入失败:{bitable['error']}。")
    return " ".join(parts), "normal"


def _build_customer_loop_nightly():
    view_url = _view_url()
    apply = _apply_enabled()

    if not view_url:
        # 明确失败步骤,而不是静默跑空——提示去设视图 URL。
        def _missing(ctx):
            raise RuntimeError("未设 JARVIS_GRADE_VIEW_URL(全字段视图);无法运行客户循环夜间作业。")
        return [wf.Step("check_config", _missing)]

    run_step, close = make_customer_loop_runtime(view_url, apply)

    def notify(ctx: dict) -> dict:
        from core import delivery as _delivery
        content, sev = _render_cycle_notification(ctx.get("cycle") or {})
        r = _delivery.deliver(track="customer_loop", title="🔁 客户循环夜间作业",
                              content=content, severity=sev)
        return {"pushed": r.get("delivered", False)}

    return {
        "steps": [wf.Step("cycle", run_step), wf.Step("notify", notify, on_error="skip")],
        "context": {},
        "cleanup": close,
    }


wr.register_workflow(
    "customer_loop_nightly", "客户循环·夜间",
    "v2 增量:读全书 → 按轮次/双时钟判定状态 → 名单法写各段私有 view + 驾驶舱 Bitable"
    "(受 JARVIS_CUSTOMER_LOOP_APPLY 控制,默认 dry-run)。resume=True,只处理 store 里没有的"
    "新账户;首次全量(冷启动)不走本工作流,由 Ned 手动分批跑。产出经飞书交付。",
    _build_customer_loop_nightly, confirm=False, dispatch="detach",
    needs="需已登录 HubSpot + 设 JARVIS_GRADE_VIEW_URL;真写需另设 JARVIS_CUSTOMER_LOOP_APPLY=1",
)
# confirm=False:定时无人值守跑,不能卡在交互确认;安全靠 apply 开关(默认 dry-run)+ 漏读拒写。
