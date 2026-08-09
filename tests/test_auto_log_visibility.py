#!/usr/bin/env python3
"""系统日志明文可查——自动生成，绝不覆盖既有测试文件。

背景：用户反馈贾维斯偶尔静默失败，想要"系统日志明文显示出来，这样不管是
我（Claude）还是贾维斯自己都能回头查问题"。排查发现：logs/jarvis.log 这份
明文滚动日志基础设施本来就有（main.py._setup_logging），飞书通道
（lark_bridge.py）出异常也早就用 logger.exception 记了完整堆栈——但网页
这条通道（web/chat.py）的对话处理异常从来没落过日志，只发了一条 error 消息
给前端，出了问题回头翻日志根本找不到。这是这次要补的第一个缺口。

第二个缺口：即便没有异常、只是模型交了白卷（DeepSeek V4 那个已知问题，见
上一次修复），也应该留痕方便统计是不是系统性问题，而不是只在传输层一闪而过。

第三，贾维斯自己也该有能力回头翻自己的日志（不止是读源码），加了
connectors/self_inspect.read_recent_logs 这个只读工具。

覆盖：
  1. web/chat.py 对话处理异常必须落 logger.exception（含完整 traceback）。
  2. core/controller.py 交白卷/兜底也失败这两种情况分别落 WARNING/ERROR。
  3. connectors/self_inspect.read_recent_logs：行数/level/keyword 过滤、
     文件不存在时的兜底提示、行数上限。
"""
import asyncio
import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _v in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
           "HTTP_PROXY", "http_proxy"):
    os.environ.pop(_v, None)

import config  # noqa: E402
config.PROGRESSIVE_TOOLS = False
if not getattr(config, "OPENROUTER_API_KEY", None):
    config.OPENROUTER_API_KEY = "test-key"

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. web/chat.py 接线检查：异常必须落日志，不能只发前端 ─────────────────────
print("[1] web/chat.py 对话处理异常落日志")
from web import chat as web_chat  # noqa: E402

check(hasattr(web_chat, "logger") and web_chat.logger.name == "jarvis.web",
      "模块级 logger 已建立，名字是 jarvis.web（方便按名字过滤日志）")

src = inspect.getsource(web_chat.websocket_chat)
check("logger.exception" in src, "websocket_chat 的异常分支里调用了 logger.exception（带完整堆栈）")
# 确认调用顺序在发给前端之前（先落盘再告知用户，即便发送本身又失败也不丢日志）
idx_log = src.find("logger.exception")
idx_send = src.find('"type": "error"')
check(0 <= idx_log < idx_send, "落日志发生在发 error 消息给前端之前")


# ── 2. core/controller.py：交白卷/兜底失败分别落 WARNING/ERROR ────────────────
print("[2] controller.py 空补全场景落日志")
from core import controller as ctl_mod  # noqa: E402


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta, finish):
        self.delta = delta
        self.finish_reason = finish


class _Chunk:
    def __init__(self, choice):
        self.choices = [choice]


class _FakeResp:
    def __init__(self, content):
        self.choices = [type("Ch", (), {"message": type("M", (), {"content": content})()})()]


class _FakeStreamClient:
    """第一次调用（stream=True）返回空补全；非流式调用（兜底）返回 fallback_text。"""

    def __init__(self, fallback_text=""):
        self.fallback_text = fallback_text
        self.chat = type("C", (), {})()
        self.chat.completions = type("CC", (), {"create": self._create})()

    async def _create(self, **kw):
        if kw.get("stream"):
            async def _gen():
                yield _Chunk(_Choice(_Delta(content=None), "stop"))
            return _gen()
        return _FakeResp(self.fallback_text)


class _CapturingLogger:
    def __init__(self, store):
        self._store = store

    def warning(self, msg, *args):
        self._store.append(("WARNING", (msg % args) if args else msg))

    def error(self, msg, *args):
        self._store.append(("ERROR", (msg % args) if args else msg))

    def info(self, *a, **kw):
        pass

    def exception(self, *a, **kw):
        pass


