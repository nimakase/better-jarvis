"""
自建技能（skills/）的导入策略 —— 单一事实来源

校验器（validate_tool_code）与代码生成提示词（CODE_GEN_PROMPT）都从这里取，
不再各写一份，因此结构上不可能再漂移：改这里一处，两端同时生效。

注意：这只约束运行时 AI 自建的、不可信的技能代码；第一方工具（core/connectors）
不受此限制。
"""

# 允许导入的模块白名单（标准库安全子集 + 已安装第三方包）。
# 不在此列表里的模块只会触发【警告】（不阻断），但提示词会告知模型只用这些。
ALLOWED_IMPORTS = {
    # 标准库安全子集
    "json", "datetime", "pathlib", "typing", "math", "random", "time",
    "hashlib", "base64", "csv", "io", "copy", "re", "collections",
    "itertools", "functools", "dataclasses", "enum", "abc", "string",
    "urllib.parse", "html", "decimal", "fractions", "statistics",
    "sqlite3", "uuid", "logging", "asyncio", "tempfile", "zipfile",
    "glob", "secrets", "textwrap", "calendar",
    "os", "sys",   # 允许但谨慎（校验器仍会就 os/sys 给出提醒）
    # 已安装第三方包
    "httpx", "openai", "requests",
    "pdfplumber", "docx", "openpyxl", "pptx",
}

# 禁止导入（阻断级别：含此类 import 的技能无法激活）
BLOCKED_IMPORTS = {
    "core", "connectors", "main", "config",     # 内部模块
    "subprocess", "multiprocessing", "signal",  # 进程/系统控制
    "socket", "ssl", "asynchat", "asyncore",    # 低级网络
    "ctypes", "cffi", "mmap",                    # 原生代码
    "importlib", "pkgutil",                      # 动态导入
    "pickle", "shelve", "marshal",              # 不安全序列化
    "pty", "tty", "termios", "fcntl",           # 终端控制
}

# 可疑模式（警告级别，不阻断；文本层面捕获 AST 难以覆盖的动态拼接等）
WARNING_PATTERNS = [
    ("os.system",    "可执行系统命令"),
    ("os.popen",     "可执行系统命令"),
    ("os.remove",    "可删除文件"),
    ("os.rmdir",     "可删除目录"),
    ("shutil.rmtree", "可递归删除目录"),
    ("sys.path",     "可修改模块搜索路径"),
    ("sys.modules",  "可修改已加载模块"),
    ("__import__",   "动态导入，可绕过白名单"),
    ("open(",        "直接读写文件系统"),
]


# ── 可复用的第一方 building block（允许技能 import；真实 API 注入生成提示词）──────
#
# 自建技能默认只能用标准库白名单 + 已装第三方包，禁止 import 内部模块。但有些第一方
# 能力（如 HubSpot 浏览器自动化）本就是给技能复用的共享 building block。这里显式列出
# 【允许复用】的模块 + 关键符号；校验器放行它们的 import，生成器则会拿到它们的【真实
# 签名】（见 building_blocks_api_text），从而写对复用代码、不再臆造不存在的方法。
#
# 注意：列在这里 = 授予技能调用它的能力。只列确需被技能复用、且激活前会人工审代码的模块。
BUILDING_BLOCKS: dict[str, dict] = {
    "prospecting.login_manager": {
        "desc": "HubSpot 登录会话管理（检查/维持登录，避免重复弹浏览器）",
        "symbols": ["LoginManager"],
    },
    "prospecting.hubspot_worker": {
        "desc": "HubSpot 浏览器自动化（驱动 HubSpot 网页）",
        "symbols": ["HubSpotBrowser"],
    },
}


def is_reusable_import(name: str) -> bool:
    """该 import 名是否属于允许复用的第一方 building block。"""
    if not name:
        return False
    for mod in BUILDING_BLOCKS:
        if name == mod or name.startswith(mod + "."):
            return True
    return False


def building_blocks_api_text() -> str:
    """用 AST 从 building block 源码抽取【真实公共签名】（不含函数体），渲染成一段
    权威 API 参考喂给生成器。让单次生成也能照真实接口写对复用代码。"""
    import ast
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent

    def fmt_args(a: "ast.arguments") -> str:
        parts = [p.arg for p in (list(getattr(a, "posonlyargs", [])) + list(a.args))]
        if a.vararg:
            parts.append("*" + a.vararg.arg)
        parts += [k.arg for k in a.kwonlyargs]
        if a.kwarg:
            parts.append("**" + a.kwarg.arg)
        return ", ".join(parts)

    def first_doc(node) -> str:
        d = (ast.get_docstring(node) or "").strip()
        return d.split("\n", 1)[0][:80]

    blocks = []
    for mod, meta in BUILDING_BLOCKS.items():
        path = repo_root / (mod.replace(".", "/") + ".py")
        symbols = set(meta.get("symbols") or [])
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        body = []
        for node in tree.body:
            if getattr(node, "name", None) not in symbols:
                continue
            if isinstance(node, ast.ClassDef):
                body.append(f"class {node.name}:")
                doc = first_doc(node)
                if doc:
                    body.append(f"    # {doc}")
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                            not m.name.startswith("_") or m.name == "__init__"):
                        kw = "async def" if isinstance(m, ast.AsyncFunctionDef) else "def"
                        body.append(f"    {kw} {m.name}({fmt_args(m.args)})")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                body.append(f"{kw} {node.name}({fmt_args(node.args)})")
                doc = first_doc(node)
                if doc:
                    body.append(f"    # {doc}")
        if body:
            blocks.append(f"# {mod} — {meta.get('desc', '')}\n" + "\n".join(body))
    return "\n\n".join(blocks) if blocks else "（当前无可复用 building block）"


