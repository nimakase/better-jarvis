#!/usr/bin/env python3
"""2026-08-07 框架升级批次的确定性单测——自动生成，绝不覆盖既有测试文件。

覆盖四项：
  1. web_search 降级文案按 :online 是否存在分支（DeepSeek 迁移前置修复）
  2. core/group_memory 存储层（组作用域记忆）
  3. connectors/spawn_tools 的 detach 派发行为（不再占用主对话轮次）
  4. core/skill_policy.auto_module_api_text（BUILDING_BLOCKS 自动生成结构层）

不联网、不装可选包、不依赖真实 key。
"""
import asyncio
import sys
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


# ── 1. web_search 降级文案按模型是否带 :online 分支 ─────────────────────────────
print("[1] web_search 降级文案")
import connectors.web_search as ws  # noqa: E402


async def _t_fallback_branches():
    orig_model = config.CLAUDE_MODEL
    orig_key = config.ANYSEARCH_API_KEY
    orig_exa_key = config.EXA_API_KEY
    config.ANYSEARCH_API_KEY = ""
    config.EXA_API_KEY = ""   # 本节专测降级文案分支，隔离掉 Exa 后端避免真实网络调用
    try:
        # 分支 A：模型带 :online → 仍建议改用模型自带联网（不破坏现有行为/现有测试）
        config.CLAUDE_MODEL = "deepseek/deepseek-v4-flash:online"
        out_online = await ws.web_search("测试A")
        check("联网" in out_online and "结构化搜索不可用" in out_online,
              ":online 存在时 → 建议改用模型自带联网")

        # 分支 B：模型不带 :online（如迁移到 DeepSeek 官方 API 后）→ 不再暗示能联网，
        # 而是如实告知没有联网渠道、要求模型明确声明信息未经核实。
        config.CLAUDE_MODEL = "deepseek-v4-flash"
        out_no_online = await ws.web_search("测试B")
        check("没有其它联网渠道" in out_no_online, "无 :online 时 → 如实告知没有联网渠道")
        check("不要编造" in out_no_online, "无 :online 时 → 明确要求不得编造看似最新的内容")
        check(":online" not in out_no_online.replace("没有其它联网渠道可用", ""),
              "无 :online 时 → 不再建议模型去用它根本没有的:online能力")
    finally:
        config.CLAUDE_MODEL = orig_model
        config.ANYSEARCH_API_KEY = orig_key
        config.EXA_API_KEY = orig_exa_key

asyncio.run(_t_fallback_branches())


# ── 2. core/group_memory 存储层 ─────────────────────────────────────────────
print("[2] group_memory 存储层")
from core import group_memory as gm  # noqa: E402

_TEST_GROUP = "_test_hubspot_auto"

# 清理可能的历史测试残留（幂等）
for n in gm.list_notes(_TEST_GROUP, include_superseded=True):
    gm.delete_note(n["id"])

check(gm.count(_TEST_GROUP) == 0, "初始条数为 0")

r1 = gm.add_note(_TEST_GROUP, "报价规则：低于成本价 15% 需人工审批")
check(r1["ok"], "add_note 成功")
check(gm.count(_TEST_GROUP) == 1, "add_note 后条数 +1")

r2 = gm.add_note(_TEST_GROUP, "报价规则：低于成本价 15% 需人工审批")  # 重复文本
check(r2["ok"] and gm.count(_TEST_GROUP) == 1, "完全重复文本 → 视为再次确认，不新增")

block = gm.build_block(_TEST_GROUP)
check(_TEST_GROUP in block and "报价规则" in block, "build_block 拼出注入块含该组笔记")
check(gm.build_block("_test_empty_group_xyz") == "", "无笔记的组 → build_block 返回空串")

sup = gm.supersede_note(r1["id"])
check(sup["ok"] and gm.count(_TEST_GROUP) == 0, "软删后活跃条数归零（未硬删）")
rows_incl = gm.list_notes(_TEST_GROUP, include_superseded=True)
check(len(rows_incl) == 1, "软删记录仍可查（include_superseded=True）")

