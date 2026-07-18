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
#
# ── 每个 building block 可声明的字段（2026-07-18 扩充）──────────────────────────
#   desc            模块一句话说明
#   symbols         要注入签名的类 / 模块级函数 / 异常类名
#   constants       要注入取值的模块级常量（选择器、列名等——技能常常必须知道它们）
#   expose_private  {类名: [私有方法名]} —— 显式声明「虽以 _ 开头，但就是给技能复用的」
#   attrs           {类名: [实例属性名]} —— 构造后可直接读写的属性
#   notes           自然语言语义说明：签名说不清、但用错就出错的约定
#   recipes         真实可抄的最小用法范例（比任何签名列表都有效）
#
# 为什么需要后四项：签名只回答「有什么」，回答不了「怎么用才对」。实测教训——
# `HubSpotBrowser.column_index_map` 的值是 HTML 的 `data-column-index` 属性值，
# 不是 td 的位置下标；只给签名，模型会写出 `cells.nth(idx)` 这种能跑通、能过校验、
# 结果却整列错位的代码。语义和范例必须一起注入。
BUILDING_BLOCKS: dict[str, dict] = {
    "prospecting.login_manager": {
        "desc": "HubSpot 登录会话管理（检查/维持登录，避免重复弹浏览器）",
        "symbols": ["LoginManager"],
        "notes": [
            "LoginManager 是单例，用 `LoginManager.get()` 取，不要自己 `LoginManager()`。",
            "`check_session()` 返回 'ok' / 'expired' / 'unknown' / 'login_in_progress' 之一。",
            "`start_login()` 会在【运行 jarvis 的那台机器】弹出有头 Chrome，是异步的：它立刻返回，"
            "真正登录在后台线程里进行，要靠轮询 `get_status()` 等它变 'ok'。",
        ],
    },
    "prospecting.hubspot_worker": {
        "desc": "HubSpot 浏览器自动化（驱动 HubSpot 网页）",
        "symbols": [
            "HubSpotBrowser",
            "resolve_paths", "ensure_directories", "setup_logger",
            "SessionExpiredError", "FileProcessFatalError",
            "normalize_spaces", "build_company_match_key", "build_domain_match_key",
        ],
        "constants": [
            "HUBSPOT_TABLE_SELECTOR", "HUBSPOT_ROW_SELECTOR", "HUBSPOT_HEADER_SELECTOR",
            "NAME_COL_LABEL", "OWNER_COL_LABEL", "DOMAIN_COL_LABEL",
            "STEALTH_CHROMIUM_ARGS", "MANUAL_LOGIN_WAIT_SECONDS",
        ],
        "expose_private": {
            # 这几个下划线方法就是给复用者用的：它们封装了 HubSpot 单元格的嵌套 DOM 结构，
            # 绕过它们自己写 inner_text 一定抓到脏数据（owner 会带邮箱、name 会串行）。
            "HubSpotBrowser": ["_extract_text", "_extract_name", "_extract_owner", "_column_idx",
                               "_collect_column_index_map", "_label_key"],
        },
        "attrs": {
            "HubSpotBrowser": ["page", "context", "playwright", "column_index_map", "run_mode", "paths"],
        },
        "notes": [
            "【列索引】`column_index_map` 是 {规范化列名: data-column-index 属性值}。它的值是 HTML "
            "属性值，**不是 td 的位置下标**。取单元格【只能】用属性选择器 "
            "`row.locator(f\"td[data-column-index='{idx}']\").first`；写成 `cells.nth(int(idx))` "
            "会整列错位（表格前面还有 checkbox 等无 data-column-index 的列）。",
            "【取列索引】用 `browser._column_idx(NAME_COL_LABEL)`，不要自己遍历 column_index_map "
            "去猜列名——列名规范化规则（小写+标点转空格）在 `_label_key` 里，自己写会对不上。",
            "【取文本】name 用 `_extract_name(cell)`、owner 用 `_extract_owner(cell)`、其余用 "
            "`_extract_text(cell)`。不要直接 `cell.inner_text()`。",
            "【硬依赖】`refresh_column_index_map()` 内部要求 name / owner / domain 三列**同时存在**，"
            "缺任一列会抛 FileProcessFatalError。若你的视图不一定有 domain 列，不要调它——"
            "改用 `_collect_column_index_map()` 自己拿完整映射，或捕获异常后降级。",
            "【登录】不要用 `input()` 等用户敲回车（技能跑在服务进程里，stdin 不可用）。"
            "用 `login_bootstrap()`（有头窗口 + 内部轮询等待，上限 MANUAL_LOGIN_WAIT_SECONDS 秒），"
            "或先 `start(run_mode='background')` 捕获 SessionExpiredError 再决定。",
            "【生命周期】`browser.stop()` 会一并关掉 page / context / playwright。自己 "
            "`sync_playwright().start()` 的话，务必在 finally 里把 context 也 close 掉，"
            "否则 persistent profile 会留锁，下次启动报 profile in use。",
            "【同步 API】本模块用的是 playwright **sync** API，不能在 async 函数里直接调；"
            "把整段浏览器操作包成一个同步函数，再用 `loop.run_in_executor(None, fn, ...)` 调。",
            "【分页】`rows()` 只返回**当前已渲染**的行。HubSpot 视图默认分页，想要全量必须自己翻页；"
            "只抓一页却宣称抓了全部，属于静默数据截断，必须避免。",
            "【profile 目录 · 必读】登录态存在浏览器 profile 里，而 profile 路径由 "
            "`resolve_paths(base_dir)` 的 base_dir 决定。app 用的是 "
            "`Path(os.environ['JARVIS_DATA_DIR']) / 'hubspot'`——你**必须**用同一个，"
            "否则会另建一份 profile，表现为「明明登录过却还要再登一次」，且在仓库里留下垃圾目录。"
            "写 `resolve_paths(Path(__file__).parent.parent.parent)` 是错的。",
        ],
        "recipes": [
            (
                "遍历视图当前页、正确取出 name/owner",
                "name_idx = browser._column_idx(NAME_COL_LABEL)\n"
                "owner_idx = browser._column_idx(OWNER_COL_LABEL)\n"
                "rows = browser.rows()\n"
                "for i in range(rows.count()):\n"
                "    row = rows.nth(i)\n"
                "    name_cell = row.locator(f\"td[data-column-index='{name_idx}']\").first\n"
                "    owner_cell = row.locator(f\"td[data-column-index='{owner_idx}']\").first\n"
                "    if name_cell.count() == 0:\n"
                "        continue\n"
                "    name = browser._extract_name(name_cell)\n"
                "    owner = browser._extract_owner(owner_cell)"
            ),
            (
                "在服务进程里安全地拿到一个已登录的 browser（不用 input()）",
                "browser = HubSpotBrowser(paths, logger)\n"
                "try:\n"
                "    browser.start(run_mode='background')      # 已登录则直接就绪\n"
                "except SessionExpiredError:\n"
                "    browser.stop()\n"
                "    browser = HubSpotBrowser(paths, logger)\n"
                "    browser.login_bootstrap()                 # 弹有头窗口 + 内部轮询等待登录"
            ),
        ],
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
    """用 AST 从 building block 源码抽取【真实 API 面】，渲染成一段权威参考喂给生成器。

    注入四类信息，缺一不可（2026-07-18 从「只注入公共签名」扩充而来）：
      1. 签名   —— 类方法 / 模块级函数 / 异常，含 expose_private 显式声明的下划线方法
      2. 常量   —— 选择器、列名等，技能不知道就只能硬编码或猜
      3. 属性   —— 构造后可直接读写的实例属性
      4. 语义   —— notes（用错就出错的约定）+ recipes（可直接抄的最小范例）

    只注入 1 会产出「调用存在的方法、但用法是错的」代码：它能过静态校验、能过子进程
    冒烟、真跑起来也不报错，只是结果悄悄是错的。这类 bug 最贵，所以 2~4 必须一起给。
    """
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

    def const_repr(node) -> str:
        """把常量赋值渲染成可读取值；取不到字面量就退回省略号。"""
        try:
            return repr(ast.literal_eval(node))
        except Exception:
            try:
                return ast.unparse(node)[:120]
            except Exception:
                return "..."

    blocks = []
    for mod, meta in BUILDING_BLOCKS.items():
        path = repo_root / (mod.replace(".", "/") + ".py")
        symbols = set(meta.get("symbols") or [])
        constants = list(meta.get("constants") or [])
        expose_private = meta.get("expose_private") or {}
        attrs = meta.get("attrs") or {}
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        body = []
        found_consts = {}
        for node in tree.body:
            # ① 常量取值
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in constants:
                        found_consts[t.id] = const_repr(node.value)
                continue
            if getattr(node, "name", None) not in symbols:
                continue
            # ② 类：公共方法 + 显式暴露的私有方法 + 实例属性
            if isinstance(node, ast.ClassDef):
                is_exc = any(isinstance(b, ast.Name) and b.id.endswith(("Error", "Exception"))
                             for b in node.bases)
                bases = ", ".join(getattr(b, "id", "") for b in node.bases if getattr(b, "id", ""))
                body.append(f"class {node.name}({bases}):" if bases else f"class {node.name}:")
                doc = first_doc(node)
                if doc:
                    body.append(f"    # {doc}")
                if is_exc:
                    continue
                allowed_private = set(expose_private.get(node.name) or [])
                for m in node.body:
                    if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    public = not m.name.startswith("_") or m.name == "__init__"
                    if not (public or m.name in allowed_private):
                        continue
                    kw = "async def" if isinstance(m, ast.AsyncFunctionDef) else "def"
                    mark = "   # ← 私有但【就是给你复用的】，请用它" if m.name in allowed_private else ""
                    body.append(f"    {kw} {m.name}({fmt_args(m.args)}){mark}")
                for a in attrs.get(node.name) or []:
                    body.append(f"    # 实例属性: self.{a}")
            # ③ 模块级函数
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                body.append(f"{kw} {node.name}({fmt_args(node.args)})")
                doc = first_doc(node)
                if doc:
                    body.append(f"    # {doc}")

        if found_consts:
            body.append("")
            body.append("# 模块级常量（真实取值，直接 import 用，别自己硬编码）：")
            for k in constants:
                if k in found_consts:
                    body.append(f"{k} = {found_consts[k]}")

        section = f"# {mod} — {meta.get('desc', '')}\n" + "\n".join(body) if body else ""

        # ④ 语义说明 + 可抄范例
        notes = meta.get("notes") or []
        if notes:
            section += f"\n\n## {mod} 的【使用约定】（签名说不清、但用错就出错的部分）：\n"
            section += "\n".join(f"- {n}" for n in notes)
        recipes = meta.get("recipes") or []
        if recipes:
            section += f"\n\n## {mod} 的【正确用法范例】（照抄这个骨架，别自创）：\n"
            for title, snippet in recipes:
                section += f"\n# {title}\n{snippet}\n"
        if section:
            blocks.append(section)
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


def check_runtime_contract(code: str) -> list[str]:
    """静态检查：技能是否违反【运行环境契约】——即它假设了一个自己并不身处的环境。

    技能跑在贾维斯服务进程里、由模型在对话中调用，没有终端。`input()` 这类调用能过语法、
    能过 import 冒烟（冒烟不执行 handler），只有真被调用时才表现为"工具卡住不返回"，
    且用户在网页/飞书里连提示都看不到——排查成本极高。所以在这里做成【阻断级】。

    只查确定性的环境错误，不做风格判断，避免误报。
    """
    import ast
    try:
        tree = ast.parse(code)
    except Exception:
        return []   # 语法错误另有报错

    banned = {
        "input": "从 stdin 读取。技能运行在服务进程里，没有终端——这会 EOFError 或永久挂起，"
                 "且用户在网页/飞书里看不到任何提示。要用户做事请改为 return 一句说明，"
                 "或调用 building block 里内部轮询等待的方法。",
        "raw_input": "同 input()，且是 Python 2 遗留写法。",
        "getpass": "从终端读密码。技能没有终端，请改从 os.environ 取。",
        "breakpoint": "会挂起进程等调试器接入。",
    }
    errors, seen = [], set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        fname = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
        if fname in banned and fname not in seen:
            seen.add(fname)
            errors.append(f"禁止调用 `{fname}()`：{banned[fname]}")
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
