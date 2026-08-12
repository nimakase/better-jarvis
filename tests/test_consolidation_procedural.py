#!/usr/bin/env python3
"""过程记忆巩固(core/consolidation.run_procedural_consolidation) —— 确定性单测
（隔离临时库）。跑：.venv/bin/python tests/test_consolidation_procedural.py"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_consol_proc_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

from core import procedures  # noqa: E402
procedures.init_db()
from core import consolidation  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


_RDIR = _TMP / "reviews_procedural"

MODEL_OUTPUT_TMPL = """好的，以下是操作：
```json
[
  {"op": "add", "problem": "customer_loop 参数散落在 config.py 全局 Settings", "method": "起一个 <skill>/settings.py 独立 pydantic-settings 类读同一个 .env，把专属参数迁过去", "evidence": "2026-08-12 第②步"},
  {"op": "add", "problem": "view_manager 里手搓 resume/limit/错误收集逻辑重复两遍", "method": "抽成 core/batch.run_batch 通用原语，key_fn+worker_fn+done_keys", "evidence": "2026-08-12 第①步"},
  {"op": "add", "problem": "没有 method 的推测型经验"},
  {"op": "add", "problem": "有问题有方法但没依据", "method": "随便什么方法"},
  {"op": "confirm", "id": %d},
  {"op": "revise", "id": %d, "method": "修正后的解法：先查 schedules/ 目录是否已有同名任务再建", "evidence": "补充细节"},
  {"op": "supersede", "id": 9999, "reason": "不存在的"},
  {"op": "自创操作", "text": "x"}
]
```""" % (0, 0)


async def _main():
    r_seed1 = procedures.add_procedure("种子问题一", "种子解法一", evidence="种子")
    r_seed2 = procedures.add_procedure("种子问题二", "种子解法二", evidence="种子")
    out = MODEL_OUTPUT_TMPL.replace('"id": 0', f'"id": {r_seed1["id"]}', 1)
    out = out.replace('"id": 0', f'"id": {r_seed2["id"]}', 1)

    async def fake_model(prompt):
        check("现有过程记忆" in prompt and "self_review" in prompt,
              "prompt 含过程记忆与 self_review 复盘块")
        check("evidence" in prompt, "prompt 含依据要求")
        return out

    material = {
        "self_reviews": "## 2026-08-10.md\n诊断出 xxx 缺陷，修法是 yyy",
        "conversations": "[user] 这个 bug 到底怎么修的",
        "signals": [],
        "procedures": procedures.list_procedures(),
    }
    res = await consolidation.run_procedural_consolidation(fake_model, material=material,
                                                            review_dir=_RDIR)
    check(res["n_ops"] == 8, "8 条操作全部被解析（含坏的）")

    probs = [p["problem"] for p in procedures.list_procedures()]
    check(any("参数散落" in t for t in probs), "第一条合格 add 落库")
    check(any("手搓" in t for t in probs), "第二条合格 add 落库")

    rejected_details = "；".join(o["detail"] for o in res["rejected"])
    check("problem/method 为空" in rejected_details, "缺 method 的 add 被打回")
    check("依据必填" in rejected_details, "无依据的 add 被打回")
    check("不存在" in rejected_details or "无此" in rejected_details,
          "对不存在 id 的 supersede 被打回")
    check(any("未知操作" in o["detail"] for o in res["rejected"]), "自创操作被打回")

    review = Path(res["review_path"])
    check(review.exists() and "过程记忆巩固复盘" in review.read_text(encoding="utf-8"),
          "复盘文档产出（独立标题，不与 L1 事实巩固复盘混淆）")

    check(any(o["verdict"] == "已修正" for o in res["applied"]), "合格 revise 生效")

    # 每轮新增上限
    distinct = [("忘记删除临时目录导致磁盘占满", "跑完测试用 tempfile 自动清理"),
                ("接口调用超时没有重试", "加指数退避重试三次"),
                ("日志打印泄露手机号", "打印前脱敏关键字段"),
                ("并发写同一文件导致错乱", "改写临时文件再原子替换"),
                ("第三方库升级后签名变化", "锁定版本号并加兼容性测试"),
                ("容器重启后定时任务丢失", "配置落盘而非只存内存")]
    many = "[" + ",".join(
        f'{{"op": "add", "problem": "{p}", "method": "{m}", "evidence": "依据{i}"}}'
        for i, (p, m) in enumerate(distinct)) + "]"

    async def flood_model(prompt):
        return many

    before = len(procedures.list_procedures())
    res2 = await consolidation.run_procedural_consolidation(flood_model, material=material,
                                                             review_dir=_RDIR)
    added = len(procedures.list_procedures()) - before
    check(added <= consolidation.MAX_PROCEDURE_ADDS_PER_RUN,
          f"每轮新增被限量（实际 {added} ≤ {consolidation.MAX_PROCEDURE_ADDS_PER_RUN}）")
    check(len(res2["rejected"]) >= 2, "超限的 add 被打回而非静默丢弃")

    async def empty_model(prompt):
        return "近期没什么值得记的。[]"

    res3 = await consolidation.run_procedural_consolidation(empty_model, material=material,
                                                             review_dir=_RDIR)
    check(res3["n_ops"] == 0 and not res3["applied"], "空轮次安全通过")

    # 与 L1 事实巩固互不干扰（共享 parse_ops/_write_review 等底层，但落库表不同）
    from core import profile
    profile.init_db()
    check(profile.count() == 0, "过程记忆巩固不写 L1 事实表（表隔离，互不污染）")


asyncio.run(_main())

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_consolidation_procedural 全部通过")
sys.exit(0)
