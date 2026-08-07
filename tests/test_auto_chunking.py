#!/usr/bin/env python3
"""任务体量检测 + 自动切块扇出兜底（任务 #15）——自动生成，绝不覆盖既有测试文件。

覆盖：
  1. core/chunking：estimate_tokens/is_oversized、chunk_text（段落边界切分/单块
     直通/超长单段硬切/空输入）、fanout_over_chunks（拼 task_template、按块打标签）
  2. connectors/document._extract_document_text：返回【完整未截断】文本；
     read_document 截断时提示 process_large_document，不截断时不提示
  3. connectors/spawn_tools.process_large_document：{chunk}占位符校验、
     经registry注册、detach立即返回不占主线、跑完汇总推送、失败时给出可读原因
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_chunking_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. core/chunking ──────────────────────────────────────────────────────────
print("[1] core/chunking")
from core import chunking  # noqa: E402

check(chunking.estimate_tokens("") == 0, "空文本估算为 0 token")
check(chunking.estimate_tokens("a" * 400) == 100, "粗略估算按 4 字符≈1 token")
check(not chunking.is_oversized("短文本", budget_tokens=6000), "短文本不算超量")
check(chunking.is_oversized("a" * 30000, budget_tokens=6000), "远超预算的文本判定为超量")

check(chunking.chunk_text("") == [], "空文本切块 → 空列表")
short = "短文本，不需要切块。"
check(chunking.chunk_text(short, chunk_chars=100) == [short], "短于阈值 → 单块直通，内容不变")

paras = "\n\n".join(f"第{i}段" * 50 for i in range(20))  # 每段约 150 字，总长远超阈值
chunks = chunking.chunk_text(paras, chunk_chars=500)
check(len(chunks) > 1, "长文本被切成多块")
check(all(len(c) <= 500 + 10 for c in chunks), "每块大小基本不超过 chunk_chars（允许边界误差）")
check("".join(chunks).replace("\n\n", "") .count("第0段") >= 1 and
      "".join(chunks).replace("\n\n", "").count(f"第19段") >= 1,
      "首尾内容都完整出现在切块结果里，没有丢内容")

huge_single_para = "无换行的超长单段" * 3000  # 没有 \n\n，模拟没有段落边界的巨块
chunks2 = chunking.chunk_text(huge_single_para, chunk_chars=1000)
check(len(chunks2) > 1, "没有段落边界的超长单段 → 退化为硬切，不会卡死/整体作为一块")
check("".join(chunks2) == huge_single_para, "硬切后拼回去内容完全一致，没有丢字符")


async def _t_fanout():
    from core import spawn as spawn_mod

    calls = []

    async def fake_spawn_many(tasks, concurrency=3):
        calls.append((tasks, concurrency))
        return [spawn_mod.SpawnResult(ok=True, label=t["label"], conclusion=f"结果:{t['label']}")
                for t in tasks]

    orig = spawn_mod.spawn_many
    spawn_mod.spawn_many = fake_spawn_many
    try:
        results = await chunking.fanout_over_chunks(
            "处理这段：{chunk}", paras, chunk_chars=500, label="测试扇出", concurrency=2)
        check(len(results) == len(chunks), "fanout_over_chunks 返回结果数与切块数一致")
        sent_tasks, sent_conc = calls[0]
        check(sent_conc == 2, "concurrency 参数正确传下去")
        check(all("处理这段：" in t["task"] for t in sent_tasks), "task_template 被正确套用到每一块")
        check(sent_tasks[0]["label"] == "测试扇出_1/{}".format(len(chunks)),
              "标签带上了原文顺序序号")
    finally:
        spawn_mod.spawn_many = orig

    empty_results = await chunking.fanout_over_chunks("x{chunk}", "", label="空输入")
    check(empty_results == [], "空文本 → fanout_over_chunks 直接返回空列表，不调用 spawn_many")

asyncio.run(_t_fanout())


# ── 2. connectors/document ────────────────────────────────────────────────────
print("[2] document._extract_document_text / read_document 截断提示")
import connectors.document as doc  # noqa: E402

_txt_small = _TMP / "small.txt"
_txt_small.write_text("这是一段不长的文字内容。" * 5, encoding="utf-8")

_txt_big = _TMP / "big.txt"
_txt_big.write_text("超长内容测试。" * 40000, encoding="utf-8")  # 远超 MAX_CHARS(120000)


async def _t_document():
    ok, text = await doc._extract_document_text(str(_txt_big))
    check(ok, "大文件提取成功")
    check(len(text) > doc.MAX_CHARS, "_extract_document_text 返回的是完整未截断文本")

    out_small = await doc.read_document(str(_txt_small))
    check("process_large_document" not in out_small, "小文件不触发截断，不出现分块提示")

    out_big = await doc.read_document(str(_txt_big))
    check(len(out_big) < len(text), "大文件经 read_document 被截断（保护主对话上下文）")
    check("process_large_document" in out_big, "截断时提示改用 process_large_document 处理全文")

    ok_missing, err = await doc._extract_document_text(str(_TMP / "不存在.txt"))
    check(not ok_missing and "不存在" in err, "文件不存在时 _extract_document_text 明确报错")

asyncio.run(_t_document())


# ── 3. connectors/spawn_tools.process_large_document ───────────────────────────
print("[3] spawn_tools.process_large_document")
from core import registry  # noqa: E402
import connectors.spawn_tools as st  # noqa: E402

handler = registry.get_handler("process_large_document")
check(handler is st.process_large_document, "process_large_document 已注册且指向正确函数")


async def _t_process_large_document():
    # 占位符校验
    out_bad = await st.process_large_document(str(_txt_small), "没有占位符的模板")
    check("{chunk}" in out_bad, "缺少 {chunk} 占位符时明确报错并指出原因")
    check(str(_txt_small) not in st._RUNNING, "校验失败不会进入运行集")

    from core import delivery
    delivered = []
    orig_deliver = delivery.deliver

    def fake_deliver(track, title, content, severity="normal", **kw):
        delivered.append({"track": track, "title": title, "content": content, "severity": severity})
        return {"delivered": True}

    # 成功路径要经过 core.spawn.spawn_many → 真实起 JarvisController/LLM 客户端，
    # 沙箱里没有可用的模型/网络；跟 test_spawn.py 一样，用桩替掉 spawn_many 本体，
    # 只验证 process_large_document 这一层的分块/派发/推送逻辑，不依赖真跑模型。
    from core import spawn as spawn_mod

    async def fake_spawn_many(tasks, concurrency=3):
        from core.spawn import SpawnResult
        return [SpawnResult(ok=True, label=t["label"], conclusion=f"（桩）已处理：{t['label']}")
                for t in tasks]

    orig_spawn_many = spawn_mod.spawn_many

    delivery.deliver = fake_deliver
    try:
        t0 = time.monotonic()
        out = await st.process_large_document(str(_txt_small), "总结这段：{chunk}", label="_测试大文档")
        elapsed = time.monotonic() - t0
        check(elapsed < 0.15, f"派发立即返回（{elapsed*1000:.0f}ms），不占用主对话")
        check("已派发" in out, "返回派发说明")
        check(any(v["kind"] == "document_fanout" for v in st._RUNNING.values()),
              "运行集里登记了 document_fanout 类型的任务")

        spawn_mod.spawn_many = fake_spawn_many   # 后台任务此刻还没跑到 spawn_many，替换来得及
        await asyncio.sleep(0.5)   # 等后台任务跑完（真实读取小文件+调用桩 spawn_many）
        check(not any(v["label"] == "_测试大文档" for v in st._RUNNING.values()),
              "跑完后从运行集移除")
        matched = [d for d in delivered if "_测试大文档" in d["title"]]
        check(len(matched) == 1, "跑完后推送了一条汇总结果")
        check(matched[0]["severity"] == "normal", "成功时是 normal 级别")
        check("（桩）已处理" in matched[0]["content"], "汇总内容里带上了各块的处理结果")

        # 失败路径：路径不存在（走不到 spawn_many 这步，不需要桩）
        out2 = await st.process_large_document(str(_TMP / "不存在的文件.txt"), "处理：{chunk}",
                                                label="_测试大文档失败")
        check("已派发" in out2, "即便目标文件不存在，派发本身仍立即成功返回")
        await asyncio.sleep(0.3)
        matched2 = [d for d in delivered if "_测试大文档失败" in d["title"]]
        check(len(matched2) == 1 and matched2[0]["severity"] == "high",
              "文件不存在 → 后台任务失败，high 级告警推送，不静默")
    finally:
        delivery.deliver = orig_deliver
        spawn_mod.spawn_many = orig_spawn_many

asyncio.run(_t_process_large_document())


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_chunking 全部通过")
sys.exit(0)
