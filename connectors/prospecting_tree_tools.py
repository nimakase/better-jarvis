"""
connectors/prospecting_tree_tools.py — 潜客树的「可读、可选」查询/跳过工具
(2026-08-14)

背景：潜客生成走 prospect_daily 这个 detach 工作流，select_node() 在其中自动
选节点、跑完自动推进树——整套逻辑对贾维斯（对话里）完全不透明：现在跑到哪个
节点、接下来排的是什么、想跳过某个不想要的节点，之前一概没有正式入口，每次
都是贾维斯自己现造一个脚本改 data/prospect_tree.json（没留痕迹、没法撤销）。

这几个工具把"读树状态""跳过/撤销跳过节点"收编成正式、幂等的操作：
  - prospect_tree_status    只读，看整体进度 + 接下来会被选中的几个节点
  - prospect_current_node   只读，看现在（可能正在联网生成中）跑的是哪个
  - prospect_skip_node      跳过一个 (类目,区域)（或整个类目剩余区域），可选留原因
  - prospect_unskip_node    撤销跳过

真正的选择/推进逻辑仍在 prospecting/generation.py（select_node/_pick/
_remaining_regions），本文件只是给它包一层对话可调的壳，不重复任何判断逻辑。
"""
from __future__ import annotations

from core import effects
from core.registry import tool


def _resolve_tree_path() -> str:
    from intel.workflow_defs import resolve_prospect_paths
    return str(resolve_prospect_paths()["tree_path"])


@tool(
    "prospect_tree_status",
    "查看潜客树整体进度（已完成/已跳过/待跑的类目×区域组合各有多少）以及"
    "接下来会依次被 select_node() 选中的几个节点——用于在某个节点真正开始跑"
    "之前就先看到它、决定要不要用 prospect_skip_node 跳过。只读，不改任何状态。",
    {"type": "object", "properties": {
        "upcoming": {"type": "integer",
                     "description": "预览接下来几个待跑节点，默认 5"},
    }},
    effect=effects.READ_LOCAL,
)
async def prospect_tree_status(upcoming: int = 5) -> str:
    from prospecting import generation as gen
    st = gen.tree_status(_resolve_tree_path(), upcoming=upcoming)
    lines = [
        f"潜客树进度：共 {st['total_combos']} 个(类目,区域)组合，"
        f"已完成 {st['done_combos']}，已跳过 {st['skipped_combos']}，"
        f"待跑 {st['pending_combos']}。",
    ]
    if st["upcoming"]:
        lines.append("\n接下来会按顺序选中：")
        for i, n in enumerate(st["upcoming"], 1):
            lines.append(f"  {i}. {n['label']}·{n['region_label']}"
                          f"（leaf_id={n['leaf_id']}, region={n['region']}）")
    else:
        lines.append("没有待跑节点了（要么全部完成，要么剩下的都被跳过了）。")
    return "\n".join(lines)


@tool(
    "prospect_current_node",
    "查看现在（可能正在联网生成中）跑的是哪个潜客树节点——回答「现在跑到哪了」"
    "这类问题。只读。如果当前没有正在跑的批次会明确说明，不是报错。",
    {"type": "object", "properties": {}},
    effect=effects.READ_LOCAL,
)
async def prospect_current_node() -> str:
    from intel.workflow_defs import resolve_prospect_paths
    from prospecting import workflows as pw

    cur = pw.load_current_node(resolve_prospect_paths()["current_node_path"])
    if not cur:
        return "现在没有正在跑的潜客生成批次。"
    node = cur.get("node") or {}
    label = node.get("label") or ""
    region_label = node.get("region_label") or node.get("region") or ""
    resumed = "（续跑存盘批）" if cur.get("resumed") else ""
    return (f"现在跑的是：{label}·{region_label}{resumed}，"
            f"选中时间 {cur.get('started_at', '未知')}。"
            "注意：这是「选中/开始时」的快照，不会实时更新生成进度"
            "（生成进度只能看运行日志）。")


