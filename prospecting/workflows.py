"""
潜客工作流声明 —— 把选节点/生成/富化/投递拼成声明式 prospect 轨。

  prospect 轨：select(选类目×区域，或接续存盘批) → generate(LLM 联网找公司)
               → checkpoint(候选落盘，防止下游任何失败浪费这次生成)
               → preflight(HubSpot 会话检查；未登录 → 通知你去登录，干净收尾)
               → enrich(逐条 HubSpot 匹配) → output(写表+投递)
               → mark_done(推进树) → clear_pending(清掉存盘批)

**v0.4：本轨与信号库无关。** prospecting 包不再 import signal_library——
潜客名单服务 0→1 大范围开发（周期以月计），信号服务存量决策（时效几周、赛道级粒度），
两者节奏与粒度都不匹配。信号相关产出全部在情报侧（市场情报日报）。

**v0.5 瘦身与降级路径重做：**
  - assemble 步删除——它只剩打一个没人读的 track 标记。
  - history_mark / history_record 步删除——历史库整条移除（重复率低，不值一个存储层）。
  - 降级路径从「全批标 pending 照样出表」改为「存盘-通知-续跑」：
    旧路径有个实质缺陷——降级批照样 mark_done，这个 (类目,区域) 被标成已完成，
    重跑会选到下一个节点，降级批**永远不会被补上匹配**；表头那句"可一键补匹配"
    也从来没有对应的功能。新路径：HubSpot 未登录 → 候选存盘、通知你去登录、
    不出表不推进树；登录后重跑会直接续跑这批（不重花 LLM 生成的钱）。
  - 树全部跑完从 RuntimeError 改为 StopWorkflow 干净收尾——那是正常结束，不是故障。

确定性步骤已用 mock 测通；LLM 步(generate)与浏览器步(preflight/match)
由生产侧注入（运行时才有已登录会话），见下方 make_* 工厂。

market_intel 日报【不在这里】——走报告框架（core/reports + report_tools）。
这些步骤都是内部函数，模型侧只通过 run_workflow(prospect_daily) 触发，不单独暴露成工具。

契约见 intel/prospect_pipeline_contract.md、intel/delivery_and_resilience_spec.md。
"""
from __future__ import annotations

import inspect
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

from core import workflow as wf
from core import delivery
from prospecting import generation as gen
from prospecting import pipeline as pl


# ────────────────────────── 存盘批（降级路径的接续点） ──────────────────────────

