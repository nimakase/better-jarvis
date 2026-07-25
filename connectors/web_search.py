"""
connectors/web_search.py — 全局结构化搜索工具（AnySearch，可选增强）

定位：这是【通用】搜索能力，不属于任何单条业务流程——对话、子 agent、信号采集、
潜客、事实核查……凡是"要上网查"的地方都可复用（已入默认常驻工具 + 只读，
子 agent 白名单天生继承）。

护栏与降级（与全局一贯风格一致）：
  - effect=read_external（只读外部）；结果是 untrusted，controller 的污染闸会据此
    禁止本回合的对外动作——防提示注入。
  - 未配 key / 超出每日额度 / 调用失败 → 不硬报错，而是【退回信号】：返回一段文本
    告诉模型"结构化搜索不可用（原因），请改用你自带的联网能力作答"。主模型带
    :online，因此能力不塌，只是从"结构化搜索"降级回"模型自带联网"。
  - 用量落遥测（core.telemetry.record_delivery 复用不合适，另记 tool_calls 已够；
    这里额外用一张轻计数，避免盲目超额被掐）。

解析已按【真实文档】(anysearch.com/docs, 2026-07 核对) 收紧：
  请求 POST /v1/search, Bearer 鉴权, body {query, max_results, [tag], [params]}
  成功 {"code":0,"message":"success","data":{"results":[{title,url,snippet,content}],
        "metadata":{total_results,search_time_ms}}}
  失败 {"code":非0,"message":"...","request_id":...}；HTTP 401/403 鉴权、429 限流。
仍保留少量信封兼容作为二次兜底。
"""
from __future__ import annotations

import json
from datetime import date
from functools import partial

import config
from core import effects
from core.registry import tool as _tool

tool = partial(_tool, group="general")

# ── 每日用量计数（防盲目超额）──────────────────────────────────────────────
_USAGE = {"date": "", "count": 0}


def _usage_today() -> int:
    today = date.today().isoformat()
    if _USAGE["date"] != today:
        _USAGE["date"], _USAGE["count"] = today, 0
    return _USAGE["count"]


def _bump_usage() -> None:
    _usage_today()
    _USAGE["count"] += 1


def usage_status() -> dict:
    return {"date": _USAGE["date"], "count": _usage_today(),
            "cap": config.ANYSEARCH_DAILY_CAP}


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
    return (f"[结构化搜索不可用：{reason}] 请改用你自带的实时联网能力（:online）直接作答；"
            f"若涉及需精确核实的事实，明确标注是基于联网检索、不确定处不要编造。")


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

    # 无 key → 直接退回联网
    if not config.ANYSEARCH_API_KEY:
        return _fallback_note("未配置 AnySearch key")

    # 超额保护 → 退回联网
    if _usage_today() >= config.ANYSEARCH_DAILY_CAP:
        return _fallback_note(f"今日已达自设用量上限 {config.ANYSEARCH_DAILY_CAP}")

    ok, res = await _call_anysearch(query, max_results)
    _bump_usage()
    if not ok:
        return _fallback_note(str(res))

    lines = [f"🔎 「{query}」搜索结果（AnySearch · 今日第 {_usage_today()} 次）："]
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
    "web_search_usage",
    "查看今日 AnySearch 搜索用量（已用次数 / 每日上限）。用户问「今天搜了多少次/还剩多少额度」时用。只读。",
    {"type": "object", "properties": {}},
    effect=effects.READ_LOCAL,
)
async def web_search_usage() -> str:
    u = usage_status()
    if not config.ANYSEARCH_API_KEY:
        return "未配置 AnySearch，web_search 当前走模型自带联网（无额度概念）。"
    return (f"AnySearch 今日用量：{u['count']} / {u['cap']} 次"
            f"（免费档官方上限 1000/天，此处自设软上限 {u['cap']} 留余量）。")
