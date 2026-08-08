#!/usr/bin/env python3
"""web_search(AnySearch) + fetch_page(Crawl4AI) + structured(Instructor) —— 确定性单测。

不联网、不装可选包：验证降级路径、护栏归属、用量、解析健壮性。
"""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. 护栏归属（三个新工具的 effect/taint）──────────────────────────────────
print("[1] 护栏归属")
from core import effects, trust  # noqa: E402
import connectors.web_search as ws  # noqa: E402
import connectors.web_fetch as wf  # noqa: E402

check(effects.effect_of("web_search") == effects.READ_EXTERNAL, "web_search=read_external")
check(effects.effect_of("fetch_page") == effects.READ_EXTERNAL, "fetch_page=read_external")
check(trust.is_tainting("web_search") and trust.is_tainting("fetch_page"),
      "两者都 tainting（结果不可信→污染本回合，禁对外动作）")
# 子 agent 默认白名单应包含它们（只读）
from core import spawn  # noqa: E402
import connectors.calendar_tools  # noqa: E402, F401
wl = spawn.default_whitelist()
check("web_search" in wl and "fetch_page" in wl, "只读→子 agent 白名单天生继承")

# ── 2. web_search 优先级 + 降级 + 用量 ─────────────────────────────────────────
# 2026-08-08 重写：原版只摆弄 ANYSEARCH_API_KEY，没管 EXA_API_KEY——但 Exa 是
# 2026-08-07 起改成的【优先】后端（见 web_search.py 顶部注释），只要 EXA_API_KEY
# 非空就会先打 Exa，AnySearch 那几个断言根本测不到。且原版直接拿假 key 打真实
# 网络，靠"沙箱连不出去"这个环境巧合制造失败路径——用户本机网络是通的，Exa
# 会真的打进去，行为完全不同，暴露了这个巧合。改成 monkeypatch _call_exa/
# _call_anysearch，不再依赖网络是否可达，也不会真的消耗第三方 API 额度。
print("[2] web_search 优先级/降级/用量")


async def _t_search():
    orig_exa_key = config.EXA_API_KEY
    orig_any_key = config.ANYSEARCH_API_KEY
    orig_exa_cap = config.EXA_DAILY_CAP
    orig_any_cap = config.ANYSEARCH_DAILY_CAP
    orig_call_exa = ws._call_exa
    orig_call_any = ws._call_anysearch

    def _reset_usage():
        ws._EXA_USAGE.update({"date": "", "count": 0})
        ws._USAGE.update({"date": "", "count": 0})

    try:
        # 两个 key 都空 → 不发任何请求，直接退回联网提示
        config.EXA_API_KEY = ""
        config.ANYSEARCH_API_KEY = ""
        _reset_usage()
        out = await ws.web_search("测试")
        check("结构化搜索不可用" in out and "联网" in out, "两个 key 都空 → 优雅退回联网提示")

        # 只配 Exa，调用失败（模拟鉴权错误）→ 退回，且只计 Exa 用量、不碰 AnySearch 计数
        config.EXA_API_KEY = "fake-exa-key"
        config.ANYSEARCH_API_KEY = ""
        _reset_usage()

        async def _fake_exa_fail(query, max_results):
            return False, "模拟鉴权失败"
        ws._call_exa = _fake_exa_fail
        out2 = await ws.web_search("深圳 连接器")
        check("结构化搜索不可用" in out2, "只配 Exa 且调用失败 → 退回而非崩溃")
        check(ws._exa_usage_today() == 1, "Exa 调用失败也计一次用量（真发生了请求）")
        check(ws._usage_today() == 0, "没配 AnySearch key 时 AnySearch 用量不应被计")

        # Exa 调用成功 → 直接用 Exa 结果，不再兜底到 AnySearch
        async def _fake_exa_ok(query, max_results):
            return True, [{"title": "T", "url": "u", "snippet": "s", "content": "c"}]
        ws._call_exa = _fake_exa_ok
        _reset_usage()
        out3 = await ws.web_search("测试")
        check("Exa" in out3 and "结构化搜索不可用" not in out3, "Exa 调用成功 → 直接返回 Exa 结果")

        # Exa 已达自设每日上限 → 跳过 Exa 不再发请求，退回信号里体现原因
        config.EXA_DAILY_CAP = 1
        ws._EXA_USAGE.update({"date": date.today().isoformat(), "count": 1})
        out4 = await ws.web_search("测试")
        check("上限" in out4, "Exa 达自设上限 → 不再调用，退回信号里体现原因")
        config.EXA_DAILY_CAP = orig_exa_cap

        # Exa 失败、AnySearch 兜底成功 → 用 AnySearch 的结果
        config.EXA_API_KEY = "fake-exa-key"
        config.ANYSEARCH_API_KEY = "fake-any-key"
        _reset_usage()
        ws._call_exa = _fake_exa_fail

        async def _fake_any_ok(query, max_results):
            return True, [{"title": "T2", "url": "u2", "snippet": "s2", "content": "c2"}]
        ws._call_anysearch = _fake_any_ok
        out5 = await ws.web_search("测试")
        check("AnySearch" in out5, "Exa 失败后正确兜底到 AnySearch")
        check(ws._exa_usage_today() == 1 and ws._usage_today() == 1,
              "两个后端的用量各自独立计数")
    finally:
        ws._call_exa = orig_call_exa
        ws._call_anysearch = orig_call_any
        config.EXA_API_KEY = orig_exa_key
        config.ANYSEARCH_API_KEY = orig_any_key
        config.EXA_DAILY_CAP = orig_exa_cap
        config.ANYSEARCH_DAILY_CAP = orig_any_cap
        _reset_usage()

