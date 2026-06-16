"""
信号库连接器 — 把共享信号库的「采集入库」与「查询」暴露为 Jarvis 工具。

- signal_ingest        采集端（每日全行业扫描）把结构化信号写入库
- signal_report        日报视图：最近 N 天信号按类型分组
- signal_for_node      潜客覆盖轨：按赛道/元件取相关余料信号（意向打分）
- signal_opportunities 机会轨：点名具体公司的强余料信号（bonus 线索）
- signal_hot_sectors   余料信号聚集、值得人工审核提前跟进的赛道

存储与算法见 intel/signal_library.py，设计契约见 intel/signal_library_spec.md。
"""
import json

from core.registry import tool
from intel import signal_library as sl

_inited = False


def _ensure() -> None:
    global _inited
    if not _inited:
        sl.init_db()
        _inited = True


@tool(
    "signal_ingest",
    (
        "把一批结构化市场信号写入共享信号库（去重/合并由库负责）。"
        "用于每日全行业信号采集：联网搜索后，把整理好的信号 JSON 数组传进来。"
        "signals_json 必须是 JSON 数组，每个元素含 summary/signal_type/scope 等字段。"
    ),
    {
        "type": "object",
        "properties": {
            "signals_json": {"type": "string", "description": "信号对象的 JSON 数组（字符串）"},
        },
        "required": ["signals_json"],
    },
)
async def signal_ingest(signals_json: str) -> str:
    _ensure()
    try:
        data = json.loads(signals_json)
    except Exception as e:
        return f"signals_json 不是合法 JSON：{e}"
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return "signals_json 必须是 JSON 数组。"
    stats = sl.ingest_signals(data)
    return f"入库完成：新增 {stats['inserted']}，合并 {stats['merged']}，跳过 {stats['skipped']}（共 {len(data)} 条输入）。"


@tool(
    "signal_report",
    "取最近 N 天的市场信号、按信号类型分组，用于生成电子元件市场情报日报。",
    {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "回看天数，默认 14"},
        },
        "required": [],
    },
)
async def signal_report(days: int = 14) -> str:
    _ensure()
    grouped = sl.query_report(days=days)
    if not grouped:
        return f"最近 {days} 天信号库为空。"
    lines = []
    for stype, items in grouped.items():
        lines.append(f"## {stype}（{len(items)}）")
        for it in items[:20]:
            src = it.get("source_url") or ""
            lines.append(f"- [{it.get('severity')}] {it['summary']}  {src}")
    return "\n".join(lines)


@tool(
    "signal_for_node",
    (
        "按潜客赛道 id 和/或元件大类，取相关的「余料」信号，按衰减强度降序。"
        "用于潜客生成的意向打分（覆盖轨）。"
        "sector_ids / components 用英文逗号分隔，如 sector_ids='industrial,ind_vfd_lv'。"
    ),
    {
        "type": "object",
        "properties": {
            "sector_ids": {"type": "string", "description": "赛道 id，逗号分隔"},
            "components": {"type": "string", "description": "元件大类，逗号分隔，可留空"},
        },
        "required": ["sector_ids"],
    },
)
async def signal_for_node(sector_ids: str = "", components: str = "") -> str:
    _ensure()
    secs = [s.strip() for s in sector_ids.split(",") if s.strip()]
    comps = [c.strip() for c in components.split(",") if c.strip()] or None
    rows = sl.query_for_node(secs, comps)
    if not rows:
        return "该赛道/元件暂无相关余料信号。"
    return "\n".join(f"- [强度{r['strength']}|{r['signal_type']}|{r['scope']}] {r['summary']}" for r in rows)


@tool(
    "signal_opportunities",
    (
        "取点名了具体公司、且余料含义强的信号 → 潜客机会轨的 bonus 线索（不占常规配额）。"
        "min_implication 默认 3（仅取明显余料来源）。"
    ),
    {
        "type": "object",
        "properties": {
            "min_implication": {"type": "integer", "description": "余料含义下限 0-5，默认 3"},
        },
        "required": [],
    },
)
async def signal_opportunities(min_implication: int = 3) -> str:
    _ensure()
    rows = sl.company_pointed_signals(min_implication=min_implication)
    if not rows:
        return "暂无点名公司的强余料信号。"
    out = []
    for r in rows:
        site = f" ({r['website']})" if r.get("website") else ""
        out.append(f"- {r.get('company_name')}{site} [{r['signal_type']}|余料{r['surplus_implication']}] {r.get('note') or ''}")
    return "\n".join(out)


@tool(
    "signal_hot_sectors",
    "列出余料信号聚集、值得人工审核提前跟进的赛道（机会轨的行业级触发）。",
    {
        "type": "object",
        "properties": {
            "threshold": {"type": "number", "description": "聚合强度阈值，默认 6.0"},
        },
        "required": [],
    },
)
async def signal_hot_sectors(threshold: float = 6.0) -> str:
    _ensure()
    hot = sl.detect_hot_sectors(threshold=threshold)
    if not hot:
        return f"暂无达到阈值（{threshold}）的热点赛道。"
    return "\n".join(
        f"- {h['sector_id']}：聚合强度 {round(h['total_strength'], 1)}（{len(h['signal_ids'])} 条信号）"
        for h in hot
    )
