"""
注册 jarvis 工作流（导入即注册，由 main 在启动时加载）。

当前注册：
  - prospect_daily（今日潜客名单）：选节点（或接续存盘批）→ 联网生成候选 →
    HubSpot 匹配富化 → 排序 → 出富 xlsx 并把"今日名单"落库喂情报台卡。
    未登录 HubSpot 时不出半成品名单：候选存盘、通知去登录，登录后重跑直接续。
  - signal_collection（采集今日信号）：独立实例跑采集提示词 → 联网广扫 →
    解析结构化信号 JSON → 入库（喂情报台看板与市场情报日报）。

报告类产出由报告框架（core/reports + generate_report）承担，不在此重复成工作流。
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from core import workflow as wf
from core import workflow_registry as wr

# 采集步骤专用的生成上限：信号采集要吐一大坨 JSON，远超通用对话的
# config.MAX_TOKENS_RESPONSE（4096，那是给聊天回复定的护栏）。这里给采集
# 单独的高预算，避免 JSON 被截断导致整批解析失败、入库 0 条。可用 env 覆盖。
_COLLECT_MAX_TOKENS = int(os.environ.get("JARVIS_SIGNAL_COLLECT_MAX_TOKENS", "16000"))


# 抢救式解析已提到 core/json_salvage 供【采集】与【潜客生成】共用——
# 两条路都是"让模型吐一大坨 JSON"，都会撞 max_tokens，逻辑不该分叉两份。
from core.json_salvage import salvage_json_array as _parse_signal_array  # noqa: E402
from core import delivery as _delivery

# 落库给情报台「今日名单」卡读的字段（v0.4 去掉信号字段；v0.5 去掉 seen_before，
# 历史库已整条移除）
_TOP_FIELDS = ("rank", "company_name", "crm_state", "hubspot_owner",
               "category", "confidence", "website")


def _store_dir() -> Path:
    import config
    d = config.DATA_DIR / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store_today_prospects(records: list, res: dict, node: dict | None = None) -> None:
    """把本次产出的名单落成结构化 JSON，供情报台「今日名单」卡读取。

    v0.4 去掉 signal_stale / signal_age_days（潜客已与信号解耦，名单跟信号新鲜度
    毫无关系，留着只会在卡片上挂一句误导人的提示）；改带 node_label——现在节点是
    (产品类目 × 区域)，卡片上说清今天扫的是哪个类目哪个区域更有用。
    v0.5 去掉 degraded：未登录时不再出半成品名单（存盘-通知-续跑），
    能走到这里的名单一定是匹配过的。
    """
    items = [{k: r.get(k) for k in _TOP_FIELDS} for r in (records or [])[:50]]
    node = node or {}
    label = node.get("label") or ""
    if label and node.get("region_label"):
        label = f'{label} · {node["region_label"]}'
    payload = {
        "date": date.today().isoformat(),
        "count": len(records or []),
        "node_label": label,
        "xlsx_path": (res or {}).get("path"),
        "items": items,
    }
    (_store_dir() / "today_prospects.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_today_prospects() -> dict | None:
    p = _store_dir() / "today_prospects.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _build_prospect_daily():
    """运行时组装潜客工作流（注入浏览器/agent 等运行时依赖）。"""
    import config
    from prospecting import workflows as pw

    # 潜客树：jarvis 拥有自己的副本（data/prospect_tree.json）并就地推进（mark_done 写回）。
    # 解析顺序：env JARVIS_PROSPECT_TREE（如需指向别处）→ 仓库 data/ 副本 → DATA_DIR。
    owned = config.BASE_DIR / "data" / "prospect_tree.json"
    candidates = []
    if getattr(config, "PROSPECT_TREE_PATH", ""):
        candidates.append(Path(config.PROSPECT_TREE_PATH))
    candidates.append(owned)
    candidates.append(config.DATA_DIR / "prospect_tree.json")
    tree_path = next((p for p in candidates if p.exists()), owned)
    prompt_path = Path(__file__).resolve().parent / "prospect_generation_prompt.md"

    generate_fn = pw.make_llm_generate_fn(prompt_path)
    # preflight/enrich 是 async 包装：浏览器整个生命周期在专属线程里
    # （sync Playwright 拒绝在事件循环线程运行 + 整批匹配不能冻住 app）
    preflight_fn, enrich_fn, close = pw.make_hubspot_runtime()
    base_output = pw.make_output_fn(config.DATA_DIR / "prospects", "prospect", "今日潜客名单")

    def output_fn(ctx):
        res = base_output(ctx) or {}
        _store_today_prospects(ctx.get("enrich") or [], res, ctx.get("node"))
        return res

    steps = pw.build_prospect_workflow(
        tree_path=tree_path,
        generate_fn=generate_fn, preflight_fn=preflight_fn,
        enrich_fn=enrich_fn, output_fn=output_fn,
        pending_path=config.DATA_DIR / "prospects" / "pending_batch.json",
    )
    return {"steps": steps, "context": {}, "cleanup": close}


wr.register_workflow(
    "prospect_daily", "今日潜客名单",
    "选潜客树节点 → 联网生成候选 → HubSpot 匹配富化 → 排序 → 出富 xlsx 并落「今日名单」",
    _build_prospect_daily, confirm=True, dispatch="detach",
    needs="需 prospect_tree.json 与已登录 HubSpot；未登录则本批候选存盘并通知，登录后重跑直接续跑匹配",
)


# 采集要覆盖的搜索主题（2026-08-07 新增，配合 core/search_augment 的 DeepSeek 迁移
# 补偿机制）。signal_collection_prompt.md 要求覆盖全行业多赛道多信号类型，一条
# 查询覆盖不了这么广；这里选的是提示词自己标注"余料含义"最高的几类信号
# （停产/关厂/减值/过剩/裁员/并购）加一条通用行情，在有限查询数内尽量换到高
# 信息密度。⚠️ 这组查询是第一版起点，不是定论——实际信号覆盖广度/质量如何，
# 要看过几天真实产出后再调（比如按当前空窗的赛道追加更针对性的查询）。
_COLLECT_SEARCH_QUERIES = [
    "electronics component shortage oversupply price cut 2026",
    "electronics manufacturer plant closure factory shutdown 2026",
    "semiconductor MCU FPGA inventory write-down excess stock 2026",
    "electronics industry layoffs restructuring 2026",
    "electronics component distributor merger acquisition 2026",
]


# ── 信号采集工作流（替代"在对话里让模型自己搜+乱建工具"的不可靠路径）──────────────
async def _completion_text(prompt: str, retries: int = 1) -> str:
    """流式累积一次大补全；传输错误/空输出自动重试 retries 次。

    流式的意义：非流式下响应体截断 = SDK 解析炸整批；流式下截断 = 提前收流，
    已累积的文本仍然可用。单测可 monkeypatch 本函数（签名保持 (prompt, retries=1)
    不变——联网检索补偿 2026-08-07 起挪到调用方 collect() 里做，见下方，
    不在这里加参数，因为已有测试会整函数替身成这个签名）。
    """
    import asyncio as _asyncio
    import config
    from core.llm import get_client
    client = get_client(timeout=float(os.environ.get("JARVIS_COLLECT_TIMEOUT", "540")))
    last_err = None
    for attempt in range(retries + 1):
        text = ""
        try:
            stream = await client.chat.completions.create(
                model=config.CLAUDE_MODEL,
                max_tokens=_COLLECT_MAX_TOKENS,     # 采集专用高上限
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is not None and delta.content:
                    text += delta.content
        except Exception as e:  # noqa: BLE001 — 传输中断：已累积的照样返回
            last_err = e
        if text.strip():
            return text
        if attempt < retries:
            await _asyncio.sleep(3)                  # 空手而归才重试
    if last_err is not None:
        raise RuntimeError(f"采集调用失败（重试 {retries} 次后仍无输出）：{last_err}")
    return ""


def _build_signal_collection():
    """采集今日全行业信号：独立 controller 跑固定采集提示词 → 解析 JSON → 入库。

    用工作流把采集框死：提示词要求"只输出 JSON 数组"，独立实例靠 :online 联网检索，
    全程确定性、不发挥、不自建工具。产出入库后即喂情报台看板与市场情报日报。
    """
    from intel import signal_library as sl

    prompt_path = Path(__file__).resolve().parent / "signal_collection_prompt.md"
    template = prompt_path.read_text(encoding="utf-8")

    async def collect(ctx: dict) -> list:
        # 采集不需要工具循环，只要一次文本补全。
        # 实测教训（2026-07-22）：非流式请求 + 大 max_tokens 的响应体巨大，传输
        # 中途被截断时 SDK 解析【整个响应体 JSON】直接抛
        # `Expecting value: line N column 1` ——整批归零，salvage 根本没机会上场。
        # 改为【流式累积】：流被切断只是提前结束，已到手的部分照常走抢救式解析，
        # 「传输层截断」从整批失败降级为少最后几条。外加一次自动重试。
        #
        # 2026-08-07：联网检索不再假设"模型自带 :online"——在这里（调用方）先经
        # core/search_augment 判断，没有内置联网才显式搜一遍拼进提示词，模型本身
        # 带联网则原样直连，零行为变化。放在 collect() 而不是 _completion_text()
        # 里做，是因为 _completion_text 的签名被已有测试整函数替身过，不能改。
        from core.search_augment import augment_with_search
        prompt = await augment_with_search(template, _COLLECT_SEARCH_QUERIES,
                                           max_results_per_query=6, label="signal_collection")
        text = await _completion_text(prompt)
        signals = _parse_signal_array(text)     # 抢救式解析，容忍尾部截断
        if not signals:
            # 明确报错而不是静默返回空——否则工作流会"显示成功但库是空的"。
            raise RuntimeError(
                f"采集未解析出任何信号（模型输出 {len(text)} 字）。"
                "可能是模型没联网/没按 JSON 输出，或输出为空。"
            )
        return signals

    def ingest(ctx: dict) -> dict:
        signals = ctx.get("collect") or []
        result = sl.ingest_signals(signals)
        # 拿到了信号却一条都没落库（多半是 signal_type/scope 枚举不合规被逐条丢弃），
        # 同样明确报错，把"静默 0 入库"暴露出来，便于定位。
        if result.get("inserted", 0) == 0 and result.get("merged", 0) == 0:
            raise RuntimeError(
                f"解析到 {len(signals)} 条信号但入库 0 条"
                f"（skipped={result.get('skipped', 0)}）。"
                "多半是 signal_type/scope 枚举与约定不符，被入库层丢弃。"
            )
        return result

    def expire(ctx: dict) -> dict:
        """把过老的信号标成 expired，让 `signals.status` 这一列**说真话**。

        为什么必须有人调它：`expire_stale()` 原先写好了却**没有任何调用方**，
        于是所有信号永远是 `active`。这对现有消费方无害（query_for_node /
        detect_hot_sectors 靠 `decayed_strength` 收敛，老信号强度自己衰减到约等于 0；
        日报靠 `days` 窗口），但它埋了个陷阱：`status` 长得像「活跃/过期」的开关，
        实际恒为 active——将来任何人写 `WHERE status='active'` 都会以为拿到的是
        新鲜信号，实际拿到全部历史。不报错，只静默给错结果。

        接在 ingest **之后**：本次采到的信号刚刷新 date_collected，不会被误伤；
        而且合并逻辑会把再次见到的老信号复活成 active，所以反复出现的行情不会被清掉。
        on_error=skip：这是收拾屋子，塌了也不该让今天的采集算失败。
        """
        n = sl.expire_stale()
        return {"expired": n}

    async def notify(ctx: dict) -> dict:
        """采集完成 → 生成市场情报日报 PDF，把摘要 + PDF 作为【一条】投递发出。

        与潜客名单业务逻辑对齐：信号是原料、市场情报日报（PDF）才是成品。此前信号
        采集只发一句文字摘要、没有可翻开的成品；现在顺手渲染日报 PDF 作为附件一并
        发出（飞书文件消息）。PDF 生成失败不影响文字摘要照发（best-effort）。
        ctx["output"] 供框架 _notify_done 把 PDF 登记进产物图书馆。
        """
        from intel import signal_library as sl
        grouped = sl.query_report(days=1)
        total = sum(len(v) for v in grouped.values())
        counts = sorted(
            [(st, len(lst)) for st, lst in grouped.items()],
            key=lambda x: x[1], reverse=True,
        )
        summary_parts = [f"{st}×{n}" for st, n in counts]
        high_impl = sum(
            1 for lst in grouped.values()
            for s in lst if s.get("surplus_implication", 0) >= 3
        )
        content = (
            f"采集到 {total} 条信号：" + "、".join(summary_parts) + "。"
            + (f"其中高余料含义信号 {high_impl} 条。" if high_impl else "")
        )
        # 成品：市场情报日报 PDF（14 天窗口，与 query_report 同口径）。
        # best-effort——渲染失败只丢附件、摘要照发，并在文字里如实说明。
        pdf_path = None
        try:
            from intel import report as _report
            pdf_path = await _report.generate_report_pdf(days=14)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger("jarvis").warning("信号采集日报 PDF 生成失败：%s", e)
            content += "\n（日报 PDF 生成失败，仅文字摘要；原因见日志）"
        result = _delivery.deliver(
            track="signal_collection",
            title="📡 今日信号采集完成",
            content=content,
            severity="normal",
            attachments=[pdf_path] if pdf_path else None,
        )
        if pdf_path:
            ctx["output"] = {"path": pdf_path}   # 供 _notify_done 登记产物
        return {"pushed": result.get("delivered", False), "total": total,
                "pdf": pdf_path}
    return [
        wf.Step("collect", collect),
        wf.Step("ingest", ingest),
        wf.Step("expire", expire, on_error="skip"),
        wf.Step("notify", notify, on_error="skip"),
    ]


wr.register_workflow(
    "signal_collection", "采集今日信号",
    "独立实例联网广扫电子元件/整机制造行业 → 结构化信号 JSON → 入库（喂情报台看板与市场情报日报）",
    _build_signal_collection, confirm=False, dispatch="detach",
    needs="依赖模型联网检索（:online）；约耗 1–3 分钟，产出当日信号入库",
)
