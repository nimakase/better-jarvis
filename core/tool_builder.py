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
import asyncio
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from openai import AsyncOpenAI

import config
from core import registry

# 工具生成的输出上限（token）。工具偏大时，重写整份文件的输出很容易在结尾 TOOL_DEF
# 处被截断，丢掉收尾的 }，导致"'{' was never closed"这类永远修不好的语法错误。
# 给足预算 + 截断检测（见 _call_codegen）从根上避免。可用 env 覆盖。
_TOOL_GEN_MAX_TOKENS = int(os.environ.get("JARVIS_TOOL_GEN_MAX_TOKENS", "16000"))
# 造工具的"生成→验证→按真实报错改"有界循环的最大轮数（三期）。可 env 覆盖。
_TOOL_AUTHOR_MAX_ATTEMPTS = int(os.environ.get("JARVIS_TOOL_AUTHOR_MAX_ATTEMPTS", "3"))
from core.controller import register_tool
from core.safety import safe_name, is_safe_name
from core.results import ToolResult, Action
from core.skill_policy import (
    ALLOWED_IMPORTS, BLOCKED_IMPORTS, WARNING_PATTERNS, import_rules_text,
    is_reusable_import, building_blocks_api_text, check_building_block_usage,
    check_runtime_contract, BUILDING_BLOCKS,
)
from core.source_read import read_symbol as _read_symbol

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
                # import a.b → 判 "a.b"
                names = [(alias.name, alias.name) for alias in node.names]
            else:
                # from a import b → 判 "a"，但白名单额外看 "a.b"：
                # `from core import results` 与 `from core.results import X` 是同一件事，
                # 只按 module("core") 判会把前者误拦。
                mod = node.module or ""
                names = [(mod, f"{mod}.{alias.name}" if mod else alias.name)
                         for alias in node.names] if mod else []

            for name, full in names:
                root = name.split(".")[0]
                # building block 白名单【先于】根黑名单：黑名单按根模块粗粒度拦
                # （core/connectors 整包），但个别子模块（如 core.results——工具
                # 返回文件/动作的法定接口）是显式授权给技能复用的。顺序反了会导致
                # 白名单形同虚设：core.* 永远到不了 is_reusable_import 那一行。
                if is_reusable_import(name) or is_reusable_import(full):
                    pass  # 允许复用的第一方 building block（见 skill_policy.BUILDING_BLOCKS）
                elif root in BLOCKED_IMPORTS:
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

    # 5. building block API 一致性（阻断级）：抓"在复用的 building block 上调用了不存在的
    #    方法/属性"这类幻觉——import 对了、静态过、真跑才 AttributeError 的正是这类。
    errors.extend(check_building_block_usage(code))

    # 6. 运行环境契约（阻断级）：抓"假设了自己不身处的环境"——典型是 input() 读 stdin。
    #    这类调用能过语法、能过 import 冒烟（冒烟不执行 handler），只有真被调用时才
    #    表现为「工具卡住不返回」，且用户在对话里看不到任何提示，排查成本极高。
    errors.extend(check_runtime_contract(code))

    # 7. SELFTEST 质量（阻断级）：声明了就必须真的断言点什么，空测试比没测试更糟。
    errors.extend(check_selftest_quality(code))

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

【运行环境契约（重要，决定工具能不能真的被用起来）】
你写的工具运行在**贾维斯服务进程**里，由模型在对话中调用（网页 WebSocket 或飞书）。因此：
- **没有终端**。禁止 `input()`、`getpass()` 等任何从 stdin 读取的调用——服务进程的 stdin 不是终端，
  轻则 EOFError，重则永久挂起且无法中断。要用户做某件事，只能 **return 一句说明**让他去做，
  或调用 building block 里【内部轮询等待】的方法（如 `login_bootstrap()`）。
- **print 不会被用户看到**。`print`/`sys.stdout` 只进服务器日志。所有要给用户的信息，
  一律通过**返回值字符串**传达。需要记录调试信息用 `logging`。
- **不允许无上限阻塞**。任何等待（网络、浏览器、用户操作）都必须有超时上限；
  `httpx`/`openai` 调用一律显式传 `timeout=`。长任务要能在上限内结束并如实汇报进度。
- **有头浏览器弹在服务器那台机器上**，不是用户面前。若工具会弹窗，必须在返回值里说明这一点。

【结果诚实性（重要，避免"看起来成功其实是错的"）】
- 凡是"处理全部 / 遍历所有 / 批量"的工具，**必须如实报告实际覆盖范围**。分页列表只读到了第一页、
  数量被上限截断、部分条目失败跳过——这些都要出现在返回值里（例如"共 500 条，本次覆盖前 50 条"）。
  **静默截断是最严重的缺陷**：用户拿到一份看起来完整的结果，却无从发现它不完整。
- 判定/分类类工具遇到"拿不准"，不要静默归入否定项。输出第三态（如 uncertain）让用户自己复核。
- 抓不到数据时，要区分"确实是空"和"解析失败"，分别给不同的返回文案——
  把解析失败报成"没有数据"会让人往完全错误的方向排查。

【SELFTEST（可选，但解析/计算/转换类工具强烈建议写）】
冒烟阶段【只 import 不执行 handler】，所以"能加载但结果是错的"这类缺陷平时抓不到。
你可以额外定义一个模块级函数 `SELFTEST()`，它会在隔离子进程里【真的被执行】：

def SELFTEST():
    # 用内置的假数据跑纯逻辑，断言结果正确
    assert _parse_size("1.5 KB") == 1536, _parse_size("1.5 KB")
    assert _normalize("  A/B ") == "a-b"

硬性要求：
- 必须【只跑纯逻辑】：不联网、不开浏览器、不读写文件、不读环境变量、不调 LLM。
  拿不到真实环境就构造最小假数据（假 HTML 片段、假 JSON、假行列表）。
- 必须有真正会失败的 `assert`。**空测试比没有测试更糟**（制造"已验证"的错觉），
  写不出有意义的断言就整个别写。