res = gm.restore_note(r1["id"])
check(res["ok"] and gm.count(_TEST_GROUP) == 1, "restore_note 恢复成功")

# 条数上限纪律
orig_max = gm.MAX_NOTES_PER_GROUP
gm.MAX_NOTES_PER_GROUP = 1
over = gm.add_note(_TEST_GROUP, "第二条不同内容的笔记")
check(not over["ok"] and "上限" in over["message"], "超出每组条数上限 → 拒绝新增")
gm.MAX_NOTES_PER_GROUP = orig_max

# 收尾清理
for n in gm.list_notes(_TEST_GROUP, include_superseded=True):
    gm.delete_note(n["id"])


# ── 3. spawn_tools 的 detach 派发行为 ────────────────────────────────────────
print("[3] spawn_tools detach 派发")
import connectors.spawn_tools as st  # noqa: E402
from core import effects, registry  # noqa: E402

check(effects.effect_of("spawn_subtask") == effects.READ_EXTERNAL, "spawn_subtask=read_external")
check(effects.effect_of("spawn_fanout") == effects.READ_EXTERNAL, "spawn_fanout=read_external")
check(registry.get_handler("spawn_status") is not None, "spawn_status 已注册")


async def _t_detach_returns_fast():
    import time

    async def _fake_spawn(task, label="", **kw):
        await asyncio.sleep(0.3)   # 模拟耗时子任务

        class R:
            ok = True
            def brief(self):
                return "fake result"
        return R()

    orig = st.spawn
    st.spawn = _fake_spawn
    try:
        t0 = time.monotonic()
        out = await st.spawn_subtask("测试任务", label="快返回测试")
        elapsed = time.monotonic() - t0
        check(elapsed < 0.2, f"spawn_subtask 立刻返回，不等子 agent 跑完（耗时 {elapsed:.3f}s）")
        check("已派发" in out and "后台" in out, "返回文案说明已派发到后台")
        check(len(st._RUNNING) >= 1, "派发后 _RUNNING 登记了这条任务")
        await asyncio.sleep(0.5)   # 等后台任务真正跑完
        check(len(st._RUNNING) == 0, "后台任务跑完后自动从 _RUNNING 移除")
    finally:
        st.spawn = orig

asyncio.run(_t_detach_returns_fast())


async def _t_fanout_limit():
    out = await st.spawn_fanout([f"任务{i}" for i in range(6)])
    check("最多 5 个" in out, "spawn_fanout 超过 5 项 → 直接拒绝，不派发")

asyncio.run(_t_fanout_limit())


# ── 4. skill_policy.auto_module_api_text ────────────────────────────────────
print("[4] auto_module_api_text 结构层自动生成")
from core import skill_policy as sp  # noqa: E402

text = sp.auto_module_api_text("core.group_memory")
check("add_note" in text, "抽到公共函数 add_note")
check("build_block" in text, "抽到公共函数 build_block")
check("MAX_NOTES_PER_GROUP" in text, "抽到大写常量 MAX_NOTES_PER_GROUP")
check("_norm_group" not in text, "默认不列私有函数（include_private=False）")
check("未经人工核实" in text, "明确标注这是自动生成、未经人工核实语义")

text_priv = sp.auto_module_api_text("core.group_memory", include_private=True)
check("_norm_group" in text_priv, "include_private=True 时列出私有函数")

bad = sp.auto_module_api_text("core.this_module_does_not_exist_xyz")
check("无法读取或解析" in bad, "不存在的模块 → 明确报错文本，不抛异常")


# ── 5. controller._build_system_prompt 的组作用域记忆注入挂钩 ──────────────────
print("[5] controller 组记忆注入挂钩")
from core import controller as ctl  # noqa: E402
from core import group_memory as gm2  # noqa: E402

_TEST_GROUP2 = "_test_ctl_hook_auto"
for n in gm2.list_notes(_TEST_GROUP2, include_superseded=True):
    gm2.delete_note(n["id"])

