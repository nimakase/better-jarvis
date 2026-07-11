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


def import_rules_text() -> str:
    """把导入策略渲染成喂给模型的提示词条目（与校验器同源）。"""
    allowed = ", ".join(sorted(ALLOWED_IMPORTS))
    blocked = ", ".join(sorted(BLOCKED_IMPORTS))
    return (
        f"2. 只能 import 以下库（标准库也仅限这些，其余一律不要用）：{allowed}\n"
        f"3. 禁止 import（会被校验器阻断、无法激活）：{blocked}"
    )