asyncio.run(_t_search())

# 响应解析：真实契约 data.results（含 content 整页抽取）
real = {"code": 0, "message": "success", "data": {"results": [
    {"title": "Go 1.26", "url": "https://go.dev", "snippet": "摘要", "content": "整页正文…"}
], "metadata": {"total_results": 1}}}
parsed = ws._extract_results(real)
check(len(parsed) == 1 and parsed[0]["title"] == "Go 1.26", "解析真实 data.results 结构")
check(parsed[0]["content"] == "整页正文…", "保留 content 字段（整页抽取，免再抓页）")
check(ws._extract_results({"results": [{"title": "A", "url": "u"}]})[0]["title"] == "A",
      "顶层 results 兜底仍兼容")
check(ws._extract_results({"weird": 1}) == [], "认不出的结构 → 空（触发降级，不炸）")

# ── 3. fetch_page URL 校验 + 降级 ────────────────────────────────────────────
print("[3] fetch_page")
check(wf._valid_url("https://x.com/a")[0], "https 通过")
check(not wf._valid_url("ftp://x")[0], "非 http/https 拒绝")
check(not wf._valid_url("https://u:p@x.com")[0], "带凭据 URL 拒绝")


async def _t_fetch():
    out = await wf.fetch_page("notaurl")
    check("无法抓取" in out, "非法 URL 明确报错")
    # crawl4ai 未装 → _via_crawl4ai 返回 None（触发 httpx 降级）
    got = await wf._via_crawl4ai("https://example.com")
    check(got is None, "未装 crawl4ai → 返回 None 触发降级（不报错）")

asyncio.run(_t_fetch())

# ── 4. structured 降级（instructor 未装）──────────────────────────────────────
print("[4] structured 降级")
from core import structured  # noqa: E402

# 不再假设"环境恰好没装 instructor"——开发/生产机装了也该稳过。这里只验证探测
# 函数返回布尔；真正的降级路径由下方 schema=None 强制走 fallback 验证（见 extract：
# 仅当 schema 非空且 instructor 可用才走 instructor 路径），与是否安装无关。
check(isinstance(structured.instructor_available(), bool),
      "instructor 可用性探测返回布尔（不挑环境）")


async def _t_structured():
    # 注入假解析器验证走的是降级路径 + fallback_parser 生效
    async def fake_llm_text():
        return None
    # 用 monkeypatch 让 get_client 返回一个吐固定 JSON 的假 client
    import core.llm as llm

    class FakeResp:
        def __init__(self, txt):
            self.choices = [type("C", (), {"message": type("M", (), {"content": txt})()})()]

    class FakeClient:
        def __init__(self, txt): self._t = txt; self.chat = type("X", (), {})()
        async def _create(self, **kw): return FakeResp(self._t)
        def bind(self):
            self.chat.completions = type("Y", (), {"create": staticmethod(self._create)})()
            return self

    orig = llm.get_client
    llm.get_client = lambda timeout=120: FakeClient('噪声 {"doc_type":"policy","fields":{"保额":"100万"}} 尾巴').bind()
    try:
        obj = await structured.extract("prompt", schema=None)  # schema=None → 纯降级
        check(obj and obj["fields"]["保额"] == "100万", "降级路径用 fallback_parser 抢救出对象")

        # 自定义 fallback_parser 被采用
        obj2 = await structured.extract("p", schema=None,
                                        fallback_parser=lambda t: {"raw": t})
        check(isinstance(obj2, dict) and obj2.get("raw", "").startswith("噪声"),
              "自定义 fallback_parser 生效")
    finally:
        llm.get_client = orig

asyncio.run(_t_structured())

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_search_fetch_structured 全部通过")
sys.exit(0)
