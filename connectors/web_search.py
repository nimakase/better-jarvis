"""
connectors/web_search.py — 全局结构化搜索工具（Exa 优先，AnySearch 兜底，可选增强）

定位：这是【通用】搜索能力，不属于任何单条业务流程——对话、子 agent、信号采集、
潜客、事实核查……凡是"要上网查"的地方都可复用（已入默认常驻工具 + 只读，
子 agent 白名单天生继承）。

后端选择顺序（2026-08-07 新增 Exa）：
  1. Exa（配了 EXA_API_KEY 才启用）——免费额度 2万次/月，多语言召回实测优于同价位
     竞品，DeepSeek 迁移后作为主搜索后端。
  2. AnySearch（配了 ANYSEARCH_API_KEY 才启用）——Exa 未配置或当日调用失败时兜底。
  3. 都不可用 → 退回信号（见 _fallback_note）：明确告知模型搜索不可用，而不是
     假装还能查——是否建议改用模型自带 :online，取决于当前配置的模型是否真有。

护栏与降级（与全局一贯风格一致）：
  - effect=read_external（只读外部）；结果是 untrusted，controller 的污染闸会据此
    禁止本回合的对外动作——防提示注入。
  - 未配 key / 超出每日额度 / 调用失败 → 不硬报错，而是按顺序尝试下一个后端，
    全部失败才退回信号。
  - 用量落遥测（core.telemetry.record_delivery 复用不合适，另记 tool_calls 已够；
    这里额外用一张轻计数，避免盲目超额被掐），Exa/AnySearch 各自独立计数。

AnySearch 解析已按【真实文档】(anysearch.com/docs, 2026-07 核对) 收紧：
  请求 POST /v1/search, Bearer 鉴权, body {query, max_results, [tag], [params]}
  成功 {"code":0,"message":"success","data":{"results":[{title,url,snippet,content}],
        "metadata":{total_results,search_time_ms}}}
  失败 {"code":非0,"message":"...","request_id":...}；HTTP 401/403 鉴权、429 限流。
仍保留少量信封兼容作为二次兜底。

Exa 契约（官方文档 exa.ai/docs，2026-08 核对）：
  请求 POST /search, header x-api-key，body {query, numResults, contents:{text:true}}
  成功 {"results":[{title,url,publishedDate,author,text,score,...}], "requestId":...}
  失败 4xx/5xx，body 里通常带 {"error":"..."}. 本文件按此实现，未做过真实网络联调
  （沙箱环境出站网络受限）——首次真实调用请留意日志，若响应结构与此不符会走
  防御式解析（_extract_exa_results 认不出字段就返回空 → 触发降级，不会崩，但
  可能拿到空结果，届时按实际返回结构调整字段名映射即可。
"""
from __future__ import annotations

import json
from datetime import date
from functools import partial

import config
from core import effects
from core.registry import tool as _tool

tool = partial(_tool, group="general")

# ── 每日用量计数（防盲目超额，Exa/AnySearch 独立计数）───────────────────────
_USAGE = {"date": "", "count": 0}                    # AnySearch（沿用原有变量名，向后兼容）
_EXA_USAGE = {"date": "", "count": 0}


def _usage_today() -> int:
    today = date.today().isoformat()
    if _USAGE["date"] != today:
        _USAGE["date"], _USAGE["count"] = today, 0
    return _USAGE["count"]


def _bump_usage() -> None:
    _usage_today()
    _USAGE["count"] += 1


def _exa_usage_today() -> int:
    today = date.today().isoformat()
    if _EXA_USAGE["date"] != today:
        _EXA_USAGE["date"], _EXA_USAGE["count"] = today, 0
    return _EXA_USAGE["count"]


def _bump_exa_usage() -> None:
    _exa_usage_today()
    _EXA_USAGE["count"] += 1


def usage_status() -> dict:
    return {"date": _USAGE["date"], "count": _usage_today(),
            "cap": config.ANYSEARCH_DAILY_CAP,
            "exa_date": _EXA_USAGE["date"], "exa_count": _exa_usage_today(),
            "exa_cap": config.EXA_DAILY_CAP}


# ── 响应解析（防御式）────────────────────────────────────────────────────────