def _bb_class_apis() -> dict:
    """从 building block 源码抽取每个类的【真实可用属性集】(方法 + self.x 实例属性 +
    类级属性)，供一致性检查用。返回 {类名: {"attrs": set, "dynamic": bool}}。
    dynamic=True(类定义了 __getattr__ 等) → 该类跳过检查(属性是动态的)。"""
    import ast
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    out: dict = {}
    for mod, meta in BUILDING_BLOCKS.items():
        path = repo / (mod.replace(".", "/") + ".py")
        symbols = set(meta.get("symbols") or [])
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in tree.body:
            if not (isinstance(node, ast.ClassDef) and node.name in symbols):
                continue
            attrs: set = set()
            dynamic = False
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    attrs.add(m.name)
                    if m.name in ("__getattr__", "__getattribute__"):
                        dynamic = True
                    for sub in ast.walk(m):
                        tgts = []
                        if isinstance(sub, ast.Assign):
                            tgts = sub.targets
                        elif isinstance(sub, ast.AnnAssign):
                            tgts = [sub.target]
                        for t in tgts:
                            if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                                    and t.value.id == "self"):
                                attrs.add(t.attr)
                elif isinstance(m, ast.Assign):
                    for t in m.targets:
                        if isinstance(t, ast.Name):
                            attrs.add(t.id)
                elif isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name):
                    attrs.add(m.target.id)
            out[node.name] = {"attrs": attrs, "dynamic": dynamic}
    return out


def check_building_block_usage(code: str) -> list[str]:
    """静态检查：技能是否在复用的 building block 上调用了【不存在的方法/属性】(臆造)。
    只对"直接 import 的 building block 类"和"由其构造器直接产生的变量"下判断，保守设计
    以避免误报；命中即返回阻断级错误(附真实可用 API)。这是拦截"import 对了但方法是编的、
    静态过校验、真跑才崩"这类幻觉的关键。"""
    import ast
    try:
        tree = ast.parse(code)
    except Exception:
        return []   # 语法错误另有报错
    apis = _bb_class_apis()
    if not apis:
        return []
    bb_classes = set(apis)

    # 1) 本文件 import 进来的 building block 类名(含别名)
    imported: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and is_reusable_import(node.module):
            for a in node.names:
                if a.name in bb_classes:
                    imported[a.asname or a.name] = a.name
    if not imported:
        return []

    # 2) var = ClassName(...) → var 是该类实例
    var_class: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            f = node.value.func
            if isinstance(f, ast.Name) and f.id in imported:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        var_class[t.id] = imported[f.id]

    # 3) 扫所有 `base.attr`：base 是 building block 类名(ClassName.attr)或其实例(var.attr)
    errors: list = []
    seen: set = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            continue
        base = node.value.id
        cls = imported.get(base) or var_class.get(base)
        if not cls or cls not in apis or apis[cls]["dynamic"]:
            continue
        if node.attr not in apis[cls]["attrs"] and (base, node.attr) not in seen:
            seen.add((base, node.attr))
            real = ", ".join(sorted(a for a in apis[cls]["attrs"] if not a.startswith("_"))) or "(无)"
            errors.append(
                f"`{cls}` 没有 `{node.attr}` 这个方法/属性（疑似臆造，会在运行时 AttributeError）。"
                f"真实可用：{real}"
            )
    return errors


def import_rules_text() -> str:
    """把导入策略渲染成喂给模型的提示词条目（与校验器同源）。"""
    allowed = ", ".join(sorted(ALLOWED_IMPORTS))
    blocked = ", ".join(sorted(BLOCKED_IMPORTS))
    bb = ", ".join(BUILDING_BLOCKS.keys())
    return (
        f"2. 只能 import 以下库（标准库也仅限这些，其余一律不要用）：{allowed}\n"
        f"   另外【允许复用】这些第一方 building block（务必严格按下方【可复用 building block 的真实 API】"
        f"给出的签名调用，绝不臆造别的方法/类）：{bb}\n"
        f"3. 禁止 import（会被校验器阻断、无法激活）：{blocked}"
    )
