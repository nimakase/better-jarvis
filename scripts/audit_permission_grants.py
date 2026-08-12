#!/usr/bin/env python3
"""scripts/audit_permission_grants.py — 权限授权全量审查报告
(core/grants.py + core/permission_scan.py，2026-08-12 · 步骤⑤一期)

跑一遍完整的工具装配(跟 main.py 一样的顺序，但不起 FastAPI/scheduler)，然后对
【当前注册表里的每一个工具】——不分 connectors/prospecting/tool_builder 元工具/
skills，origin 完全不参与判断——按源文件分组，打印它实际用到的权限点、是否已被
Ned 批准过。这是"甩掉 origin 决定信任"这件事的起点：把现状摊开来看，再决定批不批。

用法(真机跑，需要真实 venv 有 config 依赖)：
    .venv/bin/python scripts/audit_permission_grants.py           # 只看未覆盖的
    .venv/bin/python scripts/audit_permission_grants.py --all     # 全部都看

看完想批准某一条，回到对话里跟贾维斯说"批准 xxx 模块的权限"，它会调
approve_permission_grant 工具落一条记录——批准动作走对话，不走这个脚本
（这个脚本只读，不改任何状态），是刻意的：批准是需要 Ned 明确表态的动作，
不该藏在一个批量脚本参数里顺手就点了。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402, F401 — 触发 Settings 装配
from core import registry  # noqa: E402
from core import tool_builder  # noqa: E402 — import 即自注册元工具
from core import permission_scan  # noqa: E402, F401

registry.discover_connectors()      # 装配全部 connectors/（含 prospecting 暴露的工具）
tool_builder.load_all_active_skills()  # 装配全部已激活的 skills/

from core import grants  # noqa: E402 — 装配完成后再引，audit_registry 依赖完整注册表


def main() -> int:
    only_uncovered = "--all" not in sys.argv[1:]
    entries = grants.audit_registry()
    if only_uncovered:
        entries = [e for e in entries if not e["covered"]]

    if not entries:
        print("没有需要审查的项——所有已注册工具的权限footprint都已被覆盖。")
        return 0

    print(f"共 {len(entries)} 个模块" + ("（未覆盖）" if only_uncovered else "") + "：\n")
    for e in entries:
        mark = "✅ 已覆盖" if e["covered"] else f"⚠️  {e['reason']}"
        print(f"【{e['module']}】{mark}")
        print(f"  工具：{', '.join(e['tools'])}")
        if e.get("source_path"):
            print(f"  文件：{e['source_path']}")
        if e["detected_permissions"]:
            print("  当前用到的权限点：")
            for tag in e["detected_permissions"]:
                new_mark = " 🆕" if tag in e.get("newly_added", []) else ""
                print(f"    - {permission_scan.describe(tag)}{new_mark}")
        else:
            print("  当前用到的权限点：（无——只用了安全子集内的 import）")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
