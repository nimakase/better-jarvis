"""
按【符号】读取第一方源码 —— token 经济的"参考读取"基石。

`read_symbol(module, name)` 只返回某个类 / 方法 / 函数 / 模块级常量的源码片段，
而不是整个文件——供"造工具时参考现有代码"用，避免把上千行整文件塞爆上下文。
两方案（两趟参考注入 / 完整 agentic 造工具子 agent）共用它。

只读；带与 `connectors/self_inspect` 一致的安全围栏：只能读仓库内 .py，密钥/运行态
（.env、.venv、.git、__pycache__、data）一律拒读。
"""

import ast
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
_DENY_PARTS = {".env", ".venv", ".git", "__pycache__", "node_modules", "data"}
_ALLOWED_EXT = {".py"}


def _module_to_path(module: str) -> Optional[Path]:
    """把 'prospecting.hubspot_worker' 或 'prospecting/hubspot_worker.py' 归一到仓库内 .py 路径。"""
    m = (module or "").strip().strip('"').strip("'")
    if not m:
        return None
    if m.endswith(".py"):
        m = m[:-3]
    rel = m.replace(".", "/") + ".py"
    try:
        p = (REPO_ROOT / rel).resolve()
        parts = p.relative_to(REPO_ROOT).parts
    except ValueError:
        return None   # 越界
    if any(part in _DENY_PARTS for part in parts):
        return None
    if p.suffix.lower() not in _ALLOWED_EXT:
        return None
    return p if p.exists() else None


def _find_node(tree: ast.Module, name: str):
    """支持 name = 'Class' | 'Class.method' | 'func' | 'MODULE_CONST'。"""
    if "." in name:
        cls_name, attr = name.split(".", 1)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == cls_name:
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == attr:
                        return m
        return None
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and getattr(node, "name", None) == name:
            return node
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return node
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            return node
    return None


def read_symbol(module: str, name: str, max_lines: int = 200) -> str:
    """返回 module 里名为 name 的类/方法/函数/常量的源码片段（含少量头注）。
    找不到/越权/超长都给出明确文本，绝不抛异常。"""
    p = _module_to_path(module)
    if p is None:
        return f"[read_symbol] 找不到或不允许读取模块：{module!r}（只能读仓库内 .py，排除密钥/运行态）"
    try:
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception as e:
        return f"[read_symbol] 解析失败：{module}（{e}）"
    node = _find_node(tree, name)
    if node is None:
        return f"[read_symbol] 模块 {module} 里找不到符号：{name!r}"
    seg = ast.get_source_segment(src, node)
    if not seg:
        return f"[read_symbol] 无法取得 {module}:{name} 的源码"
    lines = seg.splitlines()
    trunc = ""
    if len(lines) > max_lines:
        trunc = f"\n… [截断：仅前 {max_lines} 行，符号共 {len(lines)} 行；如需更多请分符号读]"
        lines = lines[:max_lines]
    return f"# ── {module}:{name} ──\n" + "\n".join(lines) + trunc
