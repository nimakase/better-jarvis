"""
潜客工作流声明 —— 把信号库/生成/富化/投递拼成声明式 prospect 轨。

  prospect 轨：signal_check(信号新鲜度自检) → select → generate(LLM) → assemble(接意向)
               → preflight → enrich(matcher，登录挂则降级出 pending) → output(写表+投递)

确定性步骤 + 降级逻辑已用 mock 测通；LLM 步(generate)与浏览器步(preflight/match)
由生产侧注入（运行时才有 agent / 已登录会话），见下方 make_* 工厂。

market_intel 日报【不在这里】——走报告框架（core/reports + report_tools），见模块中部说明。
这些步骤都是内部函数，模型侧只通过 run_workflow(prospect_daily) 触发，不单独暴露成工具。

契约见 intel/prospect_pipeline_contract.md、intel/delivery_and_resilience_spec.md。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from core import workflow as wf
from core import delivery
from prospecting import generation as gen
from prospecting import pipeline as pl
from intel import signal_library as sl


# ────────────────────────── 降级（登录挂掉仍出清单） ──────────────────────────

def _degrade(records: list[dict]) -> list[dict]:
    """不做 HubSpot 匹配，纯按意向分排序，标 crm_state=pending。"""
    for r in records:
        intent = float(r.get("intent_score", pl.INTENT_BASELINE))
        r.update(hubspot_status="pending", hubspot_owner="", crm_state="pending",
                 intent_tier=pl.intent_tier(intent), weight=round(intent, 3))
    records.sort(key=lambda x: -x["weight"])
    for i, r in enumerate(records, 1):
        r["rank"] = i
    return records


# ────────────────────────── 工作流：潜客轨 ──────────────────────────

def build_prospect_workflow(*, tree_path: str | Path, db_path: str | Path,
                            generate_fn: Callable[[dict], list[dict]],
                            preflight_fn: Callable[[], bool],
                            match_fn: pl.MatchFn,
                            output_fn: Callable[[dict], object]) -> list[wf.Step]:
    """注入式构建潜客轨步骤列表。

    generate_fn(node)  -> 富候选列表（LLM 联网产出，见 make_llm_generate_fn）
    preflight_fn()     -> HubSpot 会话是否有效（见 make_hubspot_runtime）
    match_fn(name,dom) -> {"status","owner",...}（见 make_hubspot_runtime）
    output_fn(ctx)     -> 写富 xlsx + 投递（见 make_output_fn）
    """
    def s_signal_check(ctx):
        """信号新鲜度自检：算出离上次采集多少天、是否过期，存入 ctx 供 output 标注。
        永不抛错（on_error=skip 兜底）；过期不阻断，只标注。"""
        import config
        from datetime import date
        thresh = getattr(config, "SIGNAL_FRESHNESS_DAYS", 7)
        latest = sl.latest_collected_date(db_path=db_path)
        age = None
        if latest:
            try:
                age = (date.today() - date.fromisoformat(latest)).days
            except Exception:
                age = None
        ctx["signal_age_days"] = age
        ctx["signal_stale"] = (age is None) or (age > thresh)
        return {"latest": latest, "age_days": age, "stale": ctx["signal_stale"], "threshold": thresh}

    def s_select(ctx):
        node = gen.select_node(tree_path)
        if node is None:
            raise RuntimeError("所有潜客树节点已完成")
        ctx["node"] = node
        return node

    def s_generate(ctx):
        return generate_fn(ctx["node"])

    def s_assemble(ctx):
        return gen.assemble(ctx["node"], ctx["generate"], db_path=db_path)

    def s_preflight(ctx):
        ctx["hubspot_ok"] = bool(preflight_fn())
        return ctx["hubspot_ok"]

    def s_enrich(ctx):
        records = ctx["assemble"]
        if not ctx.get("hubspot_ok"):
            return _degrade(records)
        try:
            return pl.enrich_records(records, match_fn)
        except Exception:
            return _degrade(records)

    def s_output(ctx):
        return output_fn(ctx)

    def s_mark_done(ctx):
        # 跑完把本节点标 done 写回树，下次 select 推进到下一个 pending（写 jarvis 自己的树副本）
        leaf_id = (ctx.get("node") or {}).get("id")
        if leaf_id:
            return gen.mark_node_done(tree_path, leaf_id)
        return False

    return [
        wf.Step("signal_check", s_signal_check, on_error="skip"),  # 新鲜度自检：过期只标注不阻断
        wf.Step("select", s_select),
        wf.Step("generate", s_generate, retries=1),
        wf.Step("assemble", s_assemble),
        wf.Step("preflight", s_preflight, on_error="skip"),   # 预检失败→视为 down，enrich 降级
        wf.Step("enrich", s_enrich),                          # 永不抛：富化或降级
        wf.Step("output", s_output, on_error="skip"),
        wf.Step("mark_done", s_mark_done, on_error="skip"),   # 推进树；失败不影响已出的名单
    ]


# 注：市场情报日报【不走工作流】——它由报告框架（core/reports + connectors/report_tools
# 的 market_intel 报告类型，渲染见 intel/report.py）承担。此前这里有个 build_report_workflow
# 是历史遗留的死代码（从未注册），已删除，以免与报告框架重复、造成"日报到底走哪条"的混淆。


# ────────────────────────── 生产侧注入工厂（运行时） ──────────────────────────

def make_output_fn(out_dir: str | Path, track: str, title: str):
    """返回 output_fn：写富 xlsx（潜客轨）或直接取文本（日报轨）+ 过投递闸门推送。"""
    def output_fn(ctx) -> dict:
        degraded = bool(ctx.get("hubspot_degraded")) or (ctx.get("hubspot_ok") is False)
        stale = bool(ctx.get("signal_stale"))
        age = ctx.get("signal_age_days")
        # 收集标注语（HubSpot 降级 / 信号过期），同时用于推送文本与 xlsx 顶部 banner
        notes: list[str] = []
        if degraded:
            notes.append("本批未做 HubSpot 匹配，恢复后可一键补匹配")
        if stale:
            notes.append("信号库为空，意向排序仅按基线，仅供参考" if age is None
                         else f"信号已 {age} 天未更新，意向排序仅供参考")
        records = ctx.get("enrich")
        path = None
        if records is not None:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            node_id = (ctx.get("node") or {}).get("id", "list")
            from datetime import date
            path = str(Path(out_dir) / f"prospects_{node_id}_{date.today().isoformat()}.xlsx")
            pl.write_xlsx(records, path, banner=("⚠ " + "；".join(notes)) if notes else None)
            banner = f"（{'；'.join(notes)}）" if notes else ""
            content = f"潜客清单已生成：{len(records)} 家{banner}\n文件：{path}"
        else:
            content = ctx.get("render") or ""
        res = delivery.deliver(track, title, content, severity="normal")
        return {"path": path, "degraded": degraded,
                "signal_stale": stale, "signal_age_days": age,
                "delivered": res.get("delivered"), "reason": res.get("reason")}
    return output_fn


def make_hubspot_runtime():
    """惰性创建已登录的浏览器，返回 (preflight_fn, match_fn, close_fn)。

    依赖 playwright + 已登录 profile；仅在你机器上运行时调用。
    """
    from prospecting import hubspot_worker as w

    paths = w.resolve_paths(sl._DATA_DIR / "hubspot")
    w.ensure_directories(paths)
    logger = w.setup_logger(paths.log_file)
    browser = w.HubSpotBrowser(paths, logger)

    def preflight() -> bool:
        try:
            browser.start(run_mode="background")
            return True
        except Exception:
            return False

    match_fn = pl.make_hubspot_match_fn(browser, logger)

    def close():
        try:
            browser.stop()
        except Exception:
            pass

    return preflight, match_fn, close


def make_llm_generate_fn(prompt_path: str | Path):
    """返回 generate_fn(node)：用生成提示词跑一次 agent，解析出富候选 JSON。

    运行时依赖模型；这里给出最简实现，必要时按 controller 接口微调。
    """
    template = Path(prompt_path).read_text(encoding="utf-8")

    async def _run(node: dict) -> list[dict]:
        from core.controller import JarvisController
        prompt = (template
                  .replace("{{NODE_LABEL}}", node.get("label", ""))
                  .replace("{{REGIONS}}", "、".join(node.get("regions", []))))
        sc = JarvisController(interactive=False)  # 后台实例：不写用户档案/不自建工具
        text = ""
        async for ev in sc.chat(prompt):
            # chat() 现产出结构化事件；只取正文 text（工具进度走 type=="tool"）
            if isinstance(ev, dict) and ev.get("type") == "text":
                text += ev["text"]
        # 抽取 JSON 数组
        i, j = text.find("["), text.rfind("]")
        if i >= 0 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                return []
        return []

    def generate_fn(node: dict):
        return _run(node)   # 返回 coroutine；workflow runner 会 await

    return generate_fn
