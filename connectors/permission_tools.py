"""
connectors/permission_tools.py — 权限授权的审查/批准触发工具
(core/permission_scan.py + core/grants.py，2026-08-12 · 步骤⑤一期)

背景见 core/grants.py 顶部文档。这三个工具让 Ned 能在对话里走完"看现状→批准/驳回"
这套流程，跟现有 code_review（tool_builder 造技能时的人工审）是同一种模式：模型
不能替 Ned 做安全判断，只能把信息摆清楚、等 Ned 明确说"批准"才落一笔记录。

本轮（一期）只建立审批记录本身，【不接任何拦截闸】——批准与否目前不影响任何工具
能不能跑,纯审计/记录性质。真正"没批准就不让跑"是二期的事,要等 Ned 走完至少一轮
全量审查、确认这套机制好用之后再接。
"""
from functools import partial

from core import effects
from core.registry import tool as _tool

tool = partial(_tool, group="self")


def _fmt_entry(e: dict) -> str:
    from core import permission_scan
    lines = [f"【{e['module']}】{'✅ 已覆盖' if e['covered'] else '⚠️ ' + e['reason']}"]
    lines.append(f"  工具：{', '.join(e['tools'])}")
    if e.get("source_path"):
        lines.append(f"  文件：{e['source_path']}")
    if e["detected_permissions"]:
        lines.append("  当前用到的权限点：")
        for tag in e["detected_permissions"]:
            mark = " 🆕" if tag in e.get("newly_added", []) else ""
            lines.append(f"    - {permission_scan.describe(tag)}{mark}")
    else:
        lines.append("  当前用到的权限点：（无——只用了安全子集内的 import）")
    return "\n".join(lines)


@tool(
    "audit_permission_grants",
    "审查全部已注册工具(不分第一方connector/prospecting/自建skill)的【权限footprint】"
    "——每个工具实际 import 了哪些超出安全子集的模块/building block，以及是否已被"
    "Ned 批准过、批准后代码有没有变过。只读，不改任何状态，用于「把这些权限摆给"
    "Ned 看，等他决定批不批准」这个流程的第一步。可传 only_uncovered 只看还没被"
    "覆盖(未批准或代码已变)的那些，避免刷屏。",
    {"type": "object", "properties": {
        "only_uncovered": {"type": "boolean",
                           "description": "true=只列未覆盖的(默认)，false=全列"},
    }},
    effect=effects.READ_LOCAL,
)
async def audit_permission_grants(only_uncovered: bool = True) -> str:
    from core import grants
    entries = grants.audit_registry()
    if only_uncovered:
        entries = [e for e in entries if not e["covered"]]
    if not entries:
        return "没有需要审查的项——所有已注册工具的权限footprint都已被覆盖。"
    header = f"共 {len(entries)} 个模块" + ("（未覆盖）" if only_uncovered else "") + "：\n"
    return header + "\n\n".join(_fmt_entry(e) for e in entries)


@tool(
    "approve_permission_grant",
    "Ned 明确批准某个模块当前这版代码可以用到的权限点后调用，把这份批准落一条记录"
    "(module+权限点集合+代码hash+批准人+批准时间)。只应在 Ned 已经看过 "
    "audit_permission_grants 的结果、明确表示批准之后调用——不能替他做这个判断。",
    {"type": "object", "properties": {
        "module": {"type": "string", "description": "要批准的模块 dotted path，如 connectors.xxx"},
        "note": {"type": "string", "description": "可选备注，如批准的理由/范围限制"},
    }, "required": ["module"]},
    effect=effects.WRITE_LOCAL,
)
async def approve_permission_grant(module: str, note: str = "") -> str:
    import inspect
    from pathlib import Path
    from core import permission_scan, grants, registry

    # 不经 sys.modules/__import__ 按名找模块——skills/ 动态加载的模块根本不在
    # sys.modules 里（见 core/grants.audit_registry 的同一处注释）。改成从当前
    # 注册表里找一个属于这个模块的工具，直接对它的 handler 函数对象取源文件。
    handler = next((s.handler for s in registry.iter_specs()
                    if getattr(s.handler, "__module__", None) == module), None)
    if handler is None:
        return f"批准失败：注册表里没有任何工具属于模块 {module}（先跑 audit_permission_grants 确认模块名）"
    path = inspect.getsourcefile(handler)
    if not path:
        return f"批准失败：拿不到 {module} 的源文件路径"
    code = Path(path).read_text(encoding="utf-8")
    detected = permission_scan.scan_code(code)
    r = grants.grant(module, detected, code, approved_by="Ned", source_path=path, note=note)
    return f"已记录批准：{module} 可用权限点 {r['permissions'] or '（无，仅安全子集内 import）'}"


@tool(
    "revoke_permission_grant",
    "撤销某个模块已有的权限授权记录(变回未审批状态)。用户说「撤销/收回xxx的授权」时调用。",
    {"type": "object", "properties": {
        "module": {"type": "string", "description": "要撤销的模块 dotted path"},
    }, "required": ["module"]},
    effect=effects.WRITE_LOCAL,
)
async def revoke_permission_grant(module: str) -> str:
    from core import grants
    r = grants.revoke(module)
    return f"已撤销 {module} 的授权记录" if r["ok"] else f"{module} 本来就没有授权记录"


@tool(
    "list_permission_grants",
    "列出全部已批准的权限授权记录(module/权限点/批准时间/备注)。只读。",
    {"type": "object", "properties": {}},
    effect=effects.READ_LOCAL,
)
async def list_permission_grants() -> str:
    from core import grants
    gs = grants.list_grants()
    if not gs:
        return "还没有任何权限授权记录。"
    lines = [f"共 {len(gs)} 条授权记录："]
    for g in gs:
        lines.append(f"\n【{g['module']}】批准人：{g['approved_by']}，时间：{g['approved_at'][:19]}")
        lines.append(f"  权限点：{', '.join(g['permissions']) or '（无）'}")
        if g.get("note"):
            lines.append(f"  备注：{g['note']}")
    return "\n".join(lines)