check(ctl._build_system_prompt() != "", "无参调用（老行为）仍正常返回非空 prompt")
check(_TEST_GROUP2 not in ctl._build_system_prompt(), "未激活该组时 → prompt 不含其笔记")

gm2.add_note(_TEST_GROUP2, "这是一条只在该组激活时才该出现的测试笔记")
prompt_with = ctl._build_system_prompt({_TEST_GROUP2})
check(_TEST_GROUP2 in prompt_with and "只在该组激活时才该出现" in prompt_with,
      "激活该组时 → prompt 含其专属笔记")
check(_TEST_GROUP2 not in ctl._build_system_prompt(), "空 active_groups → 依旧不含（不会串组）")
check(_TEST_GROUP2 not in ctl._build_system_prompt({"某个不相关的组"}),
      "激活别的组时 → 不会看到这个组的笔记（组间隔离）")

for n in gm2.list_notes(_TEST_GROUP2, include_superseded=True):
    gm2.delete_note(n["id"])


# ── 6. core/model_capabilities 结构化能力表 ─────────────────────────────────
print("[6] model_capabilities 结构化能力表")
from core import model_capabilities as mc  # noqa: E402

c1 = mc.capabilities_of("deepseek/deepseek-v4-flash:online")
check(c1.online_search, "OpenRouter :online 后缀 → online_search=True")
check(c1.vision, "该模型底层是 v4-flash → 同时继承视觉能力")

c2 = mc.capabilities_of("deepseek-v4-flash-0731")
check(c2.vision and c2.context_window == 1_000_000, "DeepSeek 官方 API 型号 → 视觉+百万上下文，无需 :online")
check(not c2.online_search, "官方 API 型号本身不带 :online → online_search=False")

c3 = mc.capabilities_of("some-totally-unknown-model-xyz")
check(c3 is mc.UNKNOWN, "未登记模型 → 保守返回 UNKNOWN，不瞎猜能力")

note_online = ctl._network_capability_note()
check("联网能力" in note_online, "controller 能力提示词仍含联网段落（老行为兼容）")

orig_model = config.CLAUDE_MODEL
config.CLAUDE_MODEL = "deepseek-v4-flash-0731"
try:
    note_vision = ctl._network_capability_note()
    check("视觉能力" in note_vision, "配置为已知支持视觉的模型 → 提示词自动带上视觉段落")
    check("没有" in note_vision and "联网" in note_vision, "该型号没有 :online → 如实告知不能联网")
finally:
    config.CLAUDE_MODEL = orig_model


# ── 7. effects.ConfirmGate 门槛下放到 write_external ────────────────────────
print("[7] ConfirmGate 覆盖 write_external（如 run_workflow）")
from core import effects as eff_mod  # noqa: E402

check(eff_mod.CONFIRM_GATE_FLOOR == eff_mod.WRITE_EXTERNAL, "门槛常量 = write_external")
check(eff_mod.effect_of("run_workflow") == eff_mod.WRITE_EXTERNAL, "run_workflow 本身分级不变")

g_wf = eff_mod.ConfirmGate()
g_wf.new_user_turn()
ok_wf, msg_wf = g_wf.check("run_workflow", '{"workflow_id":"prospect_daily"}')
check(not ok_wf, "此前不受 ConfirmGate 管的 write_external 工具（run_workflow）现在也被拦")
check("对外产生影响" in msg_wf, "拦截消息按等级用词（write_external≠不可逆，措辞区分）")

g_wf.new_user_turn()   # 用户已看到复述并回话
ok_wf2, _ = g_wf.check("run_workflow", '{"workflow_id":"prospect_daily"}')
check(ok_wf2, "隔一个用户回合后同名同参 → 放行（跟 irreversible 走同一套状态机）")

# 只读/write_local 完全不受影响（老行为零变化）
g_ro = eff_mod.ConfirmGate()
ok_ro, msg_ro = g_ro.check("remember_fact", '{"text":"x"}')
check(ok_ro and msg_ro == "", "write_local 工具不受新门槛影响，仍直接放行")