def _extract_results(data) -> list[dict]:
    """按真实契约 data.results 抽结果；保留 content（整页抽取，价值高）。
    带少量信封兜底以防端点小改。找不到返回 []。"""
    items = []
    if isinstance(data, dict):
        d = data.get("data")
        if isinstance(d, dict):                      # 真实结构：data.results
            items = d.get("results") or []
        if not items:                                # 兜底：顶层 results / 其它别名
            items = (data.get("results") or data.get("items")
                     or data.get("hits") or [])
    elif isinstance(data, list):
        items = data
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        out.append({
            "title": it.get("title") or it.get("name") or "",
            "url": it.get("url") or it.get("link") or "",
            "snippet": (it.get("snippet") or it.get("summary")
                        or it.get("description") or ""),
            "content": it.get("content") or "",      # 整页正文（常已足够，免再抓页）
        })
    return out


def _fallback_note(reason: str) -> str:
    """AnySearch 不可用时的退回提示。

    2026-08-07：此前无条件建议"改用你自带的 :online 联网"——这个前提只在主模型
    走 OpenRouter :online 时成立。一旦迁移到不带内置联网的模型（如 DeepSeek 官方
    API），这句话会变成误导：模型会误以为自己还能联网，进而可能编造出看似最新
    的内容。改为按当前配置的模型是否真带 :online 分支，如实告知。
    """
    if ":online" in (config.CLAUDE_MODEL or ""):
        return (f"[结构化搜索不可用：{reason}] 请改用你自带的实时联网能力（:online）直接作答；"
                f"若涉及需精确核实的事实，明确标注是基于联网检索、不确定处不要编造。")
    return (f"[结构化搜索不可用：{reason}] 你当前没有其它联网渠道可用。"
            f"请基于已有知识作答，并明确告知用户这条信息未经实时核实、可能已过时，"
            f"绝不要编造看起来像最新检索结果的内容。")


def _extract_exa_results(data) -> list[dict]:
    """按 Exa 官方契约 {"results":[...]} 抽结果；字段名跟 AnySearch 不同，统一成
    跟 _extract_results 相同的输出形状（title/url/snippet/content），上层不用区分
    来自哪个后端。认不出结构 → 返回 []（触发降级，不崩）。"""
    items = data.get("results") if isinstance(data, dict) else None
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        text = it.get("text") or it.get("highlights") or ""
        if isinstance(text, list):   # highlights 有时是列表
            text = " ".join(str(t) for t in text)
        out.append({
            "title": it.get("title") or "",
            "url": it.get("url") or "",
            "snippet": (it.get("summary") or (text[:200] if text else "")),
            "content": text,
        })
    return out


async def _call_exa(query: str, max_results: int) -> tuple[bool, object]:
    """调 Exa。返回 (ok, results 或错误原因)。"""
    import httpx
    headers = {"x-api-key": config.EXA_API_KEY, "Content-Type": "application/json"}
    payload = {"query": query, "numResults": max_results, "contents": {"text": True}}
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            r = await client.post(config.EXA_BASE_URL, json=payload, headers=headers)
    except Exception as e:  # noqa: BLE001
        return False, f"网络错误：{type(e).__name__}"
    if r.status_code in (401, 403):
        return False, "鉴权失败（key 无效/已禁用/已过期）"
    if r.status_code == 429:
        return False, "触发限流/超额（429）"
    if r.status_code >= 400:
        try:
            body = r.json()
            err = body.get("error") if isinstance(body, dict) else None
        except Exception:
            err = None
        return False, f"HTTP {r.status_code}" + (f"：{err}" if err else "")
    try:
        data = r.json()
    except Exception:
        return False, "返回非 JSON"
    results = _extract_exa_results(data)
    if not results:
        import logging
        logging.getLogger("jarvis.web_search").warning(
            "Exa 200 但未解析出结果，顶层结构：%s",
            list(data.keys()) if isinstance(data, dict) else type(data).__name__)
        return False, "结果无命中或结构异常"
    return True, results


async def _call_anysearch(query: str, max_results: int) -> tuple[bool, object]:
    """调 AnySearch。返回 (ok, results 或错误原因)。"""
    import httpx
    headers = {"Authorization": f"Bearer {config.ANYSEARCH_API_KEY}",
               "Content-Type": "application/json"}
    # 真实契约字段（不传 tag → 服务端按查询意图自动路由至最佳数据源）。
    payload = {"query": query, "max_results": max_results}
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            r = await client.post(config.ANYSEARCH_BASE_URL, json=payload, headers=headers)
    except Exception as e:  # noqa: BLE001
        return False, f"网络错误：{type(e).__name__}"
    if r.status_code in (401, 403):
        return False, "鉴权失败（key 无效/已禁用/已过期）"
    if r.status_code == 429:
        retry = r.headers.get("Retry-After", "")
        return False, f"触发限流/超额（429{'，Retry-After ' + retry + 's' if retry else ''}）"
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}"
    try:
        data = r.json()
    except Exception:
        return False, "返回非 JSON"
    # 真实契约：code==0 成功，非 0 为业务错误（message 是人可读原因，如缺 tag 参数）
    if isinstance(data, dict) and data.get("code") not in (0, None):
        return False, f"API 错误：{data.get('message') or data.get('code')}"
    results = _extract_results(data)
    if not results:
        import logging
        logging.getLogger("jarvis.web_search").warning(
            "AnySearch 200 但未解析出结果，顶层结构：%s",
            list(data.keys()) if isinstance(data, dict) else type(data).__name__)
        return False, "结果无命中或结构异常"
    return True, results


