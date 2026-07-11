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

import config
from core.controller import register_tool
from core.safety import safe_name, is_safe_name
from core.results import ToolResult, Action
from core.skill_policy import ALLOWED_IMPORTS, BLOCKED_IMPORTS, WARNING_PATTERNS, import_rules_text

# ── 静态代码验证 ──────────────────────────────────────────────────────────────

# 导入白名单 / 黑名单 / 可疑模式均来自 core.skill_policy（与提示词同源，见文件顶部 import）


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

    # 3.5 SQL 拼接检查（AST）：execute/executemany 的 SQL 参数若是
    #     f-string / 字符串相加 / .format，提示改用参数化查询，防注入。
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("execute", "executemany") and node.args):
            a0 = node.args[0]
            unsafe = (
                isinstance(a0, ast.JoinedStr)  # f-string
                or (isinstance(a0, ast.BinOp) and isinstance(a0.op, ast.Add))  # "..." + x
                or (isinstance(a0, ast.Call) and isinstance(a0.func, ast.Attribute)
                    and a0.func.attr == "format")  # "...".format(...)
            )
            if unsafe:
                warnings.append("SQL 用 f-string/+/.format 拼接，存在注入风险，请改用参数化查询（? 占位符 + 参数元组）")
                break

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
__IMPORT_RULES__
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

【安全与转义（重要，避免注入类 bug）】
数据进入"另一种语法"之前必须按目标语境处理，绝不能让数据被当成语法执行：
- SQL：一律参数化查询，如 cursor.execute("... WHERE k = ?", (val,))。禁止用 f-string / + / .format 拼接 SQL。
- 文件路径：外部/用户/模型传入的名字先做白名单校验（只允许 [A-Za-z0-9_-]），或用 Path(x).name 去掉目录部分，并确认最终路径 .resolve() 后仍在预期基目录内，禁止把外部字符串直接拼进路径（防 ../ 或绝对路径越权）。
- URL：变量拼进 URL 前，路径段用 urllib.parse.quote、查询参数用 quote_plus 编码。
- 生成 HTML/JS：对插入的文本做 HTML 转义；不要用字符串拼接生成内联事件处理器（onclick 等），改用 data-* 属性或 addEventListener。
- JSON：用 json.dumps 生成，禁止手写字符串拼 JSON。
- 始终把用户、网络、模型返回的内容当作不可信数据处理。

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

# 用单一事实来源（skill_policy）渲染导入规则，替换占位符，保证与校验器永不漂移
CODE_GEN_PROMPT = CODE_GEN_PROMPT.replace("__IMPORT_RULES__", import_rules_text())


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
    return SKILLS_DIR / safe_name(name)   # safe_name 防路径穿越，非法名抛 ValueError


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
    try:
        d = _skill_dir(name)
    except ValueError as e:
        return False, str(e)
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
    try:
        meta_path = _skill_dir(name) / "meta.json"
    except ValueError as e:
        return False, str(e)
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
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[tool_builder] 跳过技能 {skill_dir.name}：meta.json 解析失败（{e}）")
            continue
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
    try:
        tool_path = _skill_dir(name) / "tool.py"
    except ValueError:
        return None
    if not tool_path.exists():
        return None
    return tool_path.read_text(encoding="utf-8")


def update_skill_code(name: str, new_code: str) -> tuple[bool, str]:
    """更新技能代码（保持当前激活状态不变，需重新激活才生效）。"""
    try:
        d = _skill_dir(name)
    except ValueError as e:
        return False, str(e)
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
    if not is_safe_name(name):
        return f"工具名非法：{name!r}（只允许字母、数字、下划线、连字符，长度 1-64）"
    # 检查是否已存在
    existing = read_skill_code(name)
    if existing:
        return f"工具 {name} 已存在。如需修改，请说「修改 {name} 工具」。"

    try:
        code = await _generate_code(name, request)
        _, validation = save_skill_draft(name, code, request)
        message = f"工具 {name} 代码已生成，等待你审查并激活。"
        return ToolResult(text=message, actions=[Action("code_review", {
            "name": name, "code": code, "validation": validation, "message": message,
        })])
    except Exception as e:
        return f"生成工具失败：{e}"


