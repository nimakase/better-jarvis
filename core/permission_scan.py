"""
core/permission_scan.py — 静态扫描一段代码/一个模块实际触碰了哪些【敏感权限点】

背景（2026-08-12 · 步骤⑤一期）：以前"这段代码住在 skills/ 还是 connectors/"这个
目录位置直接决定信不信——skills/ 过 skill_policy 的全局白名单静态校验，
connectors/prospecting 完全不过、住那儿就是通行证。但目录不该决定权限，这个工具
具体用到了什么才该决定。本模块把"用到了什么"变成一组可计算、可审计的【权限标签】；
core/grants.py 负责把这组标签变成"Ned 审过、批准过"的记录，替代 origin 成为信任的
唯一依据（起点：core/registry.ToolSpec.origin 只留作生命周期元数据——谁能替换谁的
注册——不再是安全判定本身；真正的判定见 grants.is_covered）。

注：模块名刻意不叫 capabilities.py——项目里已有 core/capability.py（工具搜索/查重）
和 core/capability_watch.py（模型自身能力如视觉的漂移监测），意思完全不同，撞名
会互相混淆。这里统一用"权限(permission)"这个词。

权限标签的两种形态：
  - "import:<module>"       —— 用到了 skill_policy.ALLOWED_IMPORTS 安全子集之外的
                                某个模块（core/connectors/config、prospecting 里
                                未登记为 building block 的部分、subprocess 等系统
                                调用……全部落在这一类，标签就是被 import 的完整
                                点分路径）。
  - 已登记的 BUILDING_BLOCKS key（如 "prospecting.hubspot_worker"）—— 复用
    skill_policy 现成的、经人工整理过 API 语义的白名单模块，标签就是该模块路径本身
    （比 "import:prospecting.hubspot_worker.xxx" 更粗一档，跟 skill_policy 原有的
    building-block 概念对齐，复用它已经写好的人工审计信息）。

自由集（不算权限、无需授权）：skill_policy.ALLOWED_IMPORTS 里的标准库安全子集 +
已装第三方包——这条线沿用 skill_policy 已经维护好的判断，不重复定义一份。
"""
from __future__ import annotations

import ast
from pathlib import Path

from core import skill_policy

_FREE_IMPORTS = set(skill_policy.ALLOWED_IMPORTS)


def _top_module(name: str) -> str:
    return name.split(".", 1)[0]


def _tag_for(module_name: str) -> "str | None":
    if not module_name:
        return None
    # 已登记的 building block：标签用登记的 key（哪怕 import 的是它的子路径）
    for bb in skill_policy.BUILDING_BLOCKS:
        if module_name == bb or module_name.startswith(bb + "."):
            return bb
    if module_name in _FREE_IMPORTS or _top_module(module_name) in _FREE_IMPORTS:
        return None
    return f"import:{module_name}"


def scan_code(code: str) -> set[str]:
    """扫一段源码，返回它触碰到的权限标签集合。语法错误返回空集（另有报错通道，
    调用方应先做语法校验，这里不重复报错）。"""
    try:
        tree = ast.parse(code)
    except Exception:
        return set()

    tags: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                tag = _tag_for(alias.name)
                if tag:
                    tags.add(tag)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # node.level > 0 且 node.module 为 None 是纯相对导入（`from . import x`），
            # 本仓库风格统一用绝对导入，未覆盖这种写法；真出现会被静默放过，
            # 已知限制，不是本函数的判定 bug。
            tag = _tag_for(node.module)
            if tag:
                tags.add(tag)

    return tags


def scan_file(path: "str | Path") -> set[str]:
    """同 scan_code，但从文件读。读取失败返回空集。"""
    try:
        return scan_code(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return set()


def describe(tag: str) -> str:
    """把权限标签渲染成人话，供审批时展示给 Ned。"""
    if tag in skill_policy.BUILDING_BLOCKS:
        return f"{tag} —— {skill_policy.BUILDING_BLOCKS[tag].get('desc', '')}"
    if tag.startswith("import:"):
        mod = tag[len("import:"):]
        return f"import {mod}（不在安全子集内，需具体审是否合理）"
    return tag