# irreversible 措辞仍是"不可逆"（老文案不变）
g_irr = eff_mod.ConfirmGate()
g_irr.new_user_turn()
_, msg_irr = g_irr.check("delete_tool", '{"name":"x"}')
check("不可逆" in msg_irr, "irreversible 工具的拦截消息措辞不变（仍说'不可逆'）")


# ── 8. Exa 搜索后端（解析 + 后端优先级 + 用量/降级）──────────────────────────
print("[8] Exa 搜索后端")

# 8a. 解析：Exa 真实契约 {"results":[{title,url,text,...}]}
exa_real = {"results": [
    {"title": "Go 1.26 发布", "url": "https://go.dev", "text": "正文全文……",
     "publishedDate": "2026-08-01", "score": 0.9},
], "requestId": "abc"}
parsed_exa = ws._extract_exa_results(exa_real)
check(len(parsed_exa) == 1 and parsed_exa[0]["title"] == "Go 1.26 发布", "解析 Exa 真实 results 结构")
check(parsed_exa[0]["content"] == "正文全文……", "content 取自 Exa 的 text 字段")
check(ws._extract_exa_results({"weird": 1}) == [], "认不出的结构 → 空（触发降级，不炸）")
check(ws._extract_exa_results({"results": [{"title": "无正文的条目", "url": "u"}]})[0]["content"] == "",
      "没有 text 字段时 content 留空而非报错")


async def _t_exa_priority_and_fallback():
    orig_exa = ws._call_exa
    orig_any = ws._call_anysearch
    orig_exa_key = config.EXA_API_KEY
    orig_any_key = config.ANYSEARCH_API_KEY
    orig_exa_cap = config.EXA_DAILY_CAP
    orig_any_cap = config.ANYSEARCH_DAILY_CAP
    ws._EXA_USAGE.update({"date": "", "count": 0})
    ws._USAGE.update({"date": "", "count": 0})

    exa_calls, any_calls = [], []

    async def fake_exa_ok(query, max_results):
        exa_calls.append(query)
        return True, [{"title": "Exa结果", "url": "u", "snippet": "", "content": "c"}]

    async def fake_exa_fail(query, max_results):
        exa_calls.append(query)
        return False, "模拟失败"

    async def fake_any_ok(query, max_results):
        any_calls.append(query)
        return True, [{"title": "AnySearch结果", "url": "u", "snippet": "", "content": "c"}]

    try:
        config.EXA_API_KEY = "fake-exa-key"
        config.ANYSEARCH_API_KEY = "fake-any-key"

        # Exa 配置了且成功 → 优先用 Exa，AnySearch 完全不被调用
        ws._call_exa = fake_exa_ok
        ws._call_anysearch = fake_any_ok
        out = await ws.web_search("测试查询")
        check("Exa结果" in out and "Exa" in out, "Exa 可用时优先用 Exa")
        check(len(exa_calls) == 1 and len(any_calls) == 0, "Exa 成功时 AnySearch 完全不被调用（省一次请求）")

        # Exa 失败 → 自动兜底到 AnySearch
        exa_calls.clear()
        any_calls.clear()
        ws._call_exa = fake_exa_fail
        out2 = await ws.web_search("测试查询2")
        check("AnySearch结果" in out2, "Exa 失败时自动兜底到 AnySearch")
        check(len(exa_calls) == 1 and len(any_calls) == 1, "先试 Exa 再试 AnySearch，顺序正确")

        # 两者都失败 → 退回信号，且措辞如实反映两边都试过了
        ws._call_anysearch = fake_exa_fail  # 复用同一个失败函数模拟 AnySearch 也失败
        out3 = await ws.web_search("测试查询3")
        check("结构化搜索不可用" in out3, "两个后端都失败 → 退回信号而非崩溃")

        # Exa 达每日软上限 → 跳过 Exa 直接试 AnySearch
        exa_calls.clear()
        any_calls.clear()
        ws._call_exa = fake_exa_ok
        ws._call_anysearch = fake_any_ok
        config.EXA_DAILY_CAP = 0   # 已达上限
        out4 = await ws.web_search("测试查询4")
        check(len(exa_calls) == 0, "Exa 达自设上限 → 跳过，不发请求")
        check("AnySearch结果" in out4, "Exa 跳过后仍能从 AnySearch 拿到结果")
    finally:
        ws._call_exa = orig_exa
        ws._call_anysearch = orig_any
        config.EXA_API_KEY = orig_exa_key
        config.ANYSEARCH_API_KEY = orig_any_key
        config.EXA_DAILY_CAP = orig_exa_cap
        config.ANYSEARCH_DAILY_CAP = orig_any_cap