class _FakeLoggingModule:
    def __init__(self, store):
        self._store = store

    def getLogger(self, name):
        return _CapturingLogger(self._store)


async def _t_controller_logs():
    orig_logging = ctl_mod.logging
    store = []
    ctl_mod.logging = _FakeLoggingModule(store)
    try:
        # 场景 A：交白卷，但兜底成功 → 应该只有 WARNING，没有 ERROR
        ctl = ctl_mod.JarvisController(interactive=False)
        ctl.client = _FakeStreamClient(fallback_text="补答成功")
        async for _ in ctl.chat("你好"):
            pass
        levels_a = [lvl for lvl, _ in store]
        check("WARNING" in levels_a, "交白卷时落了 WARNING（不管兜底成不成功都该留痕）")
        check("ERROR" not in levels_a, "兜底成功时不该多报一条 ERROR")

        # 场景 B：交白卷，兜底也失败 → 应该额外有 ERROR
        store.clear()
        ctl2 = ctl_mod.JarvisController(interactive=False)
        ctl2.client = _FakeStreamClient(fallback_text="")
        async for _ in ctl2.chat("你好"):
            pass
        levels_b = [lvl for lvl, _ in store]
        check("WARNING" in levels_b and "ERROR" in levels_b,
              f"兜底也失败时 WARNING+ERROR 都要有（实际 {levels_b}）")
    finally:
        ctl_mod.logging = orig_logging

asyncio.run(_t_controller_logs())


# ── 3. self_inspect.read_recent_logs ─────────────────────────────────────────
print("[3] read_recent_logs")
import tempfile  # noqa: E402
import connectors.self_inspect as si  # noqa: E402

orig_log_path = si._LOG_PATH


async def _t_read_logs():
    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "jarvis.log"
        lines_content = [
            "2026-08-08 10:00:00 | INFO | jarvis.web | 已连接",
            "2026-08-08 10:00:05 | WARNING | jarvis.controller | 本轮补全为空（model=deepseek-v4-flash）",
            "2026-08-08 10:00:06 | ERROR | jarvis.controller | 本轮补全为空且兜底总结也失败",
            "2026-08-08 10:00:10 | INFO | jarvis.lark | [飞书] 收到消息",
        ]
        log_path.write_text("\n".join(lines_content) + "\n", encoding="utf-8")
        si._LOG_PATH = log_path

        out_all = await si.read_recent_logs()
        check("已连接" in out_all and "收到消息" in out_all, "默认不过滤 → 全部行都在")

        out_level = await si.read_recent_logs(level="ERROR")
        check("兜底总结也失败" in out_level and "已连接" not in out_level,
              "level=ERROR 只留 ERROR 那一行")

        out_kw = await si.read_recent_logs(keyword="controller")
        check("本轮补全为空" in out_kw and "本轮补全为空且兜底" in out_kw and "已连接" not in out_kw,
              "keyword 按子串过滤，大小写不敏感")

        out_lines = await si.read_recent_logs(lines=2)
        check("最近 2 行" in out_lines and "已连接" not in out_lines and "收到消息" in out_lines,
              "lines 参数生效，只保留最近 N 行")

        out_nohit = await si.read_recent_logs(keyword="不存在的关键词zzz")
        check("没有匹配" in out_nohit, "过滤后无匹配 → 明确说明，不是空字符串")

        si._LOG_PATH = Path(td) / "not_exist.log"
        out_missing = await si.read_recent_logs()
        check("不存在" in out_missing, "日志文件不存在时给出明确提示，不报错崩溃")

        # 行数上限
        si._LOG_PATH = log_path
        out_cap = await si.read_recent_logs(lines=99999)
        check("最近 4 行" in out_cap, "请求行数超过日志实际总行数时，不会假装截了更多")

asyncio.run(_t_read_logs())
si._LOG_PATH = orig_log_path


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_log_visibility 全部通过")
sys.exit(0)
