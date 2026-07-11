"""
输入边界安全工具

把不可信的名字 / 文件名限定在安全语境内，防止路径穿越（../、绝对路径）等
"数据被当成语法"的问题。所有"外部/用户/模型传入的字符串将用于文件路径"的地方，
都应先经过这里。
"""

import re
from pathlib import Path

# 技能名 / 任务名：只允许字母、数字、下划线、连字符，长度 1-64
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def is_safe_name(name) -> bool:
    return isinstance(name, str) and _NAME_RE.fullmatch(name) is not None


def safe_name(name: str) -> str:
    """校验技能名/任务名；非法则抛 ValueError。"""
    if not is_safe_name(name):
        raise ValueError(f"非法名称：{name!r}（只允许字母、数字、下划线、连字符，长度 1-64）")
    return name


def safe_filename(filename: str, fallback: str = "file") -> str:
    """
    清洗上传文件名：去掉任何目录/盘符部分，过滤危险字符，保留扩展名。
    允许中文。结果绝不含路径分隔符或 ..。
    """
    base = Path(str(filename or "")).name          # 去掉目录与 Windows 盘符
    base = base.replace("\x00", "").strip()
    # 只保留：字母、数字、点、下划线、连字符、中文
    cleaned = re.sub(r"[^A-Za-z0-9._一-龥-]", "_", base).strip("._")
    # 杜绝纯点名（. / ..）
    if not cleaned or set(cleaned) <= {"."}:
        return fallback
    return cleaned


def under_base(base: Path, *parts: str) -> Path:
    """
    把 parts 拼到 base 下并 resolve；若结果跑到 base 之外则抛 ValueError。
    作为路径拼接的兜底校验（即使名字校验被绕过也挡得住）。
    """
    base_r = Path(base).resolve()
    target = base_r.joinpath(*parts).resolve()
    if target != base_r and base_r not in target.parents:
        raise ValueError("路径越界")
    return target
