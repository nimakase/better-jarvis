"""
connectors/artifact_tools.py — 产物图书馆工具（core/artifacts 的对话入口）

四个工具：
  - library_overview   数据足迹总览（「你攒了我多少东西」「图书馆里有什么」）
  - file_to_library    把一个已生成的文件登记进图书馆（给它身份和人向视图）
  - keep_tool          试用中的自建工具转正（trial → active）
  - purge_artifacts    清空某 producer 名下产物（不可逆 → 自动被确认闸拦一道）
"""
from functools import partial

from core import artifacts
from core import effects
from core.registry import tool as _tool

tool = partial(_tool, group="library")


def _fmt_size(n: int) -> str:
    if n >= 1e9:
        return f"{n/1e9:.1f} GB"
    if n >= 1e6:
        return f"{n/1e6:.1f} MB"
    return f"{n/1e3:.0f} KB"


@tool(
    "library_overview",
    "产物图书馆总览：按来源(producer)汇总登记过的所有产物（报告/清单/文档等）的数量与"
    "占用大小，并列出最近的产物。用户问「你都存了我什么」「图书馆里有什么」「产出都在哪」"
    "时用。只读。",
    {"type": "object", "properties": {}},
    effect=effects.READ_LOCAL,
)
async def library_overview() -> str:
    fp = artifacts.footprint()
    recent = artifacts.list_artifacts(limit=10)
    if not fp:
        return f"图书馆还是空的（{artifacts.LIBRARY_DIR}）。生成的报告/清单会自动登记进来。"
    lines = [f"📚 产物图书馆（本机路径：{artifacts.LIBRARY_DIR}）", "", "按来源汇总："]
    for producer, s in fp.items():
        trial = f"，其中试用 {s['trials']}" if s.get("trials") else ""
        lines.append(f"  {producer}：{s['count']} 个 · {_fmt_size(s['bytes'])}{trial}")
    lines.append("")
    lines.append("最近产物：")
    for a in recent:
        lines.append(f"  [{a['state']}] {a['label']}（{a['kind']} · {a['created_at'][:10]} · {a['producer']}）")
    return "\n".join(lines)


@tool(
    "file_to_library",
    "把一个已生成的文件登记进产物图书馆：给它身份（谁生的、什么类型、什么标签），"
    "并在本机图书馆目录里建一个人找得到的视图。生成了用户要保留的文件（清单/导出/文档）"
    "后调用。kind 可选：报告/清单/文档/情报/其他。producer 格式如 skill:xxx 或 manual。",
    {
        "type": "object",
        "properties": {
            "path":     {"type": "string", "description": "文件的绝对路径"},
            "kind":     {"type": "string", "description": "类型：报告/清单/文档/情报/其他"},
            "label":    {"type": "string", "description": "人看得懂的标签，如 OEM筛查_137家"},
            "producer": {"type": "string", "description": "来源，如 skill:oem_ems_screener / manual"},
            "trial":    {"type": "boolean", "description": "是否试用产物（短保质期，默认 false）"},
        },
        "required": ["path", "label"],
    },
    effect=effects.WRITE_LOCAL,
)
async def file_to_library(path: str, label: str, kind: str = "其他",
                          producer: str = "manual", trial: bool = False) -> str:
    from pathlib import Path
    if not Path(path).exists():
        return f"文件不存在：{path}"
    rec = artifacts.register(path, producer=producer, kind=kind, label=label,
                             state="trial" if trial else "kept")
    where = rec.get("library_path") or "（图书馆视图创建失败，仅登记）"
    ttl = f"（试用产物，{rec['ttl_days']} 天后未保留将标过期）" if trial else ""
    return f"已登记进图书馆：{where} {ttl}"


@tool(
    "keep_tool",
    "把【试用中】的自建工具转正保留（trial → active）。用户对新工具说「留下 / 就它了 / "
    "转正 / 好用」时调用。转正后该工具与其产物长期保留。",
    {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "工具名"}},
        "required": ["name"],
    },
    effect=effects.WRITE_LOCAL,
)
async def keep_tool(name: str) -> str:
    from core.tool_builder import keep_skill
    ok, msg = keep_skill(name)
    return msg if ok else f"转正失败：{msg}"


@tool(
    "purge_artifacts",
    "【不可逆】删除某个来源(producer)名下的全部产物：登记、图书馆视图、原文件一并删除。"
    "通常在用户放弃某个试用工具并确认「产物也删掉」后调用，producer 形如 skill:工具名。"
    "调用前必须先向用户复述将删除的数量并获确认。",
    {
        "type": "object",
        "properties": {"producer": {"type": "string", "description": "来源，如 skill:oem_ems_screener"}},
        "required": ["producer"],
    },
    effect=effects.IRREVERSIBLE,
)
async def purge_artifacts(producer: str) -> str:
    res = artifacts.purge_producer(producer, delete_files=True)
    n = len(res["items"])
    if n == 0:
        return f"{producer} 名下没有登记的产物。"
    return f"已删除 {producer} 名下 {n} 个产物（文件 + 图书馆视图 + 登记）。"
