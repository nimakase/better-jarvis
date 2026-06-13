"""
工具自建系统

流程：
  1. 用户描述需求 → Jarvis 调 create_tool
  2. tool_builder 调 API 生成代码，保存到 skills/<name>/tool.py（draft 状态）
  3. 前端收到 code_review 消息，展示代码 + 「激活」按钮
  4. 用户点激活 → POST /api/tools/<name>/activate
  5. 动态加载注册，立即可用；下次启动自动加载

技能文件结构：
  skills/
    stock_price/
      tool.py     ← 生成的代码（含 async 函数 + TOOL_DEF）
      meta.json   ← {"status": "draft"|"active", "description": "...", "created_at": "..."}
"""

import ast
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from openai import AsyncOpenAI

import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from core.controller import register_tool

# ── 静态代码验证 ──────────────────────────────────────────────────────────────

# 允许导入的模块白名单（标准库安全子集 + 已安装第三方包）
ALLOWED_IMPORTS = {
    "json", "datetime", "pathlib", "typing", "math", "random", "time",
    "hashlib", "base64", "csv", "io", "copy", "re", "collections",
    "itertools", "functools", "dataclasses", "enum", "abc", "string",
    "urllib.parse", "html", "decimal", "fractions", "statistics",
    "httpx", "openai", "requests",
    "pdfplumber", "docx", "openpyxl", "pptx",
}

# 禁止导入（阻断级别）
BLOCKED_IMPORTS = {
    "core", "connectors", "main", "config",   # 内部模块
    "subprocess", "multiprocessing", "signal", # 进程/系统控制
    "socket", "ssl", "asynchat", "asyncore",   # 低级网络
    "ctypes", "cffi", "mmap",                   # 原生代码
    "importlib", "pkgutil",                     # 动态导入
    "pickle", "shelve", "marshal",              # 不安全序列化
    "pty", "tty", "termios", "fcntl",           # 终端控制
}

# 可疑模式（警告级别，不阻断）
WARNING_PATTERNS = [
    ("os.system",    "可执行系统命令"),
    ("os.popen",     "可执行系统命令"),
    ("os.remove",    "可删除文件"),
    ("os.rmdir",     "可删除目录"),
    ("shutil.rmtree","可递归删除目录"),
    ("sys.path",     "可修改模块搜索路径"),
    ("sys.modules",  "可修改已加载模块"),
    ("__import__",   "动态导入，可绕过白名单"),
    ("open(",        "直接读写文件系统"),
]


def validate_tool_code(code: str) -> dict:
    """
    静态分析工具代码，返回：
    {
        "ok": bool,          # False = 有阻断级错误，不应激活
        "errors": [str],     # 阻断级问题
        "warnings": [str],   # 警告级问题
    }
    """
    errors = []
    warnings = []

    # 1. 语法检查
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"ok": False, "errors": [f"语法错误：{e}"], "warnings": []}

    # 2. Import 检查（AST）
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                names = [node.module] if node.module else []

            for name in names:
                root = name.split(".")[0]
                if root in BLOCKED_IMPORTS:
                    errors.append(f"禁止导入内部或危险模块：`{name}`")
                elif root == "os":
                    warnings.append("导入了 `os` 模块，注意避免调用系统命令或删除文件")
                elif root == "sys":
                    warnings.append("导入了 `sys` 模块，注意避免修改 sys.path / sys.modules")
                elif root not in ALLOWED_IMPORTS and root not in {"os", "sys"}:
                    warnings.append(f"导入了未在白名单内的模块：`{name}`，请确认其安全性")

    # 3. 可疑模式检查（文本层面，捕获动态拼接等 AST 无法完全覆盖的情况）
    for pattern, reason in WARNING_PATTERNS:
        if pattern in code:
            warnings.append(f"检测到 `{pattern}`：{reason}")

    # 4. 必要结构检查
    has_tool_def = "TOOL_DEF" in code
    if not has_tool_def:
        errors.append("缺少 `TOOL_DEF` 字典，工具无法被识别")

    has_async_func = any(
        isinstance(node, ast.AsyncFunctionDef)
        for node in ast.walk(tree)
    )
    if not has_async_func:
        errors.append("缺少 async 函数定义")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }

SKILLS_DIR = config.SKILLS_DIR
SKILLS_DIR.mkdir(exist_ok=True)

# ── 代码生成 ──────────────────────────────────────────────────────────────────