@tool(
    "prospect_skip_node",
    "跳过潜客树的某个 (类目,区域) 组合，以后不会再被 select_node() 选中——但不算"
    "「已完成」，跟正常跑完的区域分开记账，随时可以用 prospect_unskip_node 撤销。"
    "region 留空则跳过这个类目【当前所有还没跑完】的区域。用 prospect_tree_status "
    "先看一眼 leaf_id/region 再调用。",
    {"type": "object", "properties": {
        "leaf_id": {"type": "string", "description": "类目 id，如 ia_robotics（见 prospect_tree_status）"},
        "region": {"type": "string", "description": "区域代号，如 NA/EU/SEA/EA/SA；留空=跳过该类目全部剩余区域"},
        "reason": {"type": "string", "description": "可选，跳过的理由，纯记录用，不影响任何判断"},
    }, "required": ["leaf_id"]},
    effect=effects.WRITE_LOCAL,
)
async def prospect_skip_node(leaf_id: str, region: str = "", reason: str = "") -> str:
    from prospecting import generation as gen
    ok = gen.skip_node(_resolve_tree_path(), leaf_id, region or None, reason)
    if not ok:
        return f"没找到 leaf_id={leaf_id!r} 这个类目，先用 prospect_tree_status 确认拼写。"
    target = f"区域 {region}" if region else "全部剩余区域"
    return f"已跳过 {leaf_id} 的{target}" + (f"（理由：{reason}）" if reason else "") + "。"


@tool(
    "prospect_unskip_node",
    "撤销对某个 (类目,区域) 的跳过，重新回到待跑候选池。region 留空则撤销这个"
    "类目全部已跳过的区域。",
    {"type": "object", "properties": {
        "leaf_id": {"type": "string", "description": "类目 id"},
        "region": {"type": "string", "description": "区域代号；留空=撤销该类目全部已跳过区域"},
    }, "required": ["leaf_id"]},
    effect=effects.WRITE_LOCAL,
)
async def prospect_unskip_node(leaf_id: str, region: str = "") -> str:
    from prospecting import generation as gen
    ok = gen.unskip_node(_resolve_tree_path(), leaf_id, region or None)
    if not ok:
        return f"没找到 leaf_id={leaf_id!r} 这个类目，先用 prospect_tree_status 确认拼写。"
    target = f"区域 {region}" if region else "全部已跳过区域"
    return f"已撤销 {leaf_id} 的{target}的跳过，重新回到待跑候选池。"


@tool(
    "prospect_reset_node",
    "清空某个 (类目,区域) 的完成/跳过记录，让它重新变成【待跑】——用于「这个节点"
    "画像质量不行，想让它重新跑一遍」这类场景。region 留空=清空这个类目全部区域的"
    "记录（done_regions 和 skipped_regions 都清）。force_next=true 时会额外把它"
    "设成下一次 select_node() 必定优先选中的目标（一次性，跑完/被跳过后自动清除），"
    "而不是仅仅清记录后排回队列原来的位置等它自然轮到。"
    "⚠️ 这只清树里的进度记账，不会撤销这个节点之前已经生成过的候选公司数据"
    "（那些还在原来的批次文件/表格里）——重跑只会新增一批，不会自动去重或覆盖旧的。",
    {"type": "object", "properties": {
        "leaf_id": {"type": "string", "description": "类目 id（见 prospect_tree_status）"},
        "region": {"type": "string", "description": "区域代号，如 NA/EU/SEA/EA/SA；留空=清空该类目全部区域"},
        "force_next": {"type": "boolean",
                       "description": "true=清空后立刻设为下次必跑目标（默认 true）；"
                                      "false=只清记录，按正常优先级排队等轮到"},
    }, "required": ["leaf_id"]},
    effect=effects.WRITE_LOCAL,
)
async def prospect_reset_node(leaf_id: str, region: str = "", force_next: bool = True) -> str:
    from prospecting import generation as gen
    ok = gen.reset_node(_resolve_tree_path(), leaf_id, region or None, force_next=force_next)
    if not ok:
        return f"没找到 leaf_id={leaf_id!r} 这个类目，先用 prospect_tree_status 确认拼写。"
    target = f"区域 {region}" if region else "全部区域"
    tail = "，并已设为下一次必跑目标。" if force_next else "，重新排回待跑候选池（不保证下一次就跑到它）。"
    return f"已清空 {leaf_id} 的{target}的完成/跳过记录{tail}"


@tool(
    "prospect_clear_forced_next",
    "取消当前设置的「下一次必跑目标」（forced_next），恢复正常的优先级选择逻辑。"
    "一般用不到——目标跑完/被跳过后会自动清除——只在改主意不想强制它优先跑时手动调用。",
    {"type": "object", "properties": {}},
    effect=effects.WRITE_LOCAL,
)
async def prospect_clear_forced_next() -> str:
    from prospecting import generation as gen
    had = gen.clear_forced_next(_resolve_tree_path())
    return "已取消强制下一跑目标，恢复正常优先级选择。" if had else "当前本来就没有设置强制下一跑目标。"