def _render_results(query: str, res: list[dict], max_results: int, backend: str, seq: int) -> str:
    lines = [f"🔎 「{query}」搜索结果（{backend} · 今日第 {seq} 次）："]
    for i, r in enumerate(res[:max_results], 1):
        title = r["title"] or "(无标题)"
        url = f"\n   {r['url']}" if r["url"] else ""
        # 优先展示 content（整页抽取，常已足够精读，免再 fetch_page）；无则用 snippet
        body = r.get("content") or r.get("snippet") or ""
        body = f"\n   {body[:400]}" if body else ""
        lines.append(f"{i}. {title}{url}{body}")
    lines.append("\n（以上为联网检索结果，属外部信息；据此作答时注意甄别，不确定处不要编造。）")
    return "\n".join(lines)


@tool(
    "web_search",
    "联网搜索，返回结构化结果（标题/网址/摘要）。任何需要查【当前、真实世界】信息的"
    "场景都用它：查某公司/产品/料号、核实事实、找资料、了解近况等。比凭记忆作答可靠。"
    "只读。（底层优先用 AnySearch 结构化搜索；不可用时自动退回模型自带联网。）",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索词，尽量具体（含公司名/型号/地区等）"},
            "max_results": {"type": "integer", "description": "返回条数，默认 5，上限 10"},
        },
        "required": ["query"],
    },
    effect=effects.READ_EXTERNAL,
)
async def web_search(query: str, max_results: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        return "搜索词为空。"
    max_results = max(1, min(int(max_results or 5), 10))

    tried: list[str] = []

    # 1) Exa 优先（配了 key 且未超每日软上限）
    if config.EXA_API_KEY:
        if _exa_usage_today() < config.EXA_DAILY_CAP:
            ok, res = await _call_exa(query, max_results)
            _bump_exa_usage()
            if ok:
                return _render_results(query, res, max_results, "Exa", _exa_usage_today())
            tried.append(f"Exa失败({res})")
        else:
            tried.append(f"Exa已达今日上限{config.EXA_DAILY_CAP}")

    # 2) AnySearch 兜底
    if config.ANYSEARCH_API_KEY:
        if _usage_today() < config.ANYSEARCH_DAILY_CAP:
            ok, res = await _call_anysearch(query, max_results)
            _bump_usage()
            if ok:
                return _render_results(query, res, max_results, "AnySearch", _usage_today())
            tried.append(f"AnySearch失败({res})")
        else:
            tried.append(f"AnySearch今日已达自设用量上限{config.ANYSEARCH_DAILY_CAP}")

    # 3) 都不可用 → 如实退回
    if not tried:
        return _fallback_note("未配置任何搜索后端（EXA_API_KEY / ANYSEARCH_API_KEY 均为空）")
    return _fallback_note("；".join(tried))


@tool(
    "web_search_usage",
    "查看今日 AnySearch 搜索用量（已用次数 / 每日上限）。用户问「今天搜了多少次/还剩多少额度」时用。只读。",
    {"type": "object", "properties": {}},
    effect=effects.READ_LOCAL,
)
async def web_search_usage() -> str:
    u = usage_status()
    lines = []
    if config.EXA_API_KEY:
        lines.append(f"Exa 今日用量：{u['exa_count']} / {u['exa_cap']} 次"
                     f"（免费额度 2万/月≈666/天，此处自设软上限 {u['exa_cap']} 留余量）。")
    if config.ANYSEARCH_API_KEY:
        lines.append(f"AnySearch 今日用量：{u['count']} / {u['cap']} 次"
                     f"（免费档官方上限 1000/天，此处自设软上限 {u['cap']} 留余量）。")
    if not lines:
        return "未配置任何结构化搜索后端，web_search 当前走退回信号（不假装能联网）。"
    return "\n".join(lines)
