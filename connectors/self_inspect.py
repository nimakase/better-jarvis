"""
connectors/self_inspect.py — 只读自省工具（阶段 1）

让贾维斯「读得到自己」：列出自己的模块地图、按边界（core/self_model）读取源码。
**纯只读**——不写、不改、不执行。修改自己的能力属后续阶段，且 PROTECTED 永不自动改。

两个工具：
- list_self_modules：列出全部源码模块 + 所属区（🔒核心 / 🟢周边）+ 职责 + 行数。
- read_self_source：读取指定文件源码，并标注它属于核心还是周边、为什么。

安全围栏：只能读仓库内的源码/文档/配置文本；密钥与运行态（.env、data/、.git、
.venv、__pycache__）一律拒读。
"""

from functools import partial
from pathlib import Path

from core import self_model
from core.registry import tool as _tool
from core.safety import under_base

tool = partial(_tool, group="self")

REPO_ROOT = self_model.REPO_ROOT

# 扫描这些位置的源码（构成「自我地图」）
_SCAN_DIRS = ["core", "connectors", "web", "intel", "prospecting", "skills"]
_SCAN_ROOT_FILES = ["main.py", "config.py"]

# read_self_source 允许的扩展名（源码/文档/配置文本）
_ALLOWED_EXT = {
    ".py", ".md", ".txt", ".toml", ".cfg", ".ini",
    ".json", ".yaml", ".yml", ".sh", ".bat",
    ".html", ".js", ".css",
}
# 这些路径片段一律拒读（密钥 / 运行态 / 版本库 / 缓存）
_DENY_PARTS = {".env", ".venv", ".git", "__pycache__", "node_modules", "data"}

# 单文件读取行数上限（超出截断，避免撑爆上下文）
_MAX_LINES = 2000


def _loc(p: Path) -> int:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _iter_source_files():
    for rf in _SCAN_ROOT_FILES:
        p = REPO_ROOT / rf
        if p.exists():
            yield p
    for d in _SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            yield p


@tool(
    "list_self_modules",
    "列出贾维斯自己的全部源码模块地图：每个模块属于【核心(受保护·不自动迭代)】还是"
    "【周边(可自我迭代)】、它的职责、代码行数。用于自我认知——在优化前先看清自己有哪些"
    "部件、哪些能动哪些不能动。只读，无参数。",
    {"type": "object", "properties": {}},
)
async def list_self_modules() -> str:
    rows = []
    for p in _iter_source_files():
        rel = p.relative_to(REPO_ROOT).as_posix()
        zone, reason = self_model.classify(rel)
        rows.append((zone, rel, _loc(p), reason))
    prot = [r for r in rows if r[0] == "protected"]
    opn = [r for r in rows if r[0] == "open"]

    def fmt(group):
        if not group:
            return "  （无）"
        return "\n".join(
            f"  {rel}  ·  {loc} 行  ·  {reason}"
            for _, rel, loc, reason in group
        )

    total_loc = sum(r[2] for r in rows)
    return (
        f"贾维斯自我模块地图（共 {len(rows)} 个源文件 · {total_loc} 行）\n"
        f"边界事实源：core/self_model.py（读用 read_self_source）\n\n"
        f"🔒 核心 PROTECTED（{len(prot)} 个 · 不自动迭代，要改须人工 + 影响 + 动机）\n"
        f"{fmt(prot)}\n\n"
        f"🟢 周边 OPEN（{len(opn)} 个 · 可自我迭代，测试绿才自动生效 + 可回滚）\n"
        f"{fmt(opn)}"
    )


@tool(
    "read_self_source",
    "读取贾维斯自己某个源文件的完整源码，并在开头标注它属于核心还是周边、为什么。"
    "path 用相对仓库根的路径，如 'core/controller.py' 或 'ARCHITECTURE.md'。"
    "用于在理解/优化前真正读懂某段代码。只读；不能读仓库外或密钥/运行态文件"
    "（.env、data/、.git 等会被拒）。",
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对仓库根的文件路径，如 core/registry.py 或 ARCHITECTURE.md",
            },
        },
        "required": ["path"],
    },
)
async def read_self_source(path: str) -> str:
    raw = (path or "").strip().strip('"').strip("'").replace("\\", "/")
    if not raw:
        return "需要提供文件路径（相对仓库根），如 core/controller.py。"

    # 围栏 1：限定在仓库内（防穿越 / 绝对路径）
    try:
        target = under_base(REPO_ROOT, raw)
    except ValueError:
        return f"拒绝：路径越界，只能读取仓库内文件。你给的是 {raw!r}。"

    rel = target.relative_to(REPO_ROOT).as_posix()
    parts = target.relative_to(REPO_ROOT).parts

    # 围栏 2：密钥 / 运行态 / 版本库一律拒读
    if any(part in _DENY_PARTS for part in parts) or target.name.startswith(".env"):
        return f"拒绝：{rel} 属于密钥/运行态/版本库（不可读）。只能读源码、文档与配置文本。"

    # 围栏 3：扩展名白名单
    if target.suffix.lower() not in _ALLOWED_EXT:
        return (
            f"拒绝：不支持读取 {target.suffix or '（无扩展名）'} 文件（{rel}）。"
            f"只允许源码/文档/配置文本：{', '.join(sorted(_ALLOWED_EXT))}。"
        )

    if not target.exists() or not target.is_file():
        return f"文件不存在：{rel}。可先用 list_self_modules 查看有哪些模块。"

    zone, reason = self_model.classify(rel)
    label = "🔒 核心 PROTECTED（不自动迭代；要改须人工审核 + 影响 + 动机）" \
        if zone == "protected" else \
        "🟢 周边 OPEN（可自我迭代；测试绿才自动生效 + 可回滚）"

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"读取失败：{rel}（{e}）"

    lines = text.splitlines()
    truncated = ""
    if len(lines) > _MAX_LINES:
        lines = lines[:_MAX_LINES]
        truncated = f"\n\n[已截断：仅显示前 {_MAX_LINES} 行，全文 {len(text.splitlines())} 行]"
        text = "\n".join(lines)

    return (
        f"文件：{rel}\n"
        f"归属：{label}\n"
        f"原因：{reason}\n"
        f"{'─' * 48}\n"
        f"{text}{truncated}"
    )
