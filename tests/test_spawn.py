#!/usr/bin/env python3
"""core/spawn 子 agent 抽象 —— 确定性单测（stub controller，不依赖模型/联网）。"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_spawn_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

from core import artifacts, effects, registry, spawn  # noqa: E402

artifacts.LIBRARY_DIR = _TMP / "图书馆"
artifacts.init_db()
spawn._results_dir = lambda: _TMP / "results" or None  # noqa: E731
(_TMP / "results").mkdir()
spawn._results_dir = lambda: _TMP / "results"  # noqa: E731

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. 白名单推导（⑧）────────────────────────────────────────────────────────
print("[1] 白名单推导")
import connectors.calendar_tools  # noqa: E402, F401  （注册真实工具）
import connectors.artifact_tools  # noqa: E402, F401

wl = spawn.default_whitelist()
check("calendar_agenda" in wl, "只读工具（查日程）默认在白名单")
check("library_overview" in wl, "只读工具（图书馆总览）默认在白名单")
check("calendar_create_event" not in wl, "写本机工具默认不在白名单")
check("calendar_delete_event" not in wl, "不可逆工具默认不在白名单")
check("purge_artifacts" not in wl, "purge 默认不在白名单")

wl2 = spawn.build_whitelist(extra_tools=["calendar_create_event"])
check("calendar_create_event" in wl2 and "calendar_delete_event" not in wl2,
      "extra_tools 显式点名才升权，其余不受影响")

# ── 2. controller 白名单过滤（暴露层）─────────────────────────────────────────
print("[2] controller 暴露层过滤")
if not getattr(config, "OPENROUTER_API_KEY", None):
    config.OPENROUTER_API_KEY = "test-key-offline"
# 隔离环境的代理变量会让 httpx 构造报错（socks 依赖）；本测试不联网，清掉。
import os  # noqa: E402
for _v in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
           "HTTP_PROXY", "http_proxy"):
    os.environ.pop(_v, None)
from core.controller import JarvisController  # noqa: E402

config.PROGRESSIVE_TOOLS = False   # 隔离渐进披露，单测白名单过滤本身
ctl = JarvisController(interactive=False, allowed_tools={"calendar_agenda"},
                       max_tool_rounds=3)
names = {t["function"]["name"] for t in ctl.get_exposed_tools()
         if t.get("function")}
check("calendar_agenda" in names, "白名单内工具被暴露")
check("calendar_create_event" not in names and "purge_artifacts" not in names,
      "白名单外工具不暴露给模型")
check(ctl.max_tool_rounds == 3, "轮次预算（⑨）落到实例")

# ── 3. 结果契约解析 ───────────────────────────────────────────────────────────
print("[3] 契约解析")
ok_json = '前面是过程叙述…\n{"conclusion": "A 公司有信号", "evidence": ["新闻X"], "uncertainties": [], "unfinished": []}'
obj = spawn.parse_contract(ok_json)
check(obj and obj["conclusion"] == "A 公司有信号", "带前缀文本的契约 JSON 可解析")

multi = '{"foo": 1} 中间话 {"conclusion": "最终", "evidence": []}'
obj2 = spawn.parse_contract(multi)
check(obj2 and obj2["conclusion"] == "最终", "多个对象时取含 conclusion 的那个")

check(spawn.parse_contract("没有任何 JSON") is None, "无 JSON 返回 None（不抛错）")
check(spawn.parse_contract('{"broken": ') is None, "残缺 JSON 返回 None（不抛错）")


# ── 4. spawn 主流程（stub controller）────────────────────────────────────────
print("[4] spawn 主流程")


class StubController:
    """按脚本吐 text 事件的假 controller。"""

    def __init__(self, chunks, delay=0.0):
        self.chunks, self.delay = chunks, delay

    async def chat(self, user_message):
        self.seen_prompt = user_message
        for c in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield {"type": "text", "text": c}


async def _main():
    # 正常路径：按契约输出
    stub = StubController([
        "查了三家…", '\n{"conclusion": "B 公司裁员信号明确", '
        '"evidence": ["官网公告"], "uncertainties": ["规模未知"], "unfinished": []}',
    ])
    res = await spawn.spawn("查 B 公司信号", label="B司调研", _controller=stub)
    check(res.ok and res.conclusion == "B 公司裁员信号明确", "契约结果解析进 SpawnResult")
    check("结果契约" in stub.seen_prompt, "任务提示词里注入了结果契约")
    check(res.result_path and Path(res.result_path).exists(), "结果落盘 workspace/results")
    regs = artifacts.list_artifacts(producer="spawn:B司调研")
    check(len(regs) == 1 and regs[0]["state"] == "trial",
          "结果按 trial 登记图书馆（7 天自动过期，不积灰）")
    check("不确定：规模未知" in res.brief(), "brief 摘要含不确定项")

    # 降级路径：没按契约输出
    res2 = await spawn.spawn("x", label="降级", _controller=StubController(["自由发挥的一段话"]))
    check(res2.ok and "自由发挥" in res2.conclusion, "无契约时全文降级为结论")
    check(any("未按结果契约" in u for u in res2.uncertainties), "降级被显式标注")

    # 超时路径（⑨）：强制交回部分内容
    slow = StubController(["还在查…", '{"conclusion": "来不及了"}'], delay=0.35)
    res3 = await spawn.spawn("慢任务", label="超时", timeout_s=0.15, _controller=slow)
    check(res3.partial, "超时标记 partial")
    check("还在查" in res3.conclusion or res3.error, "超时仍交回已产出内容（不白烧）")

    # 无输出路径
    res4 = await spawn.spawn("x", label="空", _controller=StubController([]))
    check(not res4.ok and "无输出" in res4.error, "无输出判失败并说明")

    # 自由文本模式（contract=False，self_review 等要完整原文的场景）
    long_payload = '[{"path": "core/x.py", "new_code": "' + "很长的代码" * 200 + '"}]'
    stub5 = StubController([long_payload])
    res5 = await spawn.spawn("反思", label="自由文本", contract=False, _controller=stub5)
    check("结果契约" not in stub5.seen_prompt, "自由文本模式不注入契约")
    check(res5.raw_text == long_payload, "raw_text 保留完整原文（不截断）")
    check(len(res5.conclusion) <= 2000, "conclusion 仍有界（raw_text 才是主产物）")

    # 契约模式下 raw_text 同样保留
    stub6 = StubController(['{"conclusion": "短结论"}'])
    res6 = await spawn.spawn("x", label="raw保留", _controller=stub6)
    check(res6.raw_text and res6.conclusion == "短结论", "契约模式 raw_text 也保留")

    # —— self_review 换心脏的接线（_agentic_model_fn）——
    from core import self_review as sr
    import core.spawn as spawn_mod

    calls = {}
    orig_spawn = spawn_mod.spawn

    async def fake_spawn(task, **kw):
        calls["task"], calls["kw"] = task, kw
        return spawn.SpawnResult(ok=True, label="x", raw_text='[{"fake": "proposals"}]')

    spawn_mod.spawn = fake_spawn
    try:
        out = await sr._agentic_model_fn("反思 prompt 正文")
        check(out == '[{"fake": "proposals"}]', "_agentic_model_fn 返回子 agent 原文")
        check(calls["kw"].get("contract") is False, "反思用自由文本模式")
        check("只读自省工具" in calls["task"], "提示词告知可用自省工具（先查证再开方）")
        check(calls["kw"].get("max_rounds"), "反思子 agent 带轮次预算")
    finally:
        spawn_mod.spawn = orig_spawn


asyncio.run(_main())

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_spawn 全部通过")
sys.exit(0)