async def edit_tool(name: str, change_request: str) -> str:
    """
    修改已有工具的代码。
    name:           工具名
    change_request: 描述要做什么修改
    """
    if not is_safe_name(name):
        return f"工具名非法：{name!r}（只允许字母、数字、下划线、连字符，长度 1-64）"
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

        # 保留原始 description，不用 change_request 覆盖
        d = _skill_dir(name)
        meta_path = d / "meta.json"
        orig_description = ""
        if meta_path.exists():
            try:
                orig_description = json.loads(meta_path.read_text(encoding="utf-8")).get("description", "")
            except Exception:
                pass

        validation = validate_tool_code(new_code)
        d.mkdir(exist_ok=True)
        (d / "tool.py").write_text(new_code, encoding="utf-8")
        meta = {
            "status": "draft",
            "description": orig_description or change_request,
            "created_at": json.loads(meta_path.read_text(encoding="utf-8")).get("created_at", datetime.now(timezone.utc).isoformat()) if meta_path.exists() else datetime.now(timezone.utc).isoformat(),
            "last_edited_at": datetime.now(timezone.utc).isoformat(),
            "last_change": change_request,
            "validation": validation,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        message = f"工具 {name} 代码已修改，等待你审查并重新激活。"
        return ToolResult(text=message, actions=[Action("code_review", {
            "name": name, "code": new_code, "validation": validation, "message": message,
        })])
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


def delete_skill(name: str) -> tuple[bool, str]:
    """删除一个工具（包括代码和元数据）。"""
    import shutil as _shutil
    try:
        d = _skill_dir(name)
    except ValueError as e:
        return False, str(e)
    if not d.exists():
        return False, f"工具 {name} 不存在"
    _shutil.rmtree(d)
    return True, f"工具 {name} 已删除"


async def cleanup_drafts(confirm_delete: list = None) -> str:
    """
    列出所有草稿工具；如果传入 confirm_delete，则删除指定工具。
    confirm_delete: 要删除的工具名列表，不传则仅展示草稿列表
    """
    skills = list_skills_info()
    drafts = [s for s in skills if s["status"] == "draft"]

    if not confirm_delete:
        if not drafts:
            return "没有草稿工具，工具库干净。"
        lines = ["【草稿工具列表】（以下工具尚未激活）"]
        for s in drafts:
            created = (s.get("created_at") or "")[:10]
            lines.append(f"📝 {s['name']}（{created}）— {s['description'][:50]}")
        lines.append("\n告诉我哪些可以删除，我来执行。")
        return "\n".join(lines)

    deleted, errors = [], []
    for name in confirm_delete:
        ok, msg = delete_skill(name)
        (deleted if ok else errors).append(msg)
    parts = []
    if deleted:
        parts.append("已删除：" + "、".join(deleted))
    if errors:
        parts.append("失败：" + "；".join(errors))
    return "\n".join(parts)


# ── 工具定义 ──────────────────────────────────────────────────────────────────

META_TOOL_DEFS = [
    {
        "name": "create_tool",
        "description": (
            "创建一个新的自定义工具（会写代码、需用户审查后激活）。"
            "【仅当用户明确说要『做/写/创建一个工具（或插件/连接器）』时才用】，"
            "且确认现有工具与可运行的工作流（见 system prompt 的『可运行的工作流』）都满足不了需求。"
            "不要把『搜索/采集信号』『出报告』『查行情』这类普通任务理解成要造工具——"
            "采集信号请用 run_workflow signal_collection，出报告用 generate_report，查行情直接联网作答。"
            "需求不明确时先问，绝不擅自造工具。"
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
        "name": "send_file_to_chat",
        "description": (
            "将本地文件发送到网页对话界面，用户可直接点击下载（在当前对话窗口显示下载卡片）。"
            "【凡是你生成了文件——报告 PDF、导出的 Excel、文档等——都必须调用本工具把文件发给用户，"
            "不要只在回复里报路径】。file_path 为文件完整路径。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "要发送的本地文件完整路径"},
                "filename":  {"type": "string", "description": "显示给用户的文件名（可省略，默认用原文件名）"},
            },
            "required": ["file_path"]
        }
    },
    {
        "name": "delete_tool",
        "description": (
            "删除一个自建工具。当用户说「删除X工具」、「把X工具删掉」时使用。"
            "激活状态的工具也可以删除（重启后不再加载）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要删除的工具名"},
            },
            "required": ["name"]
        }
    },
    {
        "name": "cleanup_drafts",
        "description": (
            "清理草稿工具。先列出所有未激活的草稿，询问用户确认后再删除。"
            "当用户说「清理草稿」、「删掉没用的工具」、「工具库太乱了」时使用。"
            "第一步不传 confirm_delete 只列出草稿；用户确认后再传列表删除。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "confirm_delete": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "确认要删除的工具名列表。不传则仅展示草稿列表。"
                },
            },
        }
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
                "delivery_type":    {"type": "string", "description": "推送方式：目前仅支持 file（本地文件），默认 file"},
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