def load_pending(pending_path: str | Path) -> Optional[dict]:
    """读存盘批。没有 / 读不出来返回 None（坏文件不该拦住今天的名单）。"""
    p = Path(pending_path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("node") and data.get("records"):
            return data
    except Exception:
        pass
    return None


def save_pending(pending_path: str | Path, node: dict, records: list[dict]) -> str:
    p = Path(pending_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "node": node,
        "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def clear_pending(pending_path: str | Path) -> bool:
    p = Path(pending_path)
    if p.exists():
        p.unlink()
        return True
    return False


# ────────────────────────── 工作流：潜客轨 ──────────────────────────

def build_prospect_workflow(*, tree_path: str | Path,
                            generate_fn: Callable[[dict], list[dict]],
                            preflight_fn: Callable[[], object],
                            output_fn: Callable[[dict], object],
                            pending_path: str | Path,
                            match_fn: Optional[pl.MatchFn] = None,
                            enrich_fn: Optional[Callable[[list[dict]], object]] = None,
                            notify_fn: Optional[Callable[[str], None]] = None) -> list[wf.Step]:
    """注入式构建潜客轨步骤列表。

    generate_fn(node)   -> 富候选列表（LLM 联网产出，见 make_llm_generate_fn）
    preflight_fn()      -> HubSpot 会话是否有效；可返回 bool 或 awaitable
                           （生产侧是 async——浏览器活在专属线程，见 make_hubspot_runtime）
    match_fn(name,dom)  -> {"status","owner",...}；测试用（同步 mock 逐条匹配）
    enrich_fn(records)  -> 整批富化，可返回 awaitable；生产侧用它把整批匹配
                           扔进浏览器线程，不冻住事件循环。与 match_fn 二选一，
                           enrich_fn 优先。
    output_fn(ctx)      -> 写富 xlsx + 投递（见 make_output_fn）
    pending_path        -> 存盘批文件路径（降级路径的接续点）
    notify_fn(text)     -> 干净收尾时怎么通知人；None 走投递闸门（测试可注入收集器）
    """
    if match_fn is None and enrich_fn is None:
        raise ValueError("match_fn 与 enrich_fn 至少要给一个")

    def _notify(text: str) -> None:
        if notify_fn is not None:
            notify_fn(text)
            return
        try:
            delivery.deliver("prospect", "今日潜客名单", text, severity="normal")
        except Exception:
            pass  # 通知失败不该把干净收尾变成故障

    def s_select(ctx):
        # 先看有没有上次存盘的批——有就接着做它（登录后续跑），不重新生成。
        pending = load_pending(pending_path)
        if pending:
            ctx["node"] = pending["node"]
            ctx["resume_records"] = pending["records"]
            ctx["resumed"] = True
            return pending["node"]
        node = gen.select_node(tree_path)
        if node is None:
            # 正常结束，不是故障：通知一声，干净收尾。
            _notify("潜客树已全部扫完。要重新开一轮，"
                    "在仓库根目录跑 `python -m prospecting.reset --yes` 复位后再跑。")
            raise wf.StopWorkflow("潜客树已全部扫完")
        ctx["node"] = node
        return node

    def s_generate(ctx):
        if ctx.get("resumed"):
            return ctx["resume_records"]
        return generate_fn(ctx["node"])

    def s_checkpoint(ctx):
        """候选落盘。此后下游任何失败（未登录/匹配崩/断电）都不浪费这次生成——
        重跑从这批直接续。on_error=skip：落盘失败只是丢了保险，不拦今天的名单。"""
        return save_pending(pending_path, ctx["node"], ctx["generate"])

    async def s_preflight(ctx):
        # preflight_fn 可能返回 bool（测试 mock）或 awaitable（生产：浏览器在
        # 专属线程里启动——sync Playwright 在事件循环线程里直接拒绝运行）。
        try:
            ok = preflight_fn()
            if inspect.isawaitable(ok):
                ok = await ok
            ok = bool(ok)
        except Exception:
            ok = False
        if not ok:
            node = ctx.get("node") or {}
            label = node.get("label") or ""
            n = len(ctx.get("generate") or [])
            _notify(f"HubSpot 会话检查未通过（未登录、页面未就绪或浏览器被其他工具占用），"
                    f"今天这批（{label}，{n} 家）已存盘。"
                    "请确认已在网页「本地调试」登录 HubSpot、没有别的工具正开着它，"
                    "然后重跑 prospect_daily——会直接续跑匹配，不会重新生成。")
            raise wf.StopWorkflow("HubSpot 会话检查未通过：本批已存盘待续跑")
        return True

    async def s_enrich(ctx):
        # 单条匹配失败 enrich_records 内部已兜（那条标 unknown）；
        # 整批级的崩溃走 abort——存盘批还在，重跑即续，无需降级出半成品。
        # 生产走 enrich_fn（整批扔进浏览器线程，~100 次匹配要几分钟，
        # 直接在事件循环里跑会把整个 app 冻住）；测试走 match_fn 逐条。
        if enrich_fn is not None:
            out = enrich_fn(ctx["generate"])
            if inspect.isawaitable(out):
                out = await out
            return out
        return pl.enrich_records(ctx["generate"], match_fn)

    def s_output(ctx):
        return output_fn(ctx)

    def s_mark_done(ctx):
        # 跑完把本 (类目, 区域) 记进 done_regions 写回树，下次 select 推进到下一个组合。
        # 必须带 region——否则会把整个类目一次标完，另外两个区域直接被跳过。
        # 只有 enrich 成功才走到这里：未匹配的批不再推进树（旧降级路径的实质 bug）。
        node = ctx.get("node") or {}
        leaf_id = node.get("id")
        if leaf_id:
            return gen.mark_node_done(tree_path, leaf_id, node.get("region"))
        return False

    def s_clear_pending(ctx):
        # 批已匹配、已交付、树已推进——保险不再需要。
        return clear_pending(pending_path)

    return [
        wf.Step("select", s_select),
        wf.Step("generate", s_generate, retries=1),
        wf.Step("checkpoint", s_checkpoint, on_error="skip"),  # 落盘失败只丢保险，不拦名单
        wf.Step("preflight", s_preflight),                     # 未登录 → 通知+干净收尾
        wf.Step("enrich", s_enrich),
        wf.Step("output", s_output, on_error="skip"),
        wf.Step("mark_done", s_mark_done, on_error="skip"),    # 推进树；失败不影响已出的名单
        wf.Step("clear_pending", s_clear_pending, on_error="skip"),
    ]


# ────────────────────────── 生产侧注入工厂（运行时） ──────────────────────────

def make_output_fn(out_dir: str | Path, track: str, title: str):
    """返回 output_fn：写富 xlsx（潜客轨）或直接取文本（日报轨）+ 过投递闸门推送。"""
    def output_fn(ctx) -> dict:
        records = ctx.get("enrich")
        path = None
        if records is not None:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            node = ctx.get("node") or {}
            # 文件名带区域：同一类目会跑三次（EU/NA/SEA），只用类目 id 会让三份
            # 名单难以分辨，也不利于事后做区域对照。
            node_id = (node.get("node_key") or node.get("id") or "list").replace(":", "_")
            path = str(Path(out_dir) / f"prospects_{node_id}_{date.today().isoformat()}.xlsx")
            pl.write_xlsx(records, path)
            content = f"潜客清单已生成：{len(records)} 家\n文件：{path}"
        else:
            content = ctx.get("render") or ""
        # attachments：新式渠道（飞书）会把 xlsx 作为文件消息真实发出——
        # 定时触发没有对话上下文、file_download 动作无处落地，靠的就是这条路。
        res = delivery.deliver(track, title, content, severity="normal",
                               attachments=[path] if path else None)
        return {"path": path,
                "delivered": res.get("delivered"), "reason": res.get("reason")}
    return output_fn


def make_hubspot_runtime():
    """惰性创建已登录的浏览器，返回 (preflight_fn, enrich_fn, close_fn)。

    v0.5 起走统一的 hubspot_session（screener 验证过的那套拉起/探测逻辑），
    不再用 HubSpotBrowser.start()——它绑死默认视图且异常类型有三种，难兜
    （screener 实测踩过）。无人值守场景不弹登录窗口（headed_login=False）：
    未登录就让 preflight 返回 False，由工作流走「存盘-通知-续跑」。

    【线程模型——这里踩过一个静默大坑】工作流跑在 asyncio 事件循环里（web 按钮、
    聊天工具 run_workflow 都是 `await wr.run(...)`），而 Playwright 的 sync API
    在「有运行中事件循环的线程」里会直接抛错拒绝启动。老实现的 preflight 在循环
    线程里调 browser.start() → **永远抛错 → 永远走降级**，登录状态根本无关——
    这就是"明明登录了名单还是全 pending"的真实原因。
    另外 sync Playwright 对象【绑定创建线程】，且 ~100 家逐条匹配要跑几分钟，
    放在循环线程里会把整个 app 冻住。所以：单线程 executor 独占浏览器全生命周期
    （启动/匹配/关闭全在同一条线程），preflight/enrich 是 async 包装，事件循环只
    await、不碰浏览器。

    依赖 playwright + 已登录 profile；仅在你机器上运行时调用。
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from prospecting import hubspot_worker as w
    from prospecting import hubspot_session as hs

    # profile 目录直接读 config（与 login_manager / screener 同一目录，登录态共用）
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

    # max_workers=1 是正确性要求，不是省资源：保证启动/匹配/关闭都发生在同一条线程
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hubspot-browser")

    def _preflight_sync() -> bool:
        try:
            session = hs.acquire(paths, logger, w.HUBSPOT_SEARCH_URL,
                                 headed_login=False)
        except Exception as e:
            logger.warning("preflight: 拿不到已登录会话（%s）", e)
            return False
        session_holder["session"] = session
        session.attach(browser)
        browser.run_mode = "background"
        try:
            # start() 里除登录外真正必要的两步：等搜索面就绪 + 建列映射
            browser._wait_startup_surface_ready(
                timeout_seconds=w.STARTUP_SURFACE_READY_TIMEOUT_SECONDS)
            browser.refresh_column_index_map(with_retry=True, context="startup")
        except Exception as e:
            logger.warning("preflight: 会话有效但搜索面未就绪（%s）", e)
            session.close()
            session_holder["session"] = None
            return False
        return True

    match_fn = pl.make_hubspot_match_fn(browser, logger)

    async def preflight():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, _preflight_sync)

    async def enrich(records: list[dict]) -> list[dict]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            executor, pl.enrich_records, records, match_fn)

    def _close_sync():
        s = session_holder.get("session")
        if s is not None:
            s.close()
            session_holder["session"] = None

    def close():
        # 关闭也必须回浏览器线程做（同一线程约束）；registry 在 finally 里同步调它
        try:
            executor.submit(_close_sync).result(timeout=30)
        except Exception as e:
            logger.warning("close: 关闭浏览器会话失败（可能泄漏进程）：%s", e)
        executor.shutdown(wait=False)

    return preflight, enrich, close


# 潜客生成专用的输出预算。别用 config.MAX_TOKENS_RESPONSE（4096）——那是给聊天
# 回复定的护栏。一个节点最多 100 家候选、每家还要写一段具体的 contact_rationale，
# 大约需要 8k–15k tokens；用 4096 必然截断。
#
# 【这是采集轨踩过并已修好的同一个坑】：signal_collection 早就有自己的
# _COLLECT_MAX_TOKENS(16000) + 抢救式解析，潜客生成这条路却一直是 4096 + 朴素解析，
# 于是截断后 json.loads 失败 → return [] → 前面已生成的几十家【全部丢弃】，
# 表现为"工作流跑成功了但一家都没有"。两边现在对齐。
_GEN_MAX_TOKENS = int(os.environ.get("JARVIS_PROSPECT_GEN_MAX_TOKENS", "16000"))


def split_meta(parsed: list) -> tuple[list[dict], dict]:
    """把模型输出拆成 (候选公司, 汇总条目)。

    提示词要求「数组最后放一条 {"_meta": true, "coverage_note": ...}」——那是
    「这个类目还能不能再挖」的合法落点。老版本让模型把这句话写在 JSON **外面**，
    与「只输出合法 JSON 数组」直接矛盾，模型只能二选一，输出因此不稳。

    这里顺手兜住两种脏数据：
      - 显式 _meta 条目
      - 任何没有 company_name 的字典（模型偶尔会塞说明性对象）
    两者都不该进名单——否则会变成一家没有名字的"公司"混进 xlsx。
    """
    records: list[dict] = []
    meta: dict = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if item.get("_meta") or not (item.get("company_name") or "").strip():
            meta.update({k: v for k, v in item.items() if k != "_meta"})
            continue
        records.append(item)
    return records, meta


def render_generation_prompt(template: str, node: dict) -> str:
    """把节点填进生成提示词模板（纯函数，可单测）。

    v2.0 起 {{REGION}} 是**单个**区域——一次只搜一个，模型不必在一次输出里
    分心兼顾三个（那必然厚此薄彼）。区域填人话名而不是 EU/NA/SEA 代号，
    否则模型得自己猜 "SEA" 指哪些国家；HS 码给它框定"这个类目到底指哪些成品"。
    """
    hs = node.get("hs_codes") or []
    region = (node.get("region_label")
              or node.get("region")
              # 兜底：老结构（区域列表）时取第一个，不静默串成多区域
              or (node.get("regions") or [""])[0] or "（未指定）")
    return (template
            .replace("{{NODE_LABEL}}", node.get("label", ""))
            .replace("{{SECTOR_LABEL}}", node.get("sector_label") or "")
            .replace("{{HS_CODES}}", "、".join(hs) if hs else "（未指定）")
            .replace("{{REGION}}", region))


def make_llm_generate_fn(prompt_path: str | Path):
    """返回 generate_fn(node)：跑一次生成提示词，解析出富候选 JSON。

    走【直连模型调用】而不是 JarvisController——与 signal_collection 同构：
    这一步不需要工具循环，只要一次带 :online 的文本补全（联网检索由模型内建）。
    直连的好处是能给自己的 max_tokens 预算，不受通用对话护栏限制，也不必
    为了拿一段文本去启动一个完整 agent。
    """
    template = Path(prompt_path).read_text(encoding="utf-8")

    async def _run(node: dict) -> list[dict]:
        import config
        from openai import AsyncOpenAI
        from core.json_salvage import salvage_json_array, looks_truncated

        prompt = render_generation_prompt(template, node)
        from core.llm import get_client
        client = get_client(timeout=540)   # 大批量生成给长超时（core/llm 单一构建点）
        resp = await client.chat.completions.create(
            model=config.CLAUDE_MODEL,        # 含 :online，可联网检索
            max_tokens=_GEN_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (resp.choices[0].message.content or "") if resp.choices else ""

        parsed = salvage_json_array(text)       # 截断也能保住已生成的那几十家
        records, meta = split_meta(parsed)      # 剥掉汇总条目，别让它冒充一家公司
        if meta.get("coverage_note"):
            # 「这个类目还能不能再挖」——过去提示词让模型写在 JSON 外面，
            # 与"只输出合法 JSON"直接冲突。现在给它数组内的合法位置。
            tail = "（自称已挖尽）" if meta.get("exhausted") else ""
            print(f"[prospect.generate] 覆盖度自述：{meta['coverage_note']}{tail}")
        if not records:
            # 明确报错而不是静默返回空——否则工作流会"显示成功但名单是空的"。
            # （workflow 里 generate 步 retries=1，会再试一次。）
            raise RuntimeError(
                f"生成未解析出任何候选（模型输出 {len(text)} 字）。"
                "可能是模型没联网/没按 JSON 输出，或输出为空。"
            )
        if looks_truncated(text):
            # 抢救成功但确实被切了：留个痕迹，便于判断要不要再抬预算或改分批
            print(f"[prospect.generate] ⚠ 输出疑似被 max_tokens({_GEN_MAX_TOKENS}) 截断，"
                  f"已抢救 {len(records)} 条")
        return records

    def generate_fn(node: dict):
        return _run(node)   # 返回 coroutine；workflow runner 会 await

    return generate_fn
