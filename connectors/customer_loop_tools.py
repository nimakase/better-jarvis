"""connectors/customer_loop_tools.py — 客户循环夜间工作流注册(OPEN,自动加载)。

把 `prospecting/nightly.run` 注册成一个【detach 工作流】,由 core/registry 自动 import 而登记
(无需改 main.py、不碰 PROTECTED)。

线程模型照 prospecting/workflows.make_hubspot_runtime:单线程 executor 独占浏览器全生命周期,
async step 用 run_in_executor 派活——因为 Playwright 的 sync API 不能在事件循环线程里跑。

护栏:
  - 首跑=冷启动对账应用,会触发多次 write_external(经 account_writer 过 effects/trust 两闸)。
  - **安全默认 dry-run**:实际写入受环境变量 JARVIS_CUSTOMER_LOOP_APPLY 控制(未设/非 1 = 只出计划不写)。
    Ned 主干验收满意后再置 1 让首跑真写(= 我们说的"第一次正式夜间循环")。
  - 视图 URL 从 JARVIS_GRADE_VIEW_URL 取(全字段视图)。
  - 定时未在此建;由 Ned 用 scheduler 明示挂 customer_loop_nightly。

本文件只做注册与线程编排(逻辑在外);分级/对账/写回/冷启动逻辑都在 prospecting/*。
"""
from __future__ import annotations

import os
from pathlib import Path

from core import workflow as wf
from core import workflow_registry as wr


# Ned 的「全字段视图」(portal 9311334)。env JARVIS_GRADE_VIEW_URL 可覆盖。
DEFAULT_VIEW_URL = "https://app.hubspot.com/contacts/9311334/objects/0-2/views/68742792/list"


def _apply_enabled() -> bool:
    # 优先读 config(pydantic 从 .env 读进 settings,不进 os.environ);os.environ 兜底(shell export)。
    try:
        import config
        if config.CUSTOMER_LOOP_APPLY:
            return True
    except Exception:
        pass
    return os.environ.get("JARVIS_CUSTOMER_LOOP_APPLY", "").strip() in ("1", "true", "yes")


def _view_url() -> str:
    try:
        import config
        if config.GRADE_VIEW_URL:
            return config.GRADE_VIEW_URL
    except Exception:
        pass
    return os.environ.get("JARVIS_GRADE_VIEW_URL", "").strip() or DEFAULT_VIEW_URL


def make_customer_loop_runtime(view_url: str, apply: bool):
    """返回 (run_async, close):浏览器全生命周期锁在单线程 executor 里跑 nightly.run。"""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from prospecting import hubspot_worker as w
    from prospecting import hubspot_session as hs
    from prospecting import nightly

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
            return nightly.run(browser, view_url, apply=apply, logger=logger)
        except Exception as e:
            logger.warning("customer_loop: nightly.run 失败(%s)", e)
            return {"error": f"nightly.run 失败:{e}"}

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
        res = ctx.get("nightly") or {}
        if res.get("error"):
            content = f"客户循环夜间作业未完成:{res['error']}"
            sev = "high"
        else:
            phase = res.get("phase")
            if phase == "coldstart":
                s = res.get("summary", {})
                pc = s.get("plan_counts", {})
                md = s.get("manual_demote", []) or []
                mode = "已写自动集" if res.get("applied") else "dry-run(未写)"
                rc = s.get("review_count", 0)
                warn = "" if s.get("complete", True) else (
                    f"⚠️读取不完整(读到 {s.get('total')}/{s.get('expected_total')}),"
                    "本次未写、请重跑。")
                content = (warn + f"客户循环·冷启动 {mode}。计划:{pc};"
                           f"另有 {rc} 个 Core-无-deal 待你手动复核(带日期的中文清单在 plan 文件:"
                           f"{s.get('plan_file', '')})。")
            else:
                fc = res.get("flagged_count", 0)
                content = (f"客户循环·增量:更新 priority {res.get('changed', 0)} 个"
                           + (f";另有 {fc} 个你手动设的与规则不符(仅提示、未改,详见分歧提示)" if fc else "")
                           + "。")
            sev = "normal"
        r = _delivery.deliver(track="customer_loop", title="🔁 客户循环夜间作业",
                              content=content, severity=sev)
        return {"pushed": r.get("delivered", False)}

    return {
        "steps": [wf.Step("nightly", run_step), wf.Step("notify", notify, on_error="skip")],
        "context": {},
        "cleanup": close,
    }


wr.register_workflow(
    "customer_loop_nightly", "客户循环·夜间",
    "首跑应用冷启动对账计划(受 JARVIS_CUSTOMER_LOOP_APPLY 控制,默认 dry-run);"
    "之后增量(回复路+三层,待建)。产出经飞书交付。",
    _build_customer_loop_nightly, confirm=False, dispatch="detach",
    needs="需已登录 HubSpot + 设 JARVIS_GRADE_VIEW_URL;真写需另设 JARVIS_CUSTOMER_LOOP_APPLY=1",
)
# confirm=False:定时无人值守跑,不能卡在交互确认;安全靠 apply 开关(默认 dry-run)+ 漏读拒写。