CODE_GEN_PROMPT = '''你是一个 Python 工具开发助手。根据用户需求，生成一个可直接运行的 Python 工具文件。

【严格遵守的格式要求】
1. 文件顶部是注释（描述工具用途）
2. 只 import 标准库或以下已安装的库：httpx, openai, pdfplumber, python-docx, openpyxl, python-pptx, requests, pathlib, json, datetime, re, csv, os, sys
3. 禁止 import：core, connectors, main, config（内部模块），subprocess, multiprocessing, importlib, pickle, ctypes
4. 有且只有一个主 async 函数，函数名 = 工具名（snake_case）
5. 文件末尾必须有 TOOL_DEF 字典，格式如下：
   TOOL_DEF = {
       "name": "工具名",
       "description": "何时使用这个工具的描述（路由靠这个判断）",
       "input_schema": {
           "type": "object",
           "properties": {
               "参数名": {"type": "string", "description": "参数说明"}
           },
           "required": ["必填参数名"]
       }
   }
6. 工具函数必须返回 str（结果或错误信息）
7. 用 try/except 包住主逻辑，出错返回错误描述而不是抛异常
8. 只输出 Python 代码，不要任何额外说明，不要 markdown 代码块标记

【跨平台规则（Windows + macOS 必须同时兼容）】
- 所有文件路径使用 pathlib.Path，禁止硬编码 /Users/xxx 或 C:/Users/xxx
- 用户主目录用 Path.home()，不要写死
- 路径拼接用 Path / 运算符，不要用字符串加 / 或 \\
- 需要存储文件时，存在 Path.home() / "jarvis_data" 下，不要存在项目目录里
- os.path 相关操作优先改用 pathlib.Path 等价方法

【示例输出】
"""
Tool: weather
查询城市当前天气
"""
import httpx

async def weather(city: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://wttr.in/{city}?format=3")
        return resp.text
    except Exception as e:
        return f"查询天气失败：{e}"

TOOL_DEF = {
    "name": "weather",
    "description": "查询指定城市的当前天气。用户询问天气时使用。",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名，支持中英文"}
        },
        "required": ["city"]
    }
}
'''


async def _generate_code(tool_name: str, user_request: str) -> str:
    client = AsyncOpenAI(api_key=config.OPENROUTER_API_KEY, base_url=config.OPENROUTER_BASE_URL)
    resp = await client.chat.completions.create(
        model=config.CLAUDE_MODEL,
        max_tokens=8192,
        messages=[
            {"role": "system", "content": CODE_GEN_PROMPT},
            {"role": "user",   "content": f"工具名：{tool_name}\n需求：{user_request}"}
        ]
    )
    code = resp.choices[0].message.content.strip()
    # 去掉 AI 可能包的 markdown 代码块
    if code.startswith("```"):
        lines = code.split("\n")
        code = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return code


# ── 保存 & 加载 ───────────────────────────────────────────────────────────────

def _skill_dir(name: str) -> Path:
    return SKILLS_DIR / name


