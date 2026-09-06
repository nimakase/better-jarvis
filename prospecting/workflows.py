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
import itertools
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


# ────────────────────────── 「现在跑的是哪个节点」实时标记 ──────────────────────────
#
# 2026-08-14：跟上面的 pending（存盘批，"生成完了、等着匹配"的接续点）是两回事。
# 这个文件回答的是"现在正在跑（可能还在联网生成中）的是哪个节点"——之前完全没有
# 任何地方持久化这个信息：s_select 选完节点只写进 workflow 的内存 ctx，s_checkpoint
# 落盘要等 generate 那一大圈（可能 20+ 分钟）跑完才发生。这中间贾维斯自己（在对话
# 里被问"现在跑到哪了"）没有任何readable的地方能查，只能翻 stdout/日志。
# 写失败/读失败都不该影响 workflow 本身——这纯粹是给外部查询用的旁路状态，跟
# select_node()/mark_node_done() 的决策逻辑完全无关，不参与任何判断。

def save_current_node(current_node_path: str | Path, node: dict, *, resumed: bool = False) -> None:
    p = Path(current_node_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "node": node,
            "resumed": resumed,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_current_node(current_node_path: str | Path) -> Optional[dict]:
    """读「现在跑的是哪个节点」标记。没有在跑/读不出来都返回 None（不是故障）。"""
    p = Path(current_node_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_current_node(current_node_path: str | Path) -> bool:
    p = Path(current_node_path)
    if p.exists():
        try:
            p.unlink()
            return True
        except Exception:
            return False
    return False


# ────────────────────────── 工作流：潜客轨 ──────────────────────────

def build_prospect_workflow(*, tree_path: str | Path,
                            generate_fn: Callable[[dict], list[dict]],
                            preflight_fn: Callable[[], object],
                            output_fn: Callable[[dict], object],
                            pending_path: str | Path,
                            current_node_path: Optional[str | Path] = None,
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
    current_node_path   -> 「现在跑的是哪个节点」实时标记文件路径；None（默认）=
                           不写，行为与改动前完全一致（测试/旧调用点不用管这个）。
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
            if current_node_path is not None:
                save_current_node(current_node_path, pending["node"], resumed=True)
            return pending["node"]
        node = gen.select_node(tree_path)
        if node is None:
            # 正常结束，不是故障：通知一声，干净收尾。
            if current_node_path is not None:
                clear_current_node(current_node_path)
            _notify("潜客树已全部扫完。要重新开一轮，"
                    "在仓库根目录跑 `python -m prospecting.reset --yes` 复位后再跑。")
            raise wf.StopWorkflow("潜客树已全部扫完")
        ctx["node"] = node
        if current_node_path is not None:
            save_current_node(current_node_path, node)
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
        # 批已匹配、已交付、树已推进——保险不再需要；同时清掉「现在跑的是哪个
        # 节点」标记，这一批已经跑完了，不再是"现在"。
        if current_node_path is not None:
            clear_current_node(current_node_path)
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


# ── 候选生成的查询扇出（2026-08-13 修复"全是大厂假容量"） ──────────────────
#
# 问题根因：make_llm_generate_fn 只发一条宽查询（如 "可编程控制器与工业变频驱动
# manufacturers 欧洲…"），augment_with_search 只取 8 条结果拼进 prompt。模型没有
# 内置联网，它的全部素材就是这 8 条——而这类宽查询的 top 结果几乎被行业头部品牌
# （SEW、Beckhoff、NORD…）霸榜，于是模型只能顺着材料写巨头，产出全是够不着的
# 大厂（giant 档应排除但模型手头没别的可选）。
#
# 修复：把单条宽查询按区域拆成【子区域/国家】的多条窄查询，每个子区域各取 top
# 结果，总材料面从 8 条扩到 40+ 条，中小厂才有机会被检索到、进入模型视野。
# 同时给查询拼上全球巨头的负向排除词（Exa 支持 -term 语法；AnySearch 兜底后端
# 不支持也只需吞掉，负向只是帮模型把注意力从巨头身上移开，真正的排除口径还在
# 提示词的 giant 档）。

# 2026-08-14：区域框架 v2——原来只有 EU/NA/SEA 三块，是当初树（v2.0.0）随手定的
# 范围，从没系统盘点过"哪些国家该在、哪些漏了"。这次借用户追问"美洲不该只有
# 三国""捷克等是什么意思"一起盘了一遍（见 [[prospecting-generation-v3-redesign]]）：
#   - EU 清单本身有漏：原来 8 国没有西班牙/比利时/奥地利/瑞士——西班牙汽车/家电
#     制造体量不小，瑞士精密仪器/医疗器械跟本树多个类目（检测计量、医疗设备、
#     工业自动化）高度对口，不该漏。
#   - NA（现改人话名"北美"，代号不变）原来把美国当一个整体搜，跟当初"整个欧洲
#     一条查询"是同一个问题换了尺度——美国工业体量大致相当于整个欧盟，笼统搜
#     一次材料照样会被头部品牌占满。拆成几个制造业集群分别搜。
#   - 新增 EA（东亚：日本/韩国/台湾）——电子/精密制造体量很大且明确不属于树
#     已排除的中国大陆/香港，是三区域框架里最大的空白，未必比新增南美价值低。
#   - 新增 SA（南美：巴西/阿根廷/哥伦比亚）——用户直接问起"巴西呢"，巴西家电/
#     汽车/部分工业制造有真实体量。
#   - 评估过但没加：中亚（几乎没有整机电子制造业，预期产出个位数）、非洲发达
#     地区（南非/埃及/摩洛哥有产线但多是欧亚企业当地代工厂，采购权多半在总部，
#     跟"giant 子公司"排除是同一逻辑，预期产出同样很低）——判断标准与当初清理
#     4 个低价值树叶子一致：宁可不开区域，也不要开一个几乎搜不出东西的空区域。
# 树侧改动见 data/prospect_tree.json：regions_meta 新增 EA/SA 两个键、每个叶子
# 的 regions 数组从 3 个区域扩到 5 个（EU/NA/SEA/EA/SA 全量追加，不分类目挑选——
# 12 个赛道目前看不出哪个该被排除在新区域外，先统一开，后续真机跑出"这个类目在
# 某新区域完全挖不到东西"的实证再考虑收窄，好过没数据先猜着收窄）。
_REGION_QUERY_TERMS: dict[str, list[str]] = {
    "EU": ["Germany", "Italy", "France", "Netherlands", "Nordics",
           "Poland", "Czech Republic", "United Kingdom",
           "Spain", "Belgium", "Austria", "Switzerland"],
    # 原来是笼统的一个 "USA"——跟当初"整个欧洲一条查询"是同一个问题换了尺度，
    # 美国工业体量大致相当于整个欧盟，不拆开材料照样会被头部品牌占满。
    # 拆成几个制造业集群（中西部/五大湖、东南部、德州湾岸、加州、东北部）。
    "NA": ["Midwest USA", "Southeast USA", "Texas", "California", "Northeast USA",
           "Canada", "Mexico"],
    "SEA": ["Singapore", "Malaysia", "Thailand", "Vietnam",
            "Indonesia", "Philippines"],
    "EA": ["Japan", "South Korea", "Taiwan"],
    "SA": ["Brazil", "Argentina", "Colombia"],
}

# 全球工业巨头：采购集中总部、走全球框架协议、决策链长，够不着。
# 跨类目适用（自动化/能源/医疗/工业设备里它们都在最前排），点名排除。
#
# 2026-08-14：随区域框架扩到 EA/SA，原列表清一色西方工业巨头，完全没覆盖日韩台/
# 南美的头部企业——新开区域如果不同步扩排除词表，会重演"搜出来全是大厂"的老
# 问题（这次是换成松下/三星/富士康那一批）。新增覆盖东亚（日/韩/台）与南美
# （巴西）在本树各赛道最常见的头部品牌；也顺手补了几个 EU 新增国家（西/比/奥/瑞）
# 对应品类里露头最多的巨头（Philips/Legrand/ASSA ABLOY）。
_GLOBAL_GIANT_EXCLUDES = [
    "Siemens", "ABB", "Schneider", "Rockwell", "Emerson", "Honeywell",
    "Beckhoff", "SEW-EURODRIVE", "NORD", "Danfoss", "Yaskawa", "Mitsubishi",
    "Omron", "Bosch", "Eaton", "GE",
    # 东亚（日本/韩国/台湾）
    "Panasonic", "Sony", "Hitachi", "Toshiba", "Fujitsu", "NEC", "Fanuc",
    "Keyence", "Murata", "Samsung", "LG", "Hyundai", "Foxconn", "Delta Electronics",
    # 南美
    "WEG", "Embraer",
    # 欧洲新增国家里最常露头的巨头
    "Philips", "Legrand", "ASSA ABLOY",
]


# ── 潜客生成 v3：子类目扇出 + 轮次化"搜到目标为止"（2026-08-13） ──────────────
#
# 背景：上面的国家级扇出（_REGION_QUERY_TERMS）解决了"材料面太窄→全是大厂"，
# 但节点本身（如"工业自动化"）仍然只是一个宽类目，一次搜索、一次生成，findable
# 的公司池子被这一个宽概念限死——实测一个节点跑出来只有 5-6 家，怎么跑都是这几家。
#
# 真正的问题：树的大节点（sector 下的 leaf）是给人看的分类粒度，不是给搜索引擎
# 用的查询粒度。"工业自动化"底下其实是机器视觉、PLC、机器人、注塑机……十几个
# 完全不同的采购语境，混在一条查询里搜，结果自然被最强势的那个子领域霸占。
#
# 方案（按用户要求：**大节点还是这个大节点，只细化搜索**——不改树的结构/粒度，
# 只改"怎么去搜"）：
#   1. _LEAF_SUBTERMS：给每个叶子类目一组更具体的英文产品词（子类目粒度），
#      用于生成"细分产品 × 区域"的窄查询——这是补齐"这个大节点到底能挖多深"
#      的主力手段。
#   2. 保留原有的"类目 × 国家"扇出（_REGION_QUERY_TERMS）作为另一条独立的
#      查询路径——两条路径各自扇出、交替编排进查询队列（build_query_queue），
#      不做子类目 × 国家的全交叉（72 叶子 × 8 国会爆炸成本），交替编排已经能
#      同时兼顾"产品细"和"地域细"两种视角。
#   3. 把"一次搜索 + 一次生成"改成【轮次化循环】：每轮从查询队列里取一小批
#      查询去搜索+生成一次，与前面几轮的结果去重合并，直到累计满足目标数量
#      （约 100 家，软目标）、或连续若干轮搜不出新公司（挖尽）、或查询队列
#      耗尽、或触达轮数上限（成本护栏）——四个条件任一满足就停。
#
# 【区域硬边界，非新增约束，本来就是结构性的】：用户提到贾维斯手动跑的时候
# 跳出了当次该跑的地区（该补 SEA 却先跳去跑 NA）。但那是"人在对话里手动多轮
# 搜索"时发生的，不是这条自动化流水线的行为——select_node() 在生成开始【之前】
# 就已经把 (类目, 区域) 锁定成一个不可变的 node 参数，本函数从头到尾都只知道
# 这一个区域，没有任何分支能让它在跑的过程中去搜别的区域。也就是说"约束"已经
# 由函数签名本身保证，不需要也无法在这里再加一层运行时检查。真正需要改的是
# 别再用那种手动多轮对话的方式补数据——用这条新的自动化循环代替它。


# 子类目产品词表（英文，供搜索用）——2026-08-21 随 NAICS 47+5 节点新树重写：
# v3.0 是给 72 个 HS-code 叶子各配 3-5 个窄产品词；v4.0 树本身已经是 NAICS 审查后
# 的模组/类目粒度（52 节点），词表改成给每个新节点配吸收进来的老叶子词表全集
# （横向模组节点如 mod_motors_generators/mod_relays_controllers 因为合并了十几个
# 老叶子，词表比其它节点长很多，是有意为之，不是失误）。
_LEAF_SUBTERMS: dict[str, list[str]] = {
    # ── horizontal_modules ──
    "mod_ems_pcba": ["contract electronics manufacturer", "EMS provider", "PCBA assembly manufacturer", "box build manufacturer", "electronic manufacturing services company", "SMT assembly contract manufacturer"],
    "mod_process_instruments": ["process control instrument manufacturer", "pressure transmitter manufacturer", "flow meter manufacturer", "level sensor manufacturer", "temperature transmitter manufacturer", "process controller manufacturer", "metering pump manufacturer", "dosing system manufacturer", "industrial process analyzer manufacturer"],
    "mod_sensors_ndt": ["pressure transmitter manufacturer", "non-destructive testing NDT equipment manufacturer", "ultrasonic flaw detector manufacturer", "X-ray inspection system manufacturer", "industrial proximity sensor manufacturer", "load cell manufacturer"],
    "mod_transformers": ["power transformer manufacturer", "distribution transformer manufacturer", "specialty transformer manufacturer", "current transformer manufacturer"],
    "mod_motors_generators": ["industrial electric motor manufacturer", "servo motor manufacturer", "AC induction motor manufacturer", "geared motor / gearmotor manufacturer", "brushless DC motor manufacturer", "linear motor manufacturer", "industrial generator manufacturer", "alternator manufacturer", "explosion-proof motor manufacturer", "traction motor manufacturer (forklift/AGV)", "injection molding machine manufacturer", "plastic extrusion machine manufacturer"],
    "mod_switchgear": ["switchgear manufacturer", "switchboard manufacturer", "low voltage panel manufacturer", "motor control center MCC builder", "busbar system manufacturer", "distribution board manufacturer"],
    "mod_relays_controllers": ["programmable logic controller PLC manufacturer", "variable frequency drive VFD manufacturer", "servo drive manufacturer", "motion controller manufacturer", "industrial relay manufacturer", "motor starter manufacturer", "motor control center MCC manufacturer", "contactor manufacturer", "safety relay manufacturer", "timer relay manufacturer", "elevator controller manufacturer", "burner control module manufacturer", "industrial automation controller manufacturer", "blow molding machine manufacturer", "plastic processing machine controller manufacturer"],
    "mod_led_driver": ["LED driver manufacturer", "LED power supply manufacturer", "lighting control module manufacturer", "dimmable LED driver manufacturer"],
    "mod_smart_actuators": ["smart valve actuator manufacturer", "electric valve actuator manufacturer", "valve positioner manufacturer", "pneumatic actuator manufacturer"],
    "mod_service_equipment_ctrl": ["vending machine manufacturer", "commercial laundry equipment manufacturer", "photocopier control module manufacturer", "self-service kiosk payment module manufacturer"],
    "mod_digital_signage": ["digital signage manufacturer", "LED display module manufacturer", "LED video wall manufacturer", "outdoor LED billboard manufacturer"],
    "mod_welding_power": ["welding power source manufacturer", "inverter welding machine manufacturer", "arc welding power supply manufacturer", "robotic welding system manufacturer", "laser cutting machine power supply manufacturer"],
    # ── industrial_automation ──
    "ia_robotic_arms": ["industrial robot arm manufacturer", "robotic arm OEM", "collaborative robot cobot manufacturer", "robotic gripper manufacturer"],
    "ia_machine_vision": ["machine vision system manufacturer", "industrial camera manufacturer", "vision inspection system manufacturer", "barcode scanner manufacturer"],
    # ── test_measurement ──
    "test_flow_counters": ["totalizing flow meter manufacturer", "fluid counting device manufacturer", "custody transfer meter manufacturer"],
    "test_electrical_instruments": ["oscilloscope manufacturer", "electronic test instrument manufacturer", "signal generator manufacturer", "spectrum analyzer manufacturer"],
    "test_lab_instruments": ["laboratory chromatography instrument manufacturer", "mass spectrometer manufacturer", "electron microscope manufacturer", "gas analyzer manufacturer", "environmental monitoring instrument manufacturer", "spectrometer manufacturer", "in vitro diagnostics manufacturer", "clinical lab analyzer manufacturer"],
    "test_weighing": ["industrial weighing system manufacturer", "checkweigher manufacturer", "load cell manufacturer"],
    # ── medical_devices ──
    "med_electrotherapy_imaging": ["medical imaging equipment manufacturer", "ultrasound machine manufacturer", "patient monitor manufacturer", "ventilator manufacturer", "hearing aid manufacturer", "implantable medical device manufacturer", "electrotherapeutic apparatus manufacturer"],
    "med_irradiation": ["medical irradiation equipment manufacturer", "industrial sterilization equipment manufacturer", "radiotherapy equipment manufacturer"],
    "med_surgical": ["surgical device manufacturer", "electrosurgical unit manufacturer", "medical instrument manufacturer"],
    "med_dental": ["dental equipment manufacturer", "dental imaging device manufacturer"],
    # ── telecom_network ──
    "telecom_equipment": ["network switch manufacturer", "industrial router manufacturer", "ethernet switch manufacturer", "base station equipment manufacturer", "RRU remote radio unit manufacturer", "telecom RF equipment manufacturer", "fiber optic transmission equipment manufacturer", "broadband access equipment manufacturer", "optical transceiver manufacturer", "professional two-way radio manufacturer", "land mobile radio manufacturer", "telephone apparatus manufacturer"],
    # ── energy_power ──
    "energy_battery_mfg": ["industrial battery manufacturer", "lithium battery pack manufacturer", "battery management system manufacturer", "energy storage battery manufacturer"],
    "energy_pv_inverter": ["solar inverter manufacturer", "PV inverter manufacturer", "energy storage converter manufacturer"],
    "energy_ups": ["UPS manufacturer", "uninterruptible power supply manufacturer", "data center power system manufacturer"],
    "energy_ev_charging": ["EV charging station manufacturer", "electric vehicle charger manufacturer", "DC fast charger manufacturer"],
    "energy_bms_storage": ["battery management system manufacturer", "energy storage system integrator", "BMS manufacturer"],
    "energy_smart_meter": ["smart meter manufacturer", "distribution automation equipment manufacturer", "electricity meter manufacturer"],
    "energy_generator_control": ["generator control system manufacturer", "genset manufacturer", "grid-tie control equipment manufacturer"],
    # ── automotive ──
    "auto_electronics": ["automotive infotainment system manufacturer", "in-vehicle cockpit electronics manufacturer", "ADAS module manufacturer", "automotive radar sensor manufacturer", "automotive camera module manufacturer", "commercial vehicle electronics manufacturer", "truck telematics manufacturer", "EV motor drive system manufacturer", "onboard charger manufacturer"],
    "auto_brake_tcu": ["brake control unit ECU supplier", "ABS ESC module supplier", "transmission control unit TCU supplier"],
    "auto_rail_control": ["rail signaling equipment manufacturer", "train onboard electronics manufacturer", "railway control system manufacturer"],
    "auto_lev_controller": ["electric bicycle controller manufacturer", "e-scooter controller manufacturer", "light electric vehicle motor controller manufacturer"],
    # ── aerospace_defense ──
    "aero_nav_avionics": ["avionics manufacturer", "flight control system manufacturer", "ground radar system manufacturer", "navigation equipment manufacturer", "marine electronics manufacturer", "ship navigation equipment manufacturer", "satellite communication terminal manufacturer", "VSAT manufacturer"],
    "aero_aircraft_parts": ["aircraft electronics manufacturer", "avionics component manufacturer", "aircraft auxiliary equipment manufacturer"],
    "aero_uav_payload": ["drone manufacturer", "UAV payload manufacturer", "unmanned aircraft systems manufacturer"],
    # ── security_building ──
    "sec_commercial_lighting": ["commercial LED lighting fixture manufacturer", "industrial lighting control manufacturer", "architectural lighting manufacturer"],
    "sec_cctv_surveillance": ["CCTV camera manufacturer", "security camera manufacturer", "video surveillance NVR manufacturer"],
    "sec_access_control": ["access control system manufacturer", "biometric reader manufacturer", "turnstile manufacturer"],
    "sec_fire_alarm": ["fire alarm system manufacturer", "emergency evacuation system manufacturer", "smoke detector manufacturer"],
    # ── computing_datacenter ──
    "comp_peripherals": ["industrial PC manufacturer", "embedded computer manufacturer", "rugged computer manufacturer", "computer storage device manufacturer", "computer terminal manufacturer"],
    "comp_display": ["industrial display manufacturer", "touch panel PC manufacturer"],
    "comp_pos_kiosk": ["POS terminal manufacturer", "self-service kiosk manufacturer", "payment terminal manufacturer"],
    "comp_commercial_printing": ["commercial printer manufacturer", "label printer manufacturer", "industrial printing equipment manufacturer"],
    # ── consumer_appliance ──
    "av_equipment": ["professional audio equipment manufacturer", "AV equipment manufacturer", "speaker manufacturer", "broadcast equipment manufacturer", "studio camera manufacturer", "professional video production equipment manufacturer"],
    "wearable_health": ["wearable device manufacturer", "fitness tracker manufacturer", "smartwatch manufacturer"],
    # ── semicon_equipment ──
    "semi_frontend_equipment": ["semiconductor front-end equipment manufacturer", "wafer fabrication equipment manufacturer", "etching equipment manufacturer", "SMT pick and place machine manufacturer", "reflow oven manufacturer", "automated test equipment manufacturer", "IC handler manufacturer", "cleanroom equipment manufacturer", "battery manufacturing equipment manufacturer"],
    # ── specialty_machinery ──
    "sm_heatpump": ["heat pump manufacturer", "commercial HVAC unit manufacturer", "air source heat pump manufacturer"],
    "sm_cold_chain": ["cold chain equipment manufacturer", "refrigeration system manufacturer", "temperature control unit manufacturer"],
    "sm_water_treatment": ["water treatment equipment manufacturer", "wastewater treatment control system manufacturer", "environmental control equipment manufacturer"],
    "sm_textile_print": ["textile machinery manufacturer", "printing machinery control system manufacturer", "textile equipment electronics manufacturer"],
}
# 轮次化循环的四个停止条件用的旋钮，均可用环境变量覆盖，方便调试/压测不改代码。
#
# 2026-08-14：_MAX_ROUNDS 从 8 调到 14——真机首跑（ia_robotics×EU）只挖出 22 家就
# 停了，查下来不是"挖尽"也没撞轮数上限，是当时查询队列本身只有 5 轮材料，5 轮
# 跑完循环就自然结束（见 _subcategory_queries 的同日修正：现在改成子类目词×国家
# 交叉，材料面本身已经不再是瓶颈）。_MAX_ROUNDS 才是真正该卡成本的地方，材料
# 够了之后就该让它由这个数、或 _DRY_ROUND_LIMIT、或 _TARGET_CANDIDATES 来收尾，
# 而不是被一个意外偏小的队列长度提前打断。14 轮 × 3 条/轮 ≈ 42 次搜索查询，
# 单节点耗时会明显变长（尤其材料丰富、迟迟不挖尽的类目）——如果实跑下来太慢，
# 优先调小这个数（或用 JARVIS_PROSPECT_MAX_ROUNDS 覆盖），而不是让队列变回太短。
#
# 2026-08-21：_TARGET_CANDIDATES 从 100 调到 500——真机实测有节点在 target=100
# 时跑到 140 家才停，证明 100 是在真实拖累产出，不是"够用的软目标"。回头看，
# target_candidates 从一开始就回答不了"这个节点该挖多少家"这个问题——真正能
# 回答"挖没挖尽"的是 _DRY_ROUND_LIMIT（连续几轮无新增），能回答"愿意为这个
# 节点花多少成本"的是每个节点自己的 max_rounds（树里 node.max_rounds，读不到
# 才落到 _MAX_ROUNDS 兜底）。所以不再猜每个节点该给多少 target，统一调到 500——
# 几乎不会被真正撞到，退出"决定停不停"的角色，把停止逻辑真正交给挖尽信号和
# 节点级成本护栏。详见项目备忘录 prospecting-generation-v3-redesign。
#
# 2026-08-21：_MAX_ROUNDS 从全局单一值改成"节点级 max_rounds 优先，读不到再落到
# 这个全局兜底"——新树（52节点）里不同节点吸收的老叶子数量差异很大（如
# mod_motors_generators/mod_relays_controllers 各吸收了十几个老叶子，
# max_rounds=26；大多数节点维持默认 14），继续用同一个全局值会让"体量大的
# 节点搜不透、体量小的节点白烧轮数"同时发生。这个全局值现在只是兜底（树外
# 节点/测试用例/忘了配 max_rounds 的新节点）。
_TARGET_CANDIDATES = int(os.environ.get("JARVIS_PROSPECT_TARGET", "500"))     # 软目标，不是硬配额
_MAX_ROUNDS = int(os.environ.get("JARVIS_PROSPECT_MAX_ROUNDS", "14"))         # 成本护栏兜底，节点级 max_rounds 优先
_DRY_ROUND_LIMIT = int(os.environ.get("JARVIS_PROSPECT_DRY_LIMIT", "3"))      # 连续几轮无新增就算挖尽
_ROUND_BATCH_SIZE = int(os.environ.get("JARVIS_PROSPECT_ROUND_BATCH", "3"))   # 每轮打包几条查询一起搜


def _company_key(rec: dict) -> str:
    """跨轮次去重键：公司名 + 官网域名各自归一化后拼接。

    两个都参与是因为单靠名字会漏掉"同名不同实体"的极少数误判风险很低，但单靠
    域名又会漏掉"模型这轮没填 website"的情况——两者取并集判重，宁可判重判严
    一点（漏合并了大不了多一行，人工核对时能看出来），也不要漏判重复混进名单。
    """
    name = (rec.get("company_name") or "").strip().lower()
    site = (rec.get("website") or "").strip().lower()
    for prefix in ("https://", "http://"):
        if site.startswith(prefix):
            site = site[len(prefix):]
    if site.startswith("www."):
        site = site[4:]
    site = site.rstrip("/")
    return f"{name}|{site}"


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
    否则模型得自己猜 "SEA" 指哪些国家；NAICS 码给它框定"这个类目到底指哪些成品"
    （v4.0 起从 HS 码换成 NAICS 码，模板占位符同步从 {{HS_CODES}} 改成
    {{NAICS_CODES}}，见 intel/prospect_generation_prompt.md 的同步改动）。
    """
    naics = node.get("naics_codes") or []
    region = (node.get("region_label")
              or node.get("region")
              # 兜底：老结构（区域列表）时取第一个，不静默串成多区域
              or (node.get("regions") or [""])[0] or "（未指定）")
    return (template
            .replace("{{NODE_LABEL}}", node.get("label", ""))
            .replace("{{SECTOR_LABEL}}", node.get("sector_label") or "")
            .replace("{{NAICS_CODES}}", "、".join(naics) if naics else "（未指定）")
            .replace("{{REGION}}", region))


def _country_queries(label_en: str, region_key: str, region_label: str) -> list[str]:
    """路径 B：类目名 × 国家/子区域的查询（原 build_generation_queries 的逻辑）。

    - 查询词特意用英文：目标是找真实存在的制造商官网/行业新闻，这类信息源
      英文覆盖通常比中文广，跟中文场景（如 HubSpot 报价术语）不是一回事。
    - 【2026-08-21 修复】此前这里传进来的一直是树里的中文 label（如"工业机器人
      与机械臂"），拼出的查询是"工业机器人与机械臂 manufacturer Germany"这种
      中英混合——跟上面这条注释说的意图不符，是本轮 NAICS 新树重写顺带发现并
      修的一个 bug。现在改成显式接收 label_en（新树每个节点新增的英文类目名），
      调用方（make_llm_generate_fn）不再传中文 label 进来。
    - 【扇出】单条宽查询的 top 结果被头部大厂霸榜，模型只有 8 条材料可写，
      产出自然全是大厂。按子区域拆多条窄查询，每个子区域各取 top 结果，
      材料面扩到 40+ 条；再拼上全球巨头负向排除词（Exa 支持 -term），
      把够不着的大厂从材料里洗掉。
    - 未知区域（树外结构/测试用例）：回退单条宽查询，行为同旧版。
    """
    excl = " " + " ".join(f"-{g}" for g in _GLOBAL_GIANT_EXCLUDES)
    sub_terms = _REGION_QUERY_TERMS.get((region_key or "").upper())
    if sub_terms:
        queries = [f"{label_en} manufacturer {t}{excl}" for t in sub_terms]
        queries.append(f"{label_en} manufacturers {region_label}{excl}")
    else:
        queries = [f"{label_en} manufacturers {region_label}{excl}".strip()]
    return queries


def _subcategory_queries(leaf_id: str, label_en: str, region_key: str, region_label: str) -> list[str]:
    """路径 A：子类目产品词 × 国家/子区域的查询——把"工业自动化"这类宽节点拆成
    机器人、CNC、PLC……这些更窄的产品语境（见 _LEAF_SUBTERMS），再逐个按国家搜。

    2026-08-14 修正：第一版只拼「子类目词 + 区域名」（不按国家展开），一个节点
    只有 2-4 个子类目词 = 2-4 条查询，跟路径 B 交替打包成轮次后，整个队列常常
    只够 4-5 轮就耗尽——实测 ia_robotics×EU 真机只挖出 22 家就没查询可用了，
    远低于约 100 家的软目标，且不是因为"挖尽"（连续无新增）或撞到轮数上限，
    是查询队列本身太短，循环提前没材料可搜了。改成子类目词 × 国家做真正的
    交叉，让路径 A 自己就有足够材料撑起整个循环——不用等着轮数上限或挖尽信号
    生效，队列本身不会先于这两个信号耗尽。这不会让单次运行变贵：真正花钱的是
    实际跑了几轮，由 _MAX_ROUNDS 卡死，队列变大只是让"还有没有材料可搜"不再
    是瓶颈。

    树外节点 / 不在 _LEAF_SUBTERMS 里的叶子（测试用例、未来新增节点忘了配词表）：
    回退成"类目名 + 区域"的单条查询，不报错、不阻断——子类目词表是锦上添花，
    不是生成能不能跑起来的前提条件。未知区域代号：回退成子类目词各配区域人话名
    （不按国家展开，行为同修正前）。label_en 同 _country_queries：2026-08-21 起
    改用英文类目名做兜底查询，不再用中文 label。
    """
    excl = " " + " ".join(f"-{g}" for g in _GLOBAL_GIANT_EXCLUDES)
    terms = _LEAF_SUBTERMS.get(leaf_id or "")
    if not terms:
        return [f"{label_en} manufacturer {region_label}{excl}".strip()]
    countries = _REGION_QUERY_TERMS.get((region_key or "").upper())
    if not countries:
        return [f"{t} {region_label}{excl}" for t in terms]
    return [f"{t} {c}{excl}" for t in terms for c in countries]


def build_generation_queries(label_en: str, region_key: str,
                             region_label: str) -> list[str]:
    """向后兼容包装：只保留原来的「类目 × 国家」扇出（路径 B），不含子类目。

    新代码走 build_query_queue；这个函数留着是因为它是个独立可测的纯函数，
    旧测试/旧调用点若还在引用它，行为不变。"""
    return _country_queries(label_en, region_key, region_label)


# 目录/协会式站点，按区域给一个大致对口的默认值——不追求精确覆盖，查不到就是
# 这一条 Exa 查询本身没结果，不影响其它查询，不需要为每个区域找到"标准答案"。
# Kompass 是全球性目录，各区域都留作兜底。
_DIRECTORY_SITES: dict[str, list[str]] = {
    "NA": ["thomasnet.com", "kompass.com"],
    "EU": ["europages.com", "kompass.com"],
    "SEA": ["kompass.com"],
    "EA": ["kompass.com"],
    "SA": ["kompass.com"],
}


def _directory_queries(label_en: str, region_key: str, region_label: str) -> list[str]:
    """路径 C：目录/协会/展会式查询——跟路径 A/B（都是"XX manufacturer 地名"这个
    句式）问法不同源。

    背景：Exa 这类网页搜索本质上还是在同一个索引里找，不管把"manufacturer"换成
    什么近义词、把地名换多细，问的都是同一批被搜索引擎收录的页面——对 SEO 存在感
    弱的中小长尾制造商，天花板不是"查询词不够多样"，是索引本身就没怎么收录它们。
    B2B 行业专门有一批为"穷举某行业某地区的公司"设计的结构化目录站点（ThomasNet/
    Europages/Kompass 等），用 site: 把这些目录已经被 Exa 收录的页面单独捞出来，
    是一条质地不同的材料来源，成本跟其它查询完全一样（都只是一次 Exa 调用），
    不是接入这些目录的 API（那是更大的改动，没做）。2026-08-21：label_en 同上，
    改用英文类目名。
    """
    excl = " " + " ".join(f"-{g}" for g in _GLOBAL_GIANT_EXCLUDES)
    sites = _DIRECTORY_SITES.get((region_key or "").upper(), ["kompass.com"])
    queries = [f"site:{s} {label_en} manufacturer {region_label}{excl}" for s in sites]
    queries.append(f'"member directory" {label_en} manufacturers association {region_label}{excl}')
    return queries


# 路径 D：本地语言限定词——2026-08-21 新增。背景：欧洲 Mittelstand、日韩的中坚
# 制造商官网主体常常是本地语言、英文页面单薄甚至没有，A/B/C 三条路径查询词全是
# 英文"manufacturer"，对这批公司系统性不利——它们很可能正是够不着 giant 门槛
# 又有独立采购权的理想客户（mid/large 档），现在的搜法先天找不到。
#
# 范围刻意收窄：只换"manufacturer"这一个通用限定词，产品名词本身继续用英文——
# 很多工业技术名词（PLC/VFD/servo 这类）在非英语国家的 B2B 官网上本来就直接用
# 英文/缩写，硬翻译成本地语言反而可能查不准，且 47 个节点 × 每种语言全套翻译
# 细分产品词表的维护成本和出错概率都太高。只覆盖本地语言网页占比明显高的市场
# （荷兰/北欧/比利时/瑞士/英国/加拿大英语覆盖率本来就高，不做）。
_LOCAL_MANUFACTURER_WORD: dict[str, str] = {
    # 欧洲
    "Germany": "Hersteller", "Austria": "Hersteller",
    "Italy": "produttore",
    "France": "fabricant",
    "Poland": "producent",
    "Czech Republic": "výrobce",
    "Spain": "fabricante",
    # 北美（美国/加拿大英语覆盖率高，不做；墨西哥做）
    "Mexico": "fabricante",
    # 东南亚（新加坡/马来西亚/菲律宾英语覆盖率高，不做）
    "Thailand": "ผู้ผลิต",
    "Vietnam": "nhà sản xuất",
    "Indonesia": "produsen",
    # 东亚
    "Japan": "メーカー",
    "South Korea": "제조업체",
    "Taiwan": "製造商",
    # 南美
    "Brazil": "fabricante",
    "Argentina": "fabricante",
    "Colombia": "fabricante",
}


def _localized_queries(label_en: str, region_key: str, region_label: str) -> list[str]:
    """路径 D：把"manufacturer"换成对应国家的本地语言限定词，产品名词保留英文
    （见上面 _LOCAL_MANUFACTURER_WORD 的设计取舍）。只对 _REGION_QUERY_TERMS 里
    该区域下、且在 _LOCAL_MANUFACTURER_WORD 里有登记的国家生效；一个区域一条都
    没有命中就返回空列表（不报错、不影响其它路径，跟路径 C 的"查不到就是这条
    没结果"是同一个纪律）。
    """
    excl = " " + " ".join(f"-{g}" for g in _GLOBAL_GIANT_EXCLUDES)
    countries = _REGION_QUERY_TERMS.get((region_key or "").upper()) or []
    queries = []
    for c in countries:
        word = _LOCAL_MANUFACTURER_WORD.get(c)
        if word:
            queries.append(f"{label_en} {word} {c}{excl}")
    return queries


def build_query_queue(leaf_id: str, label_en: str, region_key: str, region_label: str,
                      *, batch_size: int = None) -> list[list[str]]:
    """构造轮次化查询队列：交替编排"子类目×区域"（路径 A，产品细）、
    "类目×国家"（路径 B，地域细）、"目录/协会站点"（路径 C，跟 A/B 问法不同源，
    见 _directory_queries）、"本地语言限定词"（路径 D，见 _localized_queries）
    四条独立扇出路径，每 batch_size 条打包成一轮，供 loop-until-target 循环
    逐轮取用（纯函数，可单测）。

    2026-08-14：加入路径 C 是因为真机跑发现——同一个节点的轮次序列里经常出现
    "有的轮次新增个位数甚至 0"（如 ia_robotics×NA 12 轮里 5 轮零新增），排查
    后 A/B 两条路径问法本质相同（都是"XX manufacturer 地名"句式），问的是 Exa
    同一个索引，换问法换不出新索引。加一条"目录站点"路径，让部分轮次搜到质地
    不同的页面来源，同时也顺带拉长了整条队列（路径 C 每个节点新增 2-3 条查询），
    对"队列本身太短就耗尽"（另一类已知问题）也有一点缓解，但主要目的是换材料
    来源，不是单纯凑长度。

    2026-08-21：加入路径 D（本地语言限定词），同时把 A/B/C 三条路径此前误用
    中文 label 拼英文查询的 bug 一并修掉（都改成 label_en，见各函数 2026-08-21
    的改动说明）——这两件事一起做是因为它们都是"查询语言"这个同一个问题的
    两个层面，分开改容易顾此失彼。

    四条路径都各自独立扇出，不做交叉（52 节点 × 多国家 × 多目录站点会爆炸成本）；
    交替编排已经能在第一轮就同时拿到"产品细/地域细/目录源/本地语言"四种视角，
    命中率和成本之间更平衡；真正的成本上限由每个节点自己的 max_rounds（或
    _MAX_ROUNDS 兜底）卡死，不取决于队列有多长。
    去重：四条路径偶尔会拼出完全相同的查询字符串，保留先出现的那条即可。
    """
    if batch_size is None:
        batch_size = _ROUND_BATCH_SIZE
    a = _subcategory_queries(leaf_id, label_en, region_key, region_label)
    b = _country_queries(label_en, region_key, region_label)
    c = _directory_queries(label_en, region_key, region_label)
    d = _localized_queries(label_en, region_key, region_label)
    interleaved: list[str] = []
    seen: set[str] = set()
    for x, y, z, w in itertools.zip_longest(a, b, c, d):
        for q in (x, y, z, w):
            if q and q not in seen:
                seen.add(q)
                interleaved.append(q)
    if not interleaved:
        interleaved = [f"{label_en} manufacturers {region_label}".strip()]
    return [interleaved[i:i + batch_size] for i in range(0, len(interleaved), batch_size)]


def make_llm_generate_fn(prompt_path: str | Path):
    """返回 generate_fn(node)：轮次化跑生成提示词，解析并合并出富候选 JSON。

    走【直连模型调用】而不是 JarvisController——与 signal_collection 同构：
    每一轮内部仍是"搜索补料 + 一次文本补全"，不是工具调用循环。

    2026-08-07：联网检索不再假设"模型自带 :online"——迁移到 DeepSeek 官方 API 后
    这个前提不成立。改用 core/search_augment：按 core/model_capabilities 判断当前
    模型是不是真有内置联网，有则原样直连（零行为变化，仍在 OpenRouter :online 时
    完全不受影响）；没有则显式按"产品类目 + 区域"搜一遍，把结果拼进提示词再生成，
    不再让这步在迁移后静默退化成凭训练记忆瞎编候选公司。

    2026-08-13：单次搜索 + 单次生成改成【轮次化循环】（见模块注释「潜客生成 v3」）：
    从 build_query_queue() 产出的查询队列里逐轮取一小批查询去搜索+生成，与前面
    几轮的候选按 _company_key() 去重合并，直到满足以下任一条件才停：
      1. 累计候选数达到 _TARGET_CANDIDATES（软目标，500，几乎不会真正撞到）
      2. 连续 _DRY_ROUND_LIMIT 轮没有新增候选（挖尽）
      3. 查询队列耗尽（四条路径都搜过了）
      4. 触达这个节点的 max_rounds（成本护栏；树里配了就用树里的，没配用
         _MAX_ROUNDS 兜底——2026-08-21 起从全局单一值改成节点级差异化，见下方
         node_max_rounds）
    区域边界【不需要在这里额外加约束】——node 在进入本函数前已经由 select_node()
    锁定成一个不可变的单一 (类目, 区域)，本函数从头到尾的每一轮查询都只基于
    这一个 node 生成，没有任何分支会让它在循环过程中跑去搜别的区域。
    """
    template = Path(prompt_path).read_text(encoding="utf-8")

    async def _run(node: dict) -> list[dict]:
        import config
        from core.json_salvage import salvage_json_array, looks_truncated
        from core.search_augment import augment_with_search
        from core.llm import get_client

        region_label = (node.get("region_label") or node.get("region")
                        or (node.get("regions") or [""])[0] or "")
        region_key = (node.get("region") or (node.get("regions") or [""])[0] or "").upper()
        label = node.get("label") or ""
        # label_en：2026-08-21 新增字段，给搜索查询用的英文类目名——路径 B/C/D
        # 都靠它，不能再传中文 label 进去（见 build_query_queue 改动说明里的 bug
        # 记录）。树里没配（旧结构/测试用例）就退回中文 label，好过查询词整体
        # 拼不出来；不是长期方案，是不阻断的兜底。
        label_en = node.get("label_en") or label
        leaf_id = node.get("id") or ""
        node_max_rounds = node.get("max_rounds") or _MAX_ROUNDS

        queue = build_query_queue(leaf_id, label_en, region_key, region_label)

        client = get_client(timeout=540)   # 大批量生成给长超时（core/llm 单一构建点）
        collected: dict[str, dict] = {}   # _company_key() -> record，跨轮去重合并
        dry_rounds = 0
        rounds_run = 0
        last_text_len = 0

        for round_queries in queue:
            if rounds_run >= node_max_rounds:
                print(f"[prospect.generate] {label}·{region_label}：达到最大轮数保护"
                      f"（{node_max_rounds}），停止（已收集 {len(collected)} 家）")
                break
            if len(collected) >= _TARGET_CANDIDATES:
                break
            if dry_rounds >= _DRY_ROUND_LIMIT:
                print(f"[prospect.generate] {label}·{region_label}：连续 {_DRY_ROUND_LIMIT} "
                      f"轮无新增，停止（已收集 {len(collected)} 家）")
                break

            rounds_run += 1
            prompt = render_generation_prompt(template, node)
            prompt = await augment_with_search(
                prompt, round_queries, max_results_per_query=8,
                label=f"prospect_daily:{label}·{region_label}·round{rounds_run}")
            if collected:
                # 告诉模型这是接续第几轮，并把已收集的公司名列出来——降低模型
                # 在同一个节点内重复输出同一批公司的概率（跨节点的重复不用管，
                # 见 prompt 里原有的「去重」章节：不同天扫的是不同节点）。
                already = "、".join(sorted({r.get("company_name", "")
                                            for r in collected.values()
                                            if r.get("company_name")})[:120])
                prompt += (
                    f"\n\n---\n\n【本轮补充说明】这是同一个（类目、区域）任务的第 "
                    f"{rounds_run} 轮检索，目的是把已有名单从 {len(collected)} 家继续"
                    f"补到约 {_TARGET_CANDIDATES} 家。以下是前面几轮已经找到、"
                    f"【不要在本轮重复输出】的公司：{already}"
                )

            resp = await client.chat.completions.create(
                model=config.CLAUDE_MODEL,
                max_tokens=_GEN_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            text = (resp.choices[0].message.content or "") if resp.choices else ""
            last_text_len = len(text)

            parsed = salvage_json_array(text)       # 截断也能保住已生成的那几十家
            records, meta = split_meta(parsed)      # 剥掉汇总条目，别让它冒充一家公司
            if meta.get("coverage_note"):
                # 「这个类目还能不能再挖」——过去提示词让模型写在 JSON 外面，
                # 与"只输出合法 JSON"直接冲突。现在给它数组内的合法位置。
                tail = "（自称已挖尽）" if meta.get("exhausted") else ""
                print(f"[prospect.generate] {label}·{region_label} 第{rounds_run}轮"
                      f"覆盖度自述：{meta['coverage_note']}{tail}")
            if looks_truncated(text):
                # 抢救成功但确实被切了：留个痕迹，便于判断要不要再抬预算或改分批
                print(f"[prospect.generate] ⚠ {label}·{region_label} 第{rounds_run}轮"
                      f"输出疑似被 max_tokens({_GEN_MAX_TOKENS}) 截断，已抢救 {len(records)} 条")

            new_count = 0
            for r in records:
                key = _company_key(r)
                if key == "|" or key in collected:
                    continue
                collected[key] = r
                new_count += 1
            dry_rounds = 0 if new_count else dry_rounds + 1
            print(f"[prospect.generate] {label}·{region_label} 第{rounds_run}轮：新增 "
                  f"{new_count} 家（累计 {len(collected)}/{_TARGET_CANDIDATES}）")

            if meta.get("exhausted") and new_count == 0:
                print(f"[prospect.generate] {label}·{region_label}：模型自称已挖尽"
                      "且本轮无新增，提前停止")
                break
        else:
            # for-else：循环正常走完（没有被上面任何一个 break 打断）才会进这里——
            # 说明四个停止条件（目标/挖尽/最大轮数/模型自称已挖尽）一个都没触发，
            # 是【查询队列本身用完了】。这本身不是错误（未配子类目词表的叶子、
            # 或某些区域材料确实有限时会正常发生），但如果离目标还差很远，
            # 说明材料面不够撑起循环，需要人来判断是加更多子类目词/国家、还是
            # 承认这个节点真实合格公司就是不多——不该被静默吞掉，见首次真机跑
            # ia_robotics×EU 只挖出 22 家、原因正是这里、却没有任何日志能看出来。
            if len(collected) < _TARGET_CANDIDATES:
                print(f"[prospect.generate] {label}·{region_label}：查询队列已耗尽"
                      f"（共 {rounds_run} 轮），未达软目标（已收集 "
                      f"{len(collected)}/{_TARGET_CANDIDATES} 家）。若这个类目/区域"
                      "真实合格公司数量本就有限，属正常；若怀疑材料不够，"
                      "考虑给 _LEAF_SUBTERMS 里这个叶子补更多子类目词。")

        records = list(collected.values())
        if not records:
            # 明确报错而不是静默返回空——否则工作流会"显示成功但名单是空的"。
            # （workflow 里 generate 步 retries=1，会再试一次。）
            raise RuntimeError(
                f"生成未解析出任何候选（跑了 {rounds_run} 轮，最近一轮模型输出 "
                f"{last_text_len} 字）。可能是模型没联网/没按 JSON 输出，或输出为空。"
            )
        return records

    def generate_fn(node: dict):
        return _run(node)   # 返回 coroutine；workflow runner 会 await

    return generate_fn
