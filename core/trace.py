"""
core/trace.py — 统一 trace_id（任务 #17）

背景：telemetry（工具调用记录）、workflow（多步流水线）、self_iteration/
self_review（反思周期）各自独立记录/落盘，彼此之间只能靠时间戳模糊对齐——
想回答"这次 signal_collection 工作流跑失败，是不是跟同一批里 web_search 连续
超时有关"，此前只能翻时间戳对着猜。三个系统各自加时间戳已经够用来"看当下"，
但不够用来"事后串联"。

设计：不是给每张表加外键、不是重构三个系统的调用签名——用一个轻量、贯穿调用栈
的 contextvar：谁在最外层（workflow 一次 run / self_iteration 一次 cycle）铸一个
trace_id 并"戴上"，中间嵌套多深都不用手动传参，telemetry.record() 等落盘点自动
读到当前 trace_id 落进去。没人戴 trace 时（如日常交互对话里的普通工具调用）
trace_id 就是空串——零行为影响，纯增量能力。

用法：
    with core.trace.scope(core.trace.new_id("wf")):
        await run_workflow(...)   # 这条 run 里所有 telemetry.record 都带上这个 trace_id

    tid = core.trace.new_id("review")
    with core.trace.scope(tid):
        result = await run_cycle(...)
"""
from __future__ import annotations

import contextvars
import random
import string
from contextlib import contextmanager
from datetime import datetime, timezone

_current: contextvars.ContextVar[str] = contextvars.ContextVar("jarvis_trace_id", default="")


def new_id(prefix: str = "") -> str:
    """铸一个新 trace_id：<prefix_>YYYYMMDDTHHMMSS_<4位随机后缀>（人可读、可排序、够唯一）。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{prefix}_{ts}_{suffix}" if prefix else f"{ts}_{suffix}"


def get() -> str:
    """当前 trace_id；没人 scope() 过则为空串（零行为影响的默认态）。"""
    return _current.get()


def set_current(trace_id: str):
    """底层 API：直接设置（返回 contextvars.Token，供手动 reset）。一般用 scope() 更安全。"""
    return _current.set(trace_id or "")


@contextmanager
def scope(trace_id: str):
    """在这个 with 块内（含所有嵌套 await 调用），core.trace.get() 都返回 trace_id；
    退出时自动恢复外层原值（支持嵌套：内层 scope 不会污染外层）。"""
    token = _current.set(trace_id or "")
    try:
        yield trace_id
    finally:
        _current.reset(token)