def save_skill_draft(name: str, code: str, description: str) -> tuple[Path, dict]:
    """保存草稿并返回 (path, validation_result)。"""
    d = _skill_dir(name)
    d.mkdir(exist_ok=True)
    tool_path = d / "tool.py"
    tool_path.write_text(code, encoding="utf-8")

    validation = validate_tool_code(code)
    meta = {
        "status": "draft",
        "description": description,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "validation": validation,
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return tool_path, validation


def activate_skill(name: str) -> tuple[bool, str]:
    """激活一个 draft 技能：加载、注册、更新 meta。返回 (success, message)。"""
    d = _skill_dir(name)
    tool_path = d / "tool.py"
    meta_path = d / "meta.json"

    if not tool_path.exists():
        return False, f"找不到技能文件：{tool_path}"

    # 激活前重新验证（防止手动绕过）
    code = tool_path.read_text(encoding="utf-8")
    v = validate_tool_code(code)
    if not v["ok"]:
        return False, "代码验证未通过：\n" + "\n".join(v["errors"])

    try:
        spec = importlib.util.spec_from_file_location(f"skills.{name}", tool_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        tool_def = getattr(module, "TOOL_DEF", None)
        if not tool_def:
            return False, "代码中缺少 TOOL_DEF 字典"

        handler = getattr(module, tool_def["name"], None)
        if not handler:
            return False, f"代码中找不到函数 {tool_def['name']}()"

        register_tool(tool_def, handler)

        # 更新 meta
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        meta["status"] = "active"
        meta["activated_at"] = datetime.now(timezone.utc).isoformat()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        return True, f"工具 {tool_def['name']} 已激活"

    except Exception as e:
        return False, f"加载失败：{type(e).__name__}: {e}"


def deactivate_skill(name: str) -> tuple[bool, str]:
    """将技能标记为 inactive（不卸载当前进程，重启后不加载）。"""
    meta_path = _skill_dir(name) / "meta.json"
    if not meta_path.exists():
        return False, "技能不存在"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = "inactive"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, f"技能 {name} 已停用（重启后生效）"


def load_all_active_skills():
    """启动时自动加载所有 active 状态的技能。"""
    if not SKILLS_DIR.exists():
        return
    for skill_dir in SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        meta_path = skill_dir / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("status") == "active":
            ok, msg = activate_skill(skill_dir.name)
            if not ok:
                print(f"[tool_builder] 加载技能 {skill_dir.name} 失败：{msg}")
            else:
                print(f"[tool_builder] 已加载技能：{skill_dir.name}")


def list_skills_info() -> list[dict]:
    """列出所有技能及其状态。"""
    result = []
    if not SKILLS_DIR.exists():
        return result
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        meta_path = skill_dir / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        result.append({
            "name": skill_dir.name,
            "status": meta.get("status", "unknown"),
            "description": meta.get("description", ""),
            "created_at": meta.get("created_at", ""),
        })
    return result


def read_skill_code(name: str) -> Optional[str]:
    tool_path = _skill_dir(name) / "tool.py"
    if not tool_path.exists():
        return None
    return tool_path.read_text(encoding="utf-8")


def update_skill_code(name: str, new_code: str) -> tuple[bool, str]:
    """更新技能代码（保持当前激活状态不变，需重新激活才生效）。"""
    d = _skill_dir(name)
    if not d.exists():
        return False, "技能不存在"
    (d / "tool.py").write_text(new_code, encoding="utf-8")
    return True, f"代码已更新，请重新激活 {name} 使改动生效"


# ── 注册进主控的元工具 ────────────────────────────────────────────────────────

async def create_tool(name: str, request: str) -> str:
    """
    生成一个新工具并保存为草稿。
    name:    工具名（snake_case，如 stock_price）
    request: 对工具功能的详细描述
    """
    # 检查是否已存在
    existing = read_skill_code(name)
    if existing:
        return json.dumps({
            "__skill_action__": "already_exists",
            "name": name,
            "message": f"工具 {name} 已存在。如需修改，请说「修改 {name} 工具」。"
        }, ensure_ascii=False)

    try:
        code = await _generate_code(name, request)
        _, validation = save_skill_draft(name, code, request)
        return json.dumps({
            "__skill_action__": "code_review",
            "name": name,
            "code": code,
            "validation": validation,
            "message": f"工具 {name} 代码已生成，等待你审查并激活。"
        }, ensure_ascii=False)
    except Exception as e:
        return f"生成工具失败：{e}"


async def edit_tool(name: str, change_request: str) -> str:
    """
    修改已有工具的代码。
    name:           工具名
    change_request: 描述要做什么修改
    """
    existing = read_skill_code(name)
    if not existing:
        return f"工具 {name} 不存在，请先用 create_tool 创建。"

    prompt = f"以下是现有工具代码：\n\n{existing}\n\n请根据要求修改：{change_request}\n\n只输出完整的新代码，不要说明。"
    try:
        client = AsyncOpenAI(api_key=config.OPENROUTER_API_KEY, base_url=config.OPENROUTER_BASE_URL)
        resp = await client.chat.completions.create(
            model=config.CLAUDE_MODEL,
            max_tokens=8192,
            messages=[
                {"role": "system", "content": CODE_GEN_PROMPT},
                {"role": "user",   "content": prompt}
            ]
        )
        new_code = resp.choices[0].message.content.strip()
        if new_code.startswith("```"):
            lines = new_code.split("\n")
            new_code = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        _, validation = save_skill_draft(name, new_code, change_request)
        return json.dumps({
            "__skill_action__": "code_review",
            "name": name,
            "code": new_code,
            "validation": validation,
            "message": f"工具 {name} 代码已修改，等待你审查并重新激活。"
        }, ensure_ascii=False)
    except Exception as e:
        return f"修改工具失败：{e}"


async def list_tools_meta() -> str:
    """列出所有自建工具及其状态。"""
    skills = list_skills_info()
    if not skills:
        return "还没有自建工具。"
    lines = ["【自建工具列表】"]
    for s in skills:
        status_icon = {"active": "✅", "draft": "📝", "inactive": "⏸️"}.get(s["status"], "❓")
        lines.append(f"{status_icon} {s['name']} — {s['description'][:40]}")
    return "\n".join(lines)


# ── 工具定义 ──────────────────────────────────────────────────────────────────

META_TOOL_DEFS = [
    {
        "name": "create_tool",
        "description": (
            "创建一个新的自定义工具。当用户说「帮我做一个工具」、「我需要一个能做X的工具」、"
            "「创建一个工具来Y」时使用。会生成代码供用户审查后激活。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":    {"type": "string", "description": "工具名，snake_case，如 stock_price、send_email"},
                "request": {"type": "string", "description": "对工具功能的详细描述，越具体越好"},
            },
            "required": ["name", "request"]
        }
    },
    {
        "name": "edit_tool",
        "description": (
            "修改已有的自建工具代码。当用户说「修改X工具」、「X工具有bug」、「X工具需要增加Y功能」时使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":           {"type": "string", "description": "要修改的工具名"},
                "change_request": {"type": "string", "description": "描述要做什么修改"},
            },
            "required": ["name", "change_request"]
        }
    },
    {
        "name": "list_tools_meta",
        "description": "列出所有自建工具及其状态（激活/草稿/停用）。",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "create_schedule",
        "description": (
            "创建一个定时任务，让贾维斯在指定时间自动执行某项工作并推送结果。"
            "适用于：每日情报收集、定期提醒、周期性报告等场景。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":             {"type": "string", "description": "任务名，snake_case，如 components_daily"},
                "description":      {"type": "string", "description": "任务描述，便于日后识别"},
                "cron":             {"type": "string", "description": "cron 表达式，如 '0 8 * * *'（每天8点），'0 8 * * 1-5'（工作日8点）"},
                "prompt":           {"type": "string", "description": "触发时发给贾维斯的完整指令，越具体越好"},
                "delivery_type":    {"type": "string", "description": "推送方式：feishu（飞书）或 file（本地文件），默认 file"},
                "receive_id":       {"type": "string", "description": "飞书接收方 ID（delivery_type=feishu 时必填）"},
                "receive_id_type":  {"type": "string", "description": "飞书 ID 类型：open_id / user_id / chat_id，默认 open_id"},
            },
            "required": ["name", "description", "cron", "prompt"]
        }
    },
    {
        "name": "list_schedules",
        "description": "列出所有定时任务及其状态和下次执行时间。",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "delete_schedule",
        "description": "删除一个定时任务。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要删除的任务名"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "pause_schedule",
        "description": "暂停一个定时任务（保留配置，停止执行）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要暂停的任务名"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "resume_schedule",
        "description": "恢复一个已暂停的定时任务。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要恢复的任务名"}
            },
            "required": ["name"]
        }
    },
]