asyncio.run(_t_exa_priority_and_fallback())


# ── 9. core/search_augment（DeepSeek 迁移的 :online 补偿层）──────────────────
print("[9] search_augment 联网检索补偿层")
from core import search_augment as sa  # noqa: E402
import connectors.web_search as ws2  # noqa: E402 (复用别名，避免遮蔽上面的 ws)


async def _t_search_augment():
    orig_model = config.CLAUDE_MODEL
    orig_search = ws2.web_search

    async def fake_ws(query, max_results=5):
        return f"🔎 「{query}」搜索结果（Fake）：\n1. 假结果 {query}"

    async def fake_ws_unavailable(query, max_results=5):
        return "[结构化搜索不可用：模拟] 你当前没有其它联网渠道可用。"

    try:
        # 模型自带 :online → 原样返回 prompt，完全不触发搜索（零行为变化）
        config.CLAUDE_MODEL = "deepseek/deepseek-v4-flash:online"
        ws2.web_search = fake_ws
        out = await sa.augment_with_search("原始提示词", ["查询A"], label="test")
        check(out == "原始提示词", "模型自带联网时 → 原样返回 prompt，不做任何改动")

        # 模型没有 :online → 显式搜索，结果拼进 prompt 前面
        config.CLAUDE_MODEL = "deepseek-v4-flash-0731"
        out2 = await sa.augment_with_search("原始提示词", ["查询A", "查询B"], label="test")
        check("原始提示词" in out2, "补偿后的 prompt 仍包含原始内容")
        check("假结果 查询A" in out2 and "假结果 查询B" in out2, "多条查询结果都被拼入")
        check(out2.index("假结果") < out2.index("原始提示词"), "搜索结果拼在原始 prompt 之前")
        check("只依据这些材料" in out2, "明确指示模型只依据检索材料补充、不要编造")

        # 搜索本身也不可用（比如两个后端都没配）→ 原样返回 prompt，不阻断生成
        ws2.web_search = fake_ws_unavailable
        out3 = await sa.augment_with_search("原始提示词", ["查询A"], label="test")
        check(out3 == "原始提示词", "搜索也拿不到东西时 → 原样返回 prompt，不阻断生成流程")

        # 空查询列表 → 等同不可用，原样返回
        ws2.web_search = fake_ws
        out4 = await sa.augment_with_search("原始提示词", [], label="test")
        check(out4 == "原始提示词", "空查询列表 → 原样返回")
    finally:
        config.CLAUDE_MODEL = orig_model
        ws2.web_search = orig_search

asyncio.run(_t_search_augment())

# 确认两处生产调用点改完后仍能正常 import（语法/依赖健全性冒烟）
import importlib  # noqa: E402
_pw = importlib.import_module("prospecting.workflows")
check(hasattr(_pw, "make_llm_generate_fn"), "prospecting.workflows 改完仍可正常 import")
_wd = importlib.import_module("intel.workflow_defs")
check(hasattr(_wd, "_COLLECT_SEARCH_QUERIES") and len(_wd._COLLECT_SEARCH_QUERIES) > 0,
      "intel.workflow_defs 的采集查询清单已就位")


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_framework_upgrades 全部通过")
sys.exit(0)