- 要快（1 秒内）、无副作用、可重复运行。
- 重点测那些【猜错了不会报错、只会悄悄算错】的地方：下标/偏移、边界与去重、
  单位与进制换算、字段映射、分页或截断的终止条件。

【能力边界（重要，避免臆造不存在的接口）】
- 你【看不到】app 的其它内部模块源码（core / connectors / config / main），禁止 import 或臆造它们的 API。
- 需要复用第一方能力时，【只能】用下面【可复用 building block 的真实 API】里列出的类/方法，且严格按给出的签名调用——不要臆造 `.create()`、`.fetch_xxx()` 这类没列出的方法。
- 若某个所需能力在下面这些 building block 里【根本不存在】，不要编一个方法来假装能做：让工具直接返回一句明确说明（"该能力当前缺失，需要先给 <building block> 增补 <方法>"），然后停手。诚实报缺失，好过写一个跑起来就崩的工具。
- LLM/密钥/模型从 os.environ 读（`OPENROUTER_API_KEY` / `OPENROUTER_BASE_URL` / `CLAUDE_MODEL` / `CLAUDE_MODEL_LIGHT`，app 已导出），用 `openai.AsyncOpenAI`；缺失就明确报错，不要硬编码模型名。

【可复用 building block 的真实 API（权威——只能按这些签名调用）】
__BUILDING_BLOCKS_API__

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

# 用单一事实来源（skill_policy）渲染导入规则 + 可复用 building block 的真实 API，
# 替换占位符，保证与校验器永不漂移、且模型拿到的是权威签名而非臆造。
CODE_GEN_PROMPT = (
    CODE_GEN_PROMPT
    .replace("__IMPORT_RULES__", import_rules_text())
    .replace("__BUILDING_BLOCKS_API__", building_blocks_api_text())
)


def _strip_markdown_fence(code: str) -> str:
    """去掉 AI 可能包的 markdown 代码块标记。"""
    if code.startswith("```"):
        lines = code.split("\n")
        code = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return code


class CodeGenTruncated(Exception):
    """代码生成因超出 token 上限被截断（finish_reason == 'length'）。"""


async def _call_codegen(user_content: str, max_tokens: int = None) -> str:
    """底层代码生成调用（create/edit 共用）：带【截断检测 + 自动加预算重试】。
    若模型输出因触顶被截断（finish_reason == 'length'，典型症状=结尾 } 缺失），
    自动加倍预算重试一次；仍截断则抛 CodeGenTruncated，交由上层给出清晰报错，
    绝不把半截坏代码当成结果保存。"""
    base = max_tokens or _TOOL_GEN_MAX_TOKENS
    from core.llm import get_client
    client = get_client(timeout=300)   # codegen 出整文件，给长超时（core/llm 单一构建点）
    last_mt = base
    for mt in (base, base * 2):
        last_mt = mt
        resp = await client.chat.completions.create(
            model=config.CLAUDE_MODEL,
            max_tokens=mt,
            messages=[
                {"role": "system", "content": CODE_GEN_PROMPT},
                {"role": "user",   "content": user_content},
            ],
        )
        choice = resp.choices[0]
        code = _strip_markdown_fence((choice.message.content or "").strip())
        if getattr(choice, "finish_reason", None) != "length":
            return code
        # 触顶截断 → 加倍预算再试
    raise CodeGenTruncated(
        f"代码生成被截断（已加到 {last_mt} tokens 仍超长）。这个工具可能太大——"
        "建议拆成更小的工具，或调高 JARVIS_TOOL_GEN_MAX_TOKENS 后重试。"
    )


def validation_summary(v: dict) -> str:
    """把校验结果渲染成人/模型可读的一段文字（供 read/review/飞书卡片复用）。"""
    if not v:
        return "（无校验信息）"
    parts = []
    parts.append("✅ 静态校验通过" if v.get("ok") else "❌ 静态校验未通过")
    if v.get("errors"):
        parts.append("阻断级错误：\n" + "\n".join(f"  · {e}" for e in v["errors"]))
    if v.get("warnings"):
        parts.append("警告：\n" + "\n".join(f"  · {w}" for w in v["warnings"]))
    return "\n".join(parts)


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


# 冒烟脚本：import + 结构断言 + 【可选的 SELFTEST】。
#
# SELFTEST 的动机（2026-07-18）：此前冒烟【只 import 不执行 handler】（为避免真实副作用），
# 于是"能加载但结果是错的"这类缺陷全部逃逸——oem_ems_screener 的列索引错位、静默只抓
# 第一页都属此类，三道门全绿却全错。SELFTEST 给工具一个自证正确性的口子：
# 用内置假数据跑纯逻辑，不碰网络/浏览器/文件，因此可以安全地在冒烟阶段【真的执行】。
# 它是可选的，但一旦声明就必须通过，否则失败原因会被喂回生成循环重写。
_SMOKE_SCRIPT = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('smoke_mod', sys.argv[1])\n"
    "m = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(m)\n"          # import/加载阶段：捕获 ImportError、缺依赖、模块级崩溃
    "td = getattr(m, 'TOOL_DEF', None)\n"
    "assert isinstance(td, dict) and td.get('name'), 'TOOL_DEF 缺失或没有 name'\n"
    "assert callable(getattr(m, td['name'], None)), 'handler 函数不存在或不可调用'\n"
    "st = getattr(m, 'SELFTEST', None)\n"
    "if callable(st):\n"
    "    import asyncio, inspect\n"
    "    r = st()\n"
    "    if inspect.isawaitable(r):\n"
    "        r = asyncio.get_event_loop().run_until_complete(r)\n"
    "    print('SELFTEST_RAN')\n"
    "print('SMOKE_OK')\n"
)