async def _handle_create_tool(name: str, request: str) -> str:
    return await create_tool(name=name, request=request)

async def _handle_edit_tool(name: str, change_request: str) -> str:
    return await edit_tool(name=name, change_request=change_request)

async def _handle_list_tools_meta() -> str:
    return await list_tools_meta()

async def _handle_create_schedule(
    name: str, description: str, cron: str, prompt: str,
    delivery_type: str = "file", receive_id: str = "", receive_id_type: str = "open_id"
) -> str:
    from core.scheduler import create_schedule
    ok, msg = create_schedule(name, description, cron, prompt, delivery_type, receive_id, receive_id_type)
    return msg

async def _handle_list_schedules() -> str:
    from core.scheduler import list_schedules
    schedules = list_schedules()
    if not schedules:
        return "还没有定时任务。"
    lines = ["【定时任务列表】"]
    for s in schedules:
        icon = "▶️" if s["status"] == "active" else "⏸️"
        lines.append(f"{icon} {s['name']} — {s['description']}\n   cron: {s['cron']} | 推送: {s['delivery_type']} | 下次: {s['next_run']}")
    return "\n".join(lines)

async def _handle_delete_schedule(name: str) -> str:
    from core.scheduler import delete_schedule
    ok, msg = delete_schedule(name)
    return msg

async def _handle_pause_schedule(name: str) -> str:
    from core.scheduler import pause_schedule
    ok, msg = pause_schedule(name)
    return msg

async def _handle_resume_schedule(name: str) -> str:
    from core.scheduler import resume_schedule
    ok, msg = resume_schedule(name)
    return msg

META_HANDLERS = {
    "create_tool":      _handle_create_tool,
    "edit_tool":        _handle_edit_tool,
    "list_tools_meta":  _handle_list_tools_meta,
    "create_schedule":  _handle_create_schedule,
    "list_schedules":   _handle_list_schedules,
    "delete_schedule":  _handle_delete_schedule,
    "pause_schedule":   _handle_pause_schedule,
    "resume_schedule":  _handle_resume_schedule,
}


def register_meta_tools():
    for defn in META_TOOL_DEFS:
        register_tool(defn, META_HANDLERS[defn["name"]])
