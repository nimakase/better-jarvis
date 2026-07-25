#!/usr/bin/env python3
"""profile ⑮ 轻量版 + consolidation ⑭ —— 确定性单测（隔离临时库）。"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_consol_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

from core import profile  # noqa: E402
profile.init_db()

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. profile 覆写/时效/软删（⑮ 轻量版）────────────────────────────────────
print("[1] profile 生命周期")
r1 = profile.add_fact("Ned 的回答偏好：先给结论再给理由", evidence="用户说「先说结论」")
check(r1["ok"], "带证据新增")
fid = r1["id"]

facts = profile.list_facts()
check(facts[0]["evidence"].startswith("用户说"), "evidence 落库")
check(facts[0]["last_confirmed_at"], "新增即有确认时间")

profile.confirm_fact(fid)
check(profile.list_facts()[0]["last_confirmed_at"] >= facts[0]["last_confirmed_at"],
      "confirm 刷新时效")

r_dup = profile.add_fact("Ned 的回答偏好：先给结论再给理由")
check(r_dup["id"] == fid and "已在档案" in r_dup["message"], "完全相同文本降级为确认")

r2 = profile.add_fact("Ned 在深圳做电子元器件生意")
profile.supersede_fact(r2["id"], reason="测试软删")
check(len(profile.list_facts()) == 1, "软删后不出现在活跃清单")
check(len(profile.list_facts(include_superseded=True)) == 2, "软删的仍可查回")
check(profile.count() == 1, "软删不占 MAX_FACTS 名额")
check("测试软删" in [f for f in profile.list_facts(include_superseded=True)
                    if f["id"] == r2["id"]][0]["supersede_reason"], "软删理由留档")

profile.restore_fact(r2["id"])
check(len(profile.list_facts()) == 2, "restore 恢复软删事实")
check(profile.build_block().count("- ") == 2, "build_block 只含活跃事实")

# ── 2. consolidation 机械闸 ───────────────────────────────────────────────────
print("[2] 巩固机械闸")
from core import consolidation  # noqa: E402

_RDIR = _TMP / "reviews"

MODEL_OUTPUT = """好的，以下是操作：
```json
[
  {"op": "add", "text": "Ned 偏好用飞书接收文件而非网页", "evidence": "用户说「以后文件都发飞书」"},
  {"op": "add", "text": "Ned 的回答偏好：结论先行、之后才是理由", "evidence": "多次纠正"},
  {"op": "add", "text": "没有证据的推测型事实"},
  {"op": "confirm", "id": %d},
  {"op": "revise", "id": %d, "text": "Ned 在深圳做电子元器件与呆滞料生意", "evidence": "对话提到 ESO 收货"},
  {"op": "supersede", "id": 9999, "reason": "不存在的"},
  {"op": "自创操作", "text": "x"}
]
```""" % (0, 0)


async def _main():
    fid1 = profile.list_facts()[0]["id"]   # 结论先行那条
    fid2 = profile.list_facts()[1]["id"]   # 深圳生意那条
    out = MODEL_OUTPUT.replace('"id": 0', f'"id": {fid1}', 1)
    out = out.replace('"id": 0', f'"id": {fid2}', 1)

    async def fake_model(prompt):
        # 顺便验证 prompt 组装
        check("现有档案" in prompt and "监督信号" in prompt, "prompt 含档案与信号块")
        check("evidence" in prompt, "prompt 含证据要求")
        return out

    material = {"conversations": "[user] 以后文件都发飞书",
                "signals": [{"kind": "correction", "user_text": "不是，先说结论",
                             "prev_excerpt": "让我详细解释…"}],
                "facts": profile.list_facts()}
    res = await consolidation.run_consolidation(fake_model, material=material,
                                                review_dir=_RDIR)
    check(res["n_ops"] == 7, "7 条操作全部被解析（含坏的）")

    texts = [f["text"] for f in profile.list_facts()]
    check(any("飞书" in t for t in texts), "合格 add 落库")
    # 「结论先行」add 与现有事实高度重合 → 降级为确认而非重复添加
    check(sum("结论" in t for t in texts) == 1, "词面重合的 add 不产生重复条目")
    check(len(res["demoted"]) == 1, "降级为确认被记录")
    check(any("呆滞料" in t for t in texts), "合格 revise 生效")

    rejected_details = "；".join(o["detail"] for o in res["rejected"])
    check("evidence" in rejected_details or "原话出处" in rejected_details,
          "无证据的 add 被打回")
    check("不存在" in rejected_details or "无此" in rejected_details,
          "对不存在 id 的 supersede 被打回")
    check(any("未知操作" in o["detail"] for o in res["rejected"]), "自创操作被打回")

    review = Path(res["review_path"])
    check(review.exists() and "记忆巩固复盘" in review.read_text(encoding="utf-8"),
          "复盘文档产出")

    # 每轮新增上限：再来一轮全是合格且互不相似的 add
    distinct = ["每周五晚打羽毛球", "咖啡只喝手冲不加糖", "女儿在读小学三年级",
                "计划明年考潜水证", "供应商货款月底统一结", "住处离公司骑车十分钟"]
    many = "[" + ",".join(
        f'{{"op": "add", "text": "{t}", "evidence": "原话{i}"}}'
        for i, t in enumerate(distinct)) + "]"

    async def flood_model(prompt):
        return many

    before = len(profile.list_facts())
    res2 = await consolidation.run_consolidation(flood_model, material=material,
                                                 review_dir=_RDIR)
    added = len(profile.list_facts()) - before
    check(added <= consolidation.MAX_ADDS_PER_RUN,
          f"每轮新增被限量（实际 {added} ≤ {consolidation.MAX_ADDS_PER_RUN}）")
    check(len(res2["rejected"]) >= 2, "超限的 add 被打回而非静默丢弃")

    # 空输出 → 零操作，不报错
    async def empty_model(prompt):
        return "近期没什么值得记的。[]"

    res3 = await consolidation.run_consolidation(empty_model, material=material,
                                                 review_dir=_RDIR)
    check(res3["n_ops"] == 0 and not res3["applied"], "空轮次安全通过")


asyncio.run(_main())

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_consolidation 全部通过")
sys.exit(0)