def check_selftest_quality(code: str) -> list:
    """静态检查 SELFTEST 的质量：声明了就必须真的断言点什么。

    防的是"空测试"——`def SELFTEST(): return True` 能过冒烟却什么也没验证，
    比没有测试更糟（给人以已验证的错觉）。这与 self_iteration 用「先红后绿」
    挡空测试是同一个思路的轻量版。返回阻断级错误列表。
    """
    import ast as _ast
    try:
        tree = _ast.parse(code)
    except Exception:
        return []
    for node in tree.body:
        if not (isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and node.name == "SELFTEST"):
            continue
        asserts = sum(1 for n in _ast.walk(node) if isinstance(n, _ast.Assert))
        raises = sum(1 for n in _ast.walk(node)
                     if isinstance(n, _ast.Raise))
        if asserts + raises == 0:
            return ["SELFTEST 里没有任何 assert / raise —— 空测试比没有测试更糟"
                    "（会造成『已验证』的错觉）。要么写出真正能失败的断言，"
                    "要么整个删掉 SELFTEST。"]
        return []
    return []


def smoke_import_skill(tool_path: Path, timeout: int = 45) -> tuple[bool, str]:
    """在【隔离子进程】里 import 该技能、断言 TOOL_DEF/handler 存在，并执行可选的 SELFTEST。
    handler 本身【仍不执行】（避免真实副作用）。返回 (ok, message)。"""
    import subprocess
    try:
        r = subprocess.run(
            [sys.executable, "-c", _SMOKE_SCRIPT, str(tool_path)],
            capture_output=True, text=True, timeout=timeout, env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return False, f"冒烟测试超时（>{timeout}s，import 阶段卡住，可能有阻塞/死循环的模块级代码）"
    except Exception as e:
        return True, f"（冒烟测试无法运行，跳过：{type(e).__name__}: {e}）"  # 环境问题不阻断
    if r.returncode == 0 and "SMOKE_OK" in r.stdout:
        return True, "ok（含 SELFTEST）" if "SELFTEST_RAN" in r.stdout else "ok"
    err = (r.stderr or r.stdout or "").strip()
    last = err.splitlines()[-1] if err else "未知错误"
    # 区分 import 失败与 SELFTEST 失败——两者的修法完全不同，报错必须说清是哪个
    if "SELFTEST_RAN" in r.stdout or "SELFTEST" in err:
        return False, f"SELFTEST 未通过（工具能加载，但自检断言失败——逻辑是错的）：{last}"
    return False, f"冒烟测试未通过（隔离子进程里 import/加载失败）：{last}"


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

    # 激活前重新验证（防止手动绕过）：静态校验 + building block API 一致性
    code = tool_path.read_text(encoding="utf-8")
    v = validate_tool_code(code)
    if not v["ok"]:
        return False, "代码验证未通过：\n" + "\n".join(v["errors"])

    # 运行时冒烟：先在隔离子进程里验证能 import/加载，再在主进程 exec+注册
    ok_smoke, smoke_msg = smoke_import_skill(tool_path)
    if not ok_smoke:
        return False, smoke_msg

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

        # 用 register_skill_tool（origin=skill，允许替换自己之前的注册）——
        # 这样"编辑工具→重新激活"能在不重启进程的情况下即时刷新 schema/handler。
        ok, rmsg = registry.register_skill_tool(tool_def, handler)
        if not ok:
            return False, rmsg

        # 更新 meta（记下实际注册的工具名，供 deactivate/delete 精确注销）。
        # 生命周期：draft → trial（首次激活，试用期）→ active（用户确认保留，keep_skill）。
        # 已是 active 的（编辑后重激活）保持 active，不降级。
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        was_active = meta.get("status") == "active"
        meta["status"] = "active" if was_active else "trial"
        meta["tool_name"] = tool_def["name"]
        meta["activated_at"] = datetime.now(timezone.utc).isoformat()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        if was_active:
            return True, f"工具 {tool_def['name']} 已重新激活"
        return True, (f"工具 {tool_def['name']} 已激活（试用期）。可直接使用；"
                      f"用户说「留下/就它了」再转正（keep_tool），说「放弃」则删除并清点其产物。")

    except Exception as e:
        return False, f"加载失败：{type(e).__name__}: {e}"


def keep_skill(name: str) -> tuple[bool, str]:
    """把试用中的技能转正（trial → active）。用户说「留下/就它了」时调。"""
    try:
        meta_path = _skill_dir(name) / "meta.json"
    except ValueError as e:
        return False, str(e)
    if not meta_path.exists():
        return False, "技能不存在"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("status") == "active":
        return True, f"技能 {name} 本就是正式状态"
    if meta.get("status") != "trial":
        return False, f"技能 {name} 当前是 {meta.get('status')!r}，只有试用中(trial)的能转正"
    meta["status"] = "active"
    meta["kept_at"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, f"技能 {name} 已转正保留"


def deactivate_skill(name: str) -> tuple[bool, str]:
    """停用技能：标记 inactive，并【从活着的注册表里注销】，当前进程立即失效。"""
    try:
        meta_path = _skill_dir(name) / "meta.json"
    except ValueError as e:
        return False, str(e)
    if not meta_path.exists():
        return False, "技能不存在"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = "inactive"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    # 从运行中的注册表注销（用记录的实际工具名，回退目录名），无需重启即时生效
    registry.unregister(meta.get("tool_name") or name)
    return True, f"技能 {name} 已停用（当前进程已即时生效）"


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
        if meta.get("status") in ("active", "trial"):
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


def read_skill_meta(name: str) -> Optional[dict]:
    """读取技能 meta.json（含 status / validation 等）。"""
    try:
        meta_path = _skill_dir(name) / "meta.json"
    except ValueError:
        return None
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


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


# ── 业务背景注入：造工具时也要知道「用户是谁、在做什么生意」──────────────────
#
# 动机（2026-07-18，oem_ems_screener 复盘）：此前造工具的提示词只有「工具名 + 需求」，
# 用户档案一个字都进不去。于是模型写业务判定类工具时，只能从需求字面出发。
# 实例：写「筛 OEM/EMS」的工具时，它问的是「这家公司是不是真制造商」——而用户做的是
# ESO（呆滞料）交易，真正该问的是「它在供应链的哪一端：消耗元器件还是生产元器件」。
# 后者需要知道用户靠什么赚钱才问得出来。档案里就写着，只是没送到。
#
# 安全面：档案本来每轮对话就注入 system prompt 发往同一个 OpenRouter，这里不新增
# 暴露面。设 JARVIS_TOOL_AUTHOR_PROFILE=0 可关闭。
_AUTHOR_USE_PROFILE = os.environ.get("JARVIS_TOOL_AUTHOR_PROFILE", "1") != "0"


def _user_context_text() -> str:
    """把用户档案渲染成一段「业务背景」注入造工具提示词。取不到一律降级为空串。"""
    if not _AUTHOR_USE_PROFILE:
        return ""
    try:
        from core import profile
        block = profile.build_block()
    except Exception:
        return ""
    if not block:
        return ""
    return (
        "【用户背景（据此理解需求的真实意图，尤其是业务判据）】\n"
        f"{block}\n\n"
        "用法说明：\n"
        "- 需求描述往往只写了「做什么」，没写「为什么」。写涉及业务判断的工具"
        "（打分、分类、筛选、排序、话术）时，先想清楚用户拿这个结果去干什么，"
        "据此确定判据；不要照着需求里的名词字面直译。\n"
        "- 与本工具无关的个人条目（家人、日程偏好等）直接忽略，不要写进代码或提示词。\n"
        "- 若你据此做了一个需求里没明说的业务假设，【必须】在工具的 docstring 里"
        "写明这条假设，让用户能一眼看到并纠正。\n\n"
    )


# ── 需求澄清门：写代码前先问「不问就会写出静默错误」的那几个点 ─────────────────
#
# 动机（2026-07-18，oem_ems_screener 复盘）：那个工具返工的根因几乎全在需求层面，
# 没有一条是代码 bug——
#   · 原始需求写「max_companies 默认 0=全部」，"全部"这个词本身就假设了不分页，
#     而目标视图有 532 条 / 22 页，于是工具静默只抓第一页
#   · 需求说「判断是否 OEM/EMS」，但用户做 ESO 交易，真正的判据是供应链位置
#     （消耗元器件 vs 生产元器件），照字面写出来的判据是错的
# 这些问题问一句就能避免，模型却从来不问。
#
# 【设计上最大的风险是变成盘问】。造个小工具被追着问三轮，很快就没人用了。
# 所以门槛定得很高：只问「不问就会产出静默错误结果」的点，最多 3 条，默认不问。
# 关掉：JARVIS_TOOL_AUTHOR_CLARIFY=0
_AUTHOR_CLARIFY = os.environ.get("JARVIS_TOOL_AUTHOR_CLARIFY", "1") != "0"
_MAX_CLARIFY_QUESTIONS = 3

_CLARIFY_PROMPT = """你要为用户写一个自建工具。在写代码【之前】，判断有没有【必须先问清楚】的点。

工具名：{name}
需求：{request}
{context}
【只问这四类】——它们的共同点是：猜错了不会报错，只会悄悄产出错的结果：
1. 规模与分页：要处理的数据有多少条？来源会分页/滚动加载吗？"全部"到底是多少？
   （典型事故：需求说"处理全部"，实际有几十页，工具只抓了第一页还宣称抓全了）
2. 业务判据：需求里的分类/打分/筛选标准，按字面理解和按用户的实际用途理解是否一致？
   用户拿这个结果去做什么决策？
   （典型事故：需求说"筛出制造商"，用户其实要的是"会消耗某种物料的下游厂商"，
     照字面写会把上游原厂错判进来）
3. 关键取舍：有多种合理做法且结果差别大，选错要返工的地方。
4. 失败处理：数据缺失/接口失效/权限不足时，应该报错停下、还是降级继续？

【绝对不要问】：
- 你自己能用合理默认值决定的（文件放哪、超时多少、并发数、输出格式细节）
- 纯技术实现细节（用哪个库、函数怎么命名、要不要写日志）
- 需求里已经写清楚的
- 客套或确认性问题（"你是要我写一个 X 工具对吗"）

判断标准：**如果这个问题猜错了，用户会拿到一份看起来正常、实际是错的结果吗？**
是 → 值得问。否 → 不要问。

多数工具【不需要】问任何问题。宁可不问也不要凑数。

若无需提问，只回复 NONE。
若确有必须问的，每行一个问题，最多 {maxq} 条，中文，每条一句话、具体可答，
并在问题后用括号简述你打算采用的默认假设（这样用户不回也能继续）。"""


async def _gather_clarifications(name: str, request: str) -> list:
    """轻模型门控：返回必须先问用户的问题列表；无则空列表。失败一律降级为空
    （澄清步骤绝不阻断造工具）。"""
    if not _AUTHOR_CLARIFY:
        return []
    try:
        from core.llm import get_client
        client = get_client()   # 轻调用，默认有界超时（core/llm 单一构建点）
        resp = await client.chat.completions.create(
            model=config.CLAUDE_MODEL_LIGHT, max_tokens=500, timeout=30,
            messages=[{"role": "user", "content": _CLARIFY_PROMPT.format(
                name=name, request=request, context=_user_context_text(),
                maxq=_MAX_CLARIFY_QUESTIONS)}],
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception:
        return []
    if not text or "NONE" in text.upper().split():
        return []
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*0123456789.、 ").strip()
        if len(line) > 4:
            out.append(line)
        if len(out) >= _MAX_CLARIFY_QUESTIONS:
            break
    return out


# ── 环境探针：写代码前先看一眼目标网页的真实结构 ──────────────────────────────
#
# 动机：抓取类工具最大的失败源是「代码和目标网页对不上」，而模型从没见过那个页面。
# 实测（oem_ems_screener）：翻页选择器靠倒推猜，5 个候选只中 1 个，真正稳的那个
# （button[data-next-page='true']）谁都没猜到。差别不在谁猜得准，在于能不能去看一眼。
#
# 门控设计（避免每次造工具都白开一次浏览器）：
#   1. 需求里没有 http(s) URL → 直接跳过，零成本。绝大多数工具走这条。
#   2. 有 URL → 才启动 headless 浏览器抽一份【结构摘要】注入提示词。
# 只读：只导航 + 读 DOM，不点击不填表。URL 只取自用户的需求描述，不接受页面里发现的
# URL（防注入）。失败一律降级为空串。关闭：JARVIS_TOOL_ENV_PROBE=0
_AUTHOR_ENV_PROBE = os.environ.get("JARVIS_TOOL_ENV_PROBE", "1") != "0"


async def _gather_env_probe(request: str) -> str:
    """需求里带 URL 时，实地探一份目标页面结构摘要。无 URL / 失败均返回空串。"""
    if not _AUTHOR_ENV_PROBE:
        return ""
    try:
        from core import env_probe
    except Exception:
        return ""
    urls = env_probe.extract_urls(request, limit=1)
    if not urls:
        return ""    # 绝大多数工具在这里零成本返回
    url = urls[0]
    # 复用 app 的浏览器 profile，这样已登录页面也看得到（与技能运行时同一份会话）
    profile_dir = None
    try:
        profile_dir = config.DATA_DIR / "hubspot" / "chrome_profile"
        if not profile_dir.exists():
            profile_dir = None
    except Exception:
        pass
    try:
        digest = await asyncio.to_thread(env_probe.probe_page, url, profile_dir)
    except Exception:
        return ""
    if not digest:
        return ""
    return (
        "【目标页面的真实结构（探针实地抓取，权威——照这里的属性写选择器，"
        "绝不要凭经验猜）】\n" + digest + "\n\n"
    )


# ── MCP 能力浏览闸门（任务 #7）：造涉外平台的工具前，先看有没有现成 MCP server ──
#
# 动机：贾维斯造技能最容易在"根本不知道某个外部系统提供什么能力"这一步就开始
# 猜（例：给飞书加多维表格能力，不知道飞书有多维表格，也不知道官方已经把整套
# 能力包成了 MCP server）。core/mcp_discovery.py（任务 #6）已经能连 server 拿
# 权威工具清单，这里把它接进造技能的生成提示词，分两层生效：
#   1. 确定性、零成本的自动匹配：已配置的 server 名和 request 词面有重叠 →
#      直接发现一次并把权威清单注入提示词（跟 _gather_env_probe 对网页结构探针
#      同一个思路：能拿到权威材料就不该让模型凭猜）。
#   2. 无论匹不匹配，都带一句简短的标准指令——涉及对接外部平台/服务时，先想
#      有没有配置对应 MCP server（mcp_list_servers/mcp_discover_tools 可查/可连），
#      真没有就在生成说明里如实说"没有可核实来源，基于训练知识实现，建议验证"，
#      不要装作很确定。这条指令是"闸"真正强制的部分——不管匹没匹配到都会出现，
#      不依赖猜测式匹配是否命中。
_MCP_STANDING_NOTE = (
    "【关于对接外部平台/服务】若这个工具要跟某个外部平台/服务打交道（如某个 "
    "SaaS、办公协作工具的开放能力），先想一下是否已经配置了它的 MCP server——"
    "可以用 mcp_list_servers 看配置清单、mcp_discover_tools 拿权威工具清单据此"
    "实现（比凭训练知识/文档印象猜方法名可靠）。如果确认没有配置对应 server，"
    "就在生成说明里如实指出「未找到可核实来源，本工具基于训练知识实现，建议用后"
    "先验证一遍」，不要假装很确定。\n\n"
)


async def _gather_mcp_hints(request: str) -> str:
    """已配置的 MCP server 里，若有名字/描述与 request 词面重叠的，自动发现一次
    并把权威工具清单注入提示词；同时始终附带一句标准指令（见上）。全程失败降级
    为只保留标准指令，绝不阻断造工具。"""
    try:
        from core import mcp_discovery
        names = mcp_discovery.configured_servers()
        if not names:
            return _MCP_STANDING_NOTE
        from core.capability import _score, _tokens
        q = _tokens(request)
        # 注意方向：query=server 名的词、target=request 的词——server 名通常很短
        # （2~4 个词/二元组），"名字里的词有多大比例被 request 提到"是比反过来更
        # 干净的信号；反过来算（request 词有多少落在短短的 server 名里）会被
        # request 里的大量无关词面稀释，实测漏掉明显相关的例子。
        hit_names = [n for n in names if _score(_tokens(n), q) >= 0.5]
        if not hit_names:
            return _MCP_STANDING_NOTE
        blocks = []
        for n in hit_names[:2]:   # 最多自动连两个，避免造一次工具触发一堆连接
            result = await mcp_discovery.discover(n, use_cache=True, timeout=15)
            if not result["ok"] or not result["tools"]:
                continue
            lines = [f"『{n}』MCP server 的权威工具清单（据此实现，不要臆造未列出的方法）："]
            for t in result["tools"]:
                lines.append(f"  - {t['name']}：{(t.get('description') or '')[:150]}")
            blocks.append("\n".join(lines))
        if not blocks:
            return _MCP_STANDING_NOTE
        return "【" + "；".join(hit_names) + " 看起来与需求相关，已自动发现】\n\n" \
               + "\n\n".join(blocks) + "\n\n" + _MCP_STANDING_NOTE
    except Exception:
        return _MCP_STANDING_NOTE


# ── 两趟参考注入：写代码前先读现有源码学真实用法/网页结构（省 token 的门控式做法）──

_REFERENCE_MODULES = list(BUILDING_BLOCKS.keys())   # 允许被参考的第一方模块
_MAX_REFERENCE_SYMBOLS = int(os.environ.get("JARVIS_TOOL_REF_MAX_SYMBOLS", "12"))


async def _gather_references(task_desc: str) -> str:
    """第一趟（便宜的轻模型调用，门控）：问模型"要不要参考现有代码、参考哪些符号"，
    再用 read_symbol 把它点名的【真实源码片段】取回，拼成可注入生成提示词的参考块。
    自包含工具会回 NONE → 返回空串，不产生任何额外 token 开销。失败一律优雅降级为空。"""
    if not _REFERENCE_MODULES:
        return ""
    ask = (
        f"你要写一个自建工具，任务：{task_desc}\n\n"
        f"可参考的第一方模块（会把你点名的真实源码片段给你）：{', '.join(_REFERENCE_MODULES)}\n"
        "如果写对它需要参考其中某些类/方法/常量（例如理解真实用法、网页结构、选择器），"
        "就列出你要读的符号，每行一个，格式 `module:symbol`"
        "（symbol 可为 ClassName、ClassName.method 或模块级常量名）。只读你真正需要的，别贪多。\n"
        "若这个工具自包含、不需要参考任何第一方代码，只回复 NONE。"
    )
    try:
        from core.llm import get_client
        client = get_client()   # 轻调用，默认有界超时（core/llm 单一构建点）
        resp = await client.chat.completions.create(
            model=config.CLAUDE_MODEL_LIGHT, max_tokens=400, timeout=30,
            messages=[{"role": "user", "content": ask}],
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""   # 参考步骤绝不阻断造工具
    if not text or "NONE" in text.upper().split():
        return ""

    blocks, seen = [], set()
    for line in text.splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip().strip("`")
        if ":" not in line:
            continue
        module, _, symbol = line.partition(":")
        module, symbol = module.strip(), symbol.strip()
        if module not in _REFERENCE_MODULES or not symbol or (module, symbol) in seen:
            continue
        seen.add((module, symbol))
        blocks.append(_read_symbol(module, symbol))
        if len(blocks) >= _MAX_REFERENCE_SYMBOLS:
            break
    if not blocks:
        return ""
    return (
        "【参考：以下是你点名要读的第一方【真实源码片段】（权威——据此写对真实用法/选择器，"
        "严格不要臆造没出现过的方法/属性）】\n\n" + "\n\n".join(blocks) + "\n\n"
    )


# ── 造工具的有界自我修正循环（三期：生成→验证门→按真实报错改）────────────────

def _write_draft(name: str, code: str, description: str, extra_meta: dict = None) -> tuple[Path, dict]:
    """写草稿 tool.py + meta（保留原 created_at/description），返回 (path, validation)。"""
    d = _skill_dir(name)
    d.mkdir(exist_ok=True)
    tool_path = d / "tool.py"
    tool_path.write_text(code, encoding="utf-8")
    validation = validate_tool_code(code)
    prev = read_skill_meta(name) or {}
    meta = {
        "status": "draft",
        "description": description or prev.get("description", "") or "",
        "created_at": prev.get("created_at", datetime.now(timezone.utc).isoformat()),
        "validation": validation,
    }
    if extra_meta:
        meta.update(extra_meta)
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return tool_path, validation


def _write_back_building_block_notes(name: str, description: str, code: str) -> None:
    """任务 #12：技能通过静态校验+隔离冒烟后，给它实际 import 过的 BUILDING_BLOCKS
    模块各记一笔实践验证笔记（core/building_block_notes.py）——闭环任务 #10 留下的
    "自动抽取的 API 卡片未经人工核实"这句免责声明：用得越多，卡片旁边积累的实践
    证据越多。绝不抛异常、绝不阻断造工具主流程（旁路增强，失败纯粹不记）。"""
    try:
        from core import building_block_notes, skill_policy
        mods = skill_policy.used_building_block_modules(code)
        if not mods:
            return
        for mod in mods:
            building_block_notes.add_note(
                mod,
                f"技能「{name}」（{(description or '')[:60]}）用到了这个模块，"
                f"经隔离冒烟验证可正常 import/加载",
                evidence=f"skill:{name}",
            )
    except Exception:
        pass


async def _author_verified_loop(
    name: str, base_content: str, description: str,
    extra_meta: dict = None, max_attempts: int = None,
) -> tuple[str, dict, bool, str, int]:
    """有界的"生成→静态+一致性校验→隔离子进程 import 冒烟→把【真实报错】喂回重生成"循环。
    只有全部门都过才停；用尽仍不过则返回最后一版 + 未过原因（诚实交付，不假装成功）。
    返回 (code, validation, smoke_ok, smoke_msg, attempts)。"""
    max_attempts = max_attempts or _TOOL_AUTHOR_MAX_ATTEMPTS
    content = base_content
    code, validation, smoke_ok, smoke_msg = "", {"ok": False, "errors": [], "warnings": []}, False, ""
    for attempt in range(1, max_attempts + 1):
        code = await _call_codegen(content)
        tool_path, validation = _write_draft(name, code, description, extra_meta)
        if not validation["ok"]:
            content = base_content + (
                "\n\n上一版没通过静态校验/一致性检查，请修复这些【阻断级错误】后重出完整代码"
                "（尤其：不要调用 building block 上不存在的方法，严格按给你的真实 API）：\n"
                + "\n".join(f"- {e}" for e in validation["errors"])
                + f"\n\n上一版代码：\n{code}"
            )
            continue
        smoke_ok, smoke_msg = smoke_import_skill(tool_path)
        if smoke_ok:
            _write_back_building_block_notes(name, description, code)
            return code, validation, True, "ok", attempt
        content = base_content + (
            f"\n\n上一版能过静态校验，但在隔离子进程 import/加载时失败：{smoke_msg}\n"
            f"请修复后重出完整代码。\n\n上一版代码：\n{code}"
        )
    return code, validation, smoke_ok, smoke_msg, max_attempts


def _authoring_message(name: str, verb: str, validation: dict, smoke_ok: bool,
                       smoke_msg: str, attempts: int) -> str:
    """据循环结果生成给用户的诚实说明。"""
    if validation["ok"] and smoke_ok:
        return (f"工具 {name} 代码已{verb}，并通过静态校验 + 一致性检查 + 隔离冒烟"
                f"（共 {attempts} 轮），等待你审查并激活。")
    if not validation["ok"]:
        problem = "；".join(validation["errors"])
        return (f"工具 {name} 我改了 {attempts} 遍，仍没通过校验：{problem}。"
                f"可能是所需能力缺失或太复杂——你看下代码，或告诉我怎么调整。")
    return (f"工具 {name} 静态校验过了，但隔离冒烟仍失败：{smoke_msg}（已试 {attempts} 轮）。"
            f"很可能是依赖缺失或复用的接口对不上，你看下代码或告诉我怎么调整。")


# ── 注册进主控的元工具 ────────────────────────────────────────────────────────

async def create_tool(name: str, request: str, clarifications: str = "") -> str:
    """
    生成一个新工具并保存为草稿。
    name:           工具名（snake_case，如 stock_price）
    request:        对工具功能的详细描述
    clarifications: 用户对澄清问题的回答（首次调用留空）。一旦非空即跳过澄清门，
                    保证「问一轮就走」，不会反复追问。
    """
    if not is_safe_name(name):
        return f"工具名非法：{name!r}（只允许字母、数字、下划线、连字符，长度 1-64）"
    # 检查是否已存在
    existing = read_skill_code(name)
    if existing:
        return f"工具 {name} 已存在。如需修改，请说「修改 {name} 工具」。"

    # 先查后建闸（core/capability）：与已有能力高度重合时拦下，要求显式说明差异。
    # clarifications 非空（第二趟，已带回答/差异说明）则放行——最多拦一次，不拉锯。
    if not clarifications.strip():
        try:
            from core import capability as _capability
            dup_note = _capability.check_duplicate(request)
        except Exception:
            dup_note = None   # 查重失败绝不阻断造工具
        if dup_note:
            return dup_note

    # 澄清门：只在「猜错会产出静默错误结果」时拦一次。已带 clarifications 则直接放行，
    # 因此最多问一轮，绝不会来回拉锯。
    if not clarifications.strip():
        questions = await _gather_clarifications(name, request)
        if questions:
            qs = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
            return (
                f"在动手写 {name} 之前，有 {len(questions)} 个点需要你确认——"
                f"这几处猜错了不会报错，只会让工具产出看起来正常、实际是错的结果：\n\n"
                f"{qs}\n\n"
                f"（请把用户的回答整理后，用同样的 name 和 request 再调一次 create_tool，"
                f"并把回答放进 clarifications 参数；用户若说「你看着办」，"
                f"就把括号里的默认假设作为回答传进去，不要再问第二轮。）"
            )

    try:
        # 两趟：先（门控地）读现有源码学真实用法/网页结构，再进有界自我修正循环
        # （生成 → 静态+一致性校验 → 隔离冒烟 → 把真实报错喂回改，全部门过才停）。
        ref = await _gather_references(request)
        env = await _gather_env_probe(request)
        mcp_hint = await _gather_mcp_hints(request)
        clar = (f"\n\n【用户对关键问题的确认（优先级高于上面的需求描述，"
                f"如有冲突以此为准）】\n{clarifications.strip()}" if clarifications.strip() else "")
        base = _user_context_text() + env + ref + mcp_hint + f"工具名：{name}\n需求：{request}{clar}"
        code, validation, smoke_ok, smoke_msg, attempts = await _author_verified_loop(name, base, request)
        message = _authoring_message(name, "生成", validation, smoke_ok, smoke_msg, attempts)
        return ToolResult(text=message + "\n\n" + validation_summary(validation), actions=[Action("code_review", {
            "name": name, "code": code, "validation": validation, "message": message,
        })])
    except CodeGenTruncated as e:
        return f"生成工具失败：{e}"
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

    orig_description = (read_skill_meta(name) or {}).get("description", "") or change_request
    try:
        # 两趟：先门控地读参考源码，再进有界自我修正循环
        ref = await _gather_references(f"修改工具 {name}：{change_request}")
        env = await _gather_env_probe(change_request)
        base = (_user_context_text() + env + ref
                + f"以下是现有工具代码：\n\n{existing}\n\n请根据要求修改：{change_request}\n\n"
                  f"只输出完整的新代码，不要说明。")
        extra = {"last_edited_at": datetime.now(timezone.utc).isoformat(), "last_change": change_request}
        new_code, validation, smoke_ok, smoke_msg, attempts = await _author_verified_loop(
            name, base, orig_description, extra_meta=extra)
        message = _authoring_message(name, "修改", validation, smoke_ok, smoke_msg, attempts)
        return ToolResult(text=message + "\n\n" + validation_summary(validation), actions=[Action("code_review", {
            "name": name, "code": new_code, "validation": validation, "message": message,
        })])
    except CodeGenTruncated as e:
        return f"修改工具失败：{e}"
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
    """删除一个工具（代码 + 元数据），并【从活着的注册表里注销】，当前进程立即失效。"""
    import shutil as _shutil
    try:
        d = _skill_dir(name)
    except ValueError as e:
        return False, str(e)
    if not d.exists():
        return False, f"工具 {name} 不存在"
    # 先按记录的实际工具名从运行中的注册表注销（回退目录名），再删文件——
    # 否则进程里仍留着旧注册，"删了重建"也逃不出旧登记。
    tool_name = name
    meta_path = d / "meta.json"
    if meta_path.exists():
        try:
            tool_name = json.loads(meta_path.read_text(encoding="utf-8")).get("tool_name") or name
        except Exception:
            pass
    registry.unregister(tool_name)
    _shutil.rmtree(d)
    # 清点该工具名下的产物（登记表），绝不擅自删——删/留由用户拍板。
    orphan_note = ""
    try:
        from core import artifacts as _artifacts
        items = _artifacts.list_artifacts(producer=f"skill:{name}", limit=1000)
        if items:
            total_mb = sum(a.get("size") or 0 for a in items) / 1e6
            orphan_note = (f"\n注意：它名下还有 {len(items)} 个产物（约 {total_mb:.1f} MB）"
                           f"仍在图书馆/登记表里。请问用户这些产出【删还是留】；"
                           f"删则调 purge_artifacts(producer='skill:{name}')。")
    except Exception:
        pass
    return True, f"工具 {name} 已删除（当前进程已即时注销）{orphan_note}"


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
                "clarifications": {
                    "type": "string",
                    "description": (
                        "用户对澄清问题的回答。【首次调用留空】；若本工具返回了需要确认的问题，"
                        "就去问用户，然后把回答整理进这个参数、用同样的 name/request 再调一次。"
                        "用户说「你看着办」时，把问题里括号内的默认假设作为回答填进来。"
                    ),
                },
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
        "description": "列出所有自建工具及其状态（激活/草稿/停用）。用户问「有哪些工具/待审的工具」时用。",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "read_tool_code",
        "description": (
            "读取某个自建工具的完整源码 + 静态校验结果。"
            "当用户要你看某工具代码、查 bug、讲解实现、或工具报错需要排查时用。"
            "你【能】读自建工具的 py 代码——就用这个工具，不要说自己读不了。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "工具名"}},
            "required": ["name"]
        }
    },
    {
        "name": "review_tool",
        "description": (
            "把某个自建工具的代码 + 校验结果重新调出来供审查（网页重弹审查卡片、飞书重发代码卡片）。"
            "当用户说「再给我看看 X 工具的代码 / 调出 X 的审查 / 刚才那个工具的审查窗口没了」时用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "工具名"}},
            "required": ["name"]
        }
    },
    {
        "name": "update_tool_code",
        "description": (
            "把你写好的【确切代码】原样写入某工具（不让模型改写/重生成）。"
            "当你已经手写好完整 tool.py、只想原样保存时用它——而不是 create_tool/edit_tool"
            "（那两个会让模型重新生成，代码会变样）。写入后是 draft，需再 activate_tool 激活。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "工具名（snake_case）"},
                "code": {"type": "string", "description": "完整的 tool.py 源码（含 async 主函数 + TOOL_DEF）"},
            },
            "required": ["name", "code"]
        }
    },
    {
        "name": "activate_tool",
        "description": (
            "激活一个草稿（draft）工具使其立即生效（等价于网页里的「激活」按钮，飞书对话里也能用）。"
            "当用户审查完代码后说「激活 X / 这个可以用了 / 启用 X 工具」时用。"
            "激活前会自动重新静态校验，不过关会拒绝。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "要激活的工具名"}},
            "required": ["name"]
        }
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


async def _handle_create_tool(name: str, request: str, clarifications: str = "") -> str:
    return await create_tool(name=name, request=request, clarifications=clarifications)

async def _handle_edit_tool(name: str, change_request: str) -> str:
    return await edit_tool(name=name, change_request=change_request)

async def _handle_list_tools_meta() -> str:
    return await list_tools_meta()

async def _handle_read_tool_code(name: str) -> str:
    """读取某个自建工具的代码 + 校验结果，直接返回给模型看（用于查 bug / 讲解）。"""
    if not is_safe_name(name):
        return f"工具名非法：{name!r}"
    code = read_skill_code(name)
    if code is None:
        return f"工具 {name} 不存在（skills/{name}/tool.py 找不到）。"
    meta = read_skill_meta(name) or {}
    v = meta.get("validation") or validate_tool_code(code)
    status = meta.get("status", "unknown")
    return (
        f"【工具 {name}】状态：{status}\n{validation_summary(v)}\n\n"
        f"```python\n{code}\n```"
    )

async def _handle_activate_tool(name: str) -> str:
    """通过对话激活一个 draft 工具（不再依赖网页按钮，飞书也能用）。
    activate_skill 内部会激活前重新静态校验，不过关会拒绝。"""
    if not is_safe_name(name):
        return f"工具名非法：{name!r}"
    ok, msg = activate_skill(name)
    return ("✅ " if ok else "❌ ") + msg

async def _handle_update_tool_code(name: str, code: str) -> str:
    """把【确切代码】原样写入某工具（不经模型改写/重生成）。用于"我已写好完整
    tool.py、只想原样保存"的场景——避免 create_tool/edit_tool 让模型重写成别的样子。"""
    if not is_safe_name(name):
        return f"工具名非法：{name!r}"
    code = _strip_markdown_fence((code or "").strip())
    if not code:
        return "没有收到代码内容。"
    validation = validate_tool_code(code)
    d = _skill_dir(name)
    d.mkdir(exist_ok=True)
    (d / "tool.py").write_text(code, encoding="utf-8")
    # 保留原 description / created_at（若有）
    meta_path = d / "meta.json"
    orig = read_skill_meta(name) or {}
    meta = {
        "status": "draft",
        "description": orig.get("description", "") or "（手动写入的代码）",
        "created_at": orig.get("created_at", datetime.now(timezone.utc).isoformat()),
        "last_edited_at": datetime.now(timezone.utc).isoformat(),
        "last_change": "manual update_tool_code",
        "validation": validation,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    message = f"工具 {name} 的代码已按你给的原样写入（未经模型改写），审查后回复「激活 {name}」生效。"
    return ToolResult(text=message + "\n\n" + validation_summary(validation), actions=[Action("code_review", {
        "name": name, "code": code, "validation": validation, "message": message,
    })])

async def _handle_review_tool(name: str) -> str:
    """随时把某个工具的代码 + 校验结果重新调出来审查（网页重弹审查卡片，
    飞书重发代码卡片）。解决"审查窗口滚走后再也调不出来"。"""
    if not is_safe_name(name):
        return f"工具名非法：{name!r}"
    code = read_skill_code(name)
    if code is None:
        return f"工具 {name} 不存在，无法审查。"
    meta = read_skill_meta(name) or {}
    v = meta.get("validation") or validate_tool_code(code)
    status = meta.get("status", "unknown")
    message = f"工具 {name}（当前状态：{status}）的代码如下，审查后可回复「激活 {name}」使其生效。"
    return ToolResult(
        text=message + "\n\n" + validation_summary(v),
        actions=[Action("code_review", {
            "name": name, "code": code, "validation": v, "message": message,
        })],
    )

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
    "read_tool_code":   _handle_read_tool_code,
    "review_tool":      _handle_review_tool,
    "update_tool_code": _handle_update_tool_code,
    "activate_tool":    _handle_activate_tool,
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
    "read_tool_code": "authoring", "review_tool": "authoring", "activate_tool": "authoring",
    "update_tool_code": "authoring",
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