async def _handle_send_file_to_chat(file_path: str, filename: str = "") -> str:
    import json as _json
    from pathlib import Path as _Path
    fp = _Path(file_path)
    if not fp.exists():
        return f"文件不存在：{file_path}"
    name = filename or fp.name
    size = fp.stat().st_size
    return ToolResult(
        text=f"已将文件「{name}」发送到对话界面，可点击下载。",
        actions=[Action("file_download", {"file_path": str(fp), "filename": name, "size": size})],
    )


async def _handle_create_tool(name: str, request: str) -> str:
    return await create_tool(name=name, request=request)

async def _handle_edit_tool(name: str, change_request: str) -> str:
    return await edit_tool(name=name, change_request=change_request)

async def _handle_list_tools_meta() -> str:
    return await list_tools_meta()

async def _handle_delete_tool(name: str) -> str:
    ok, msg = delete_skill(name)
    return msg

async def _handle_cleanup_drafts(confirm_delete: list = None) -> str:
    return await cleanup_drafts(confirm_delete=confirm_delete)

async def _handle_create_schedule(
    name: str, description: str, cron: str, prompt: str,
    delivery_type: str = "file"
) -> str:
    from core.scheduler import create_schedule
    ok, msg = create_schedule(name, description, cron, prompt, delivery_type)
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
    "send_file_to_chat": _handle_send_file_to_chat,
    "create_tool":      _handle_create_tool,
    "edit_tool":        _handle_edit_tool,
    "list_tools_meta":  _handle_list_tools_meta,
    "delete_tool":      _handle_delete_tool,
    "cleanup_drafts":   _handle_cleanup_drafts,
    "create_schedule":  _handle_create_schedule,
    "list_schedules":   _handle_list_schedules,
    "delete_schedule":  _handle_delete_schedule,
    "pause_schedule":   _handle_pause_schedule,
    "resume_schedule":  _handle_resume_schedule,
}


# 元工具按职责分到三个领域组（原先全是默认的 general）：
#   authoring  自建工具的增删改查
#   scheduling 定时任务管理
#   fileio     文件 I/O（与 connectors/document 的 read_document 同组）
_META_GROUPS = {
    "create_tool": "authoring", "edit_tool": "authoring", "delete_tool": "authoring",
    "cleanup_drafts": "authoring", "list_tools_meta": "authoring",
    "create_schedule": "scheduling", "list_schedules": "scheduling",
    "delete_schedule": "scheduling", "pause_schedule": "scheduling", "resume_schedule": "scheduling",
    "send_file_to_chat": "fileio",
}


def register_meta_tools():
    for defn in META_TOOL_DEFS:
        # 不就地改 def，拷一份注入 group（保持 META_TOOL_DEFS 纯净）
        spec = {**defn, "group": _META_GROUPS.get(defn["name"], "general")}
        register_tool(spec, META_HANDLERS[defn["name"]])


# 模块导入即注册元工具（统一为"导入即自注册"，main.py 不再显式调用）
register_meta_tools()
