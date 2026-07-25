#!/usr/bin/env python3
"""core/trust 数据信任分级 + 污染闸 —— 确定性单测（不依赖模型/联网）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import effects, registry, trust  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. tainting 分类 ──────────────────────────────────────────────────────────
print("[1] tainting 分类")
check(trust.is_tainting("run_workflow"), "跑浏览器工作流 → tainting")
check(trust.is_tainting("read_document"), "读第三方文档 → tainting")
check(trust.is_tainting("weather"), "外部 API → tainting")
check(not trust.is_tainting("recall"), "自我记忆召回 → 可信")
check(not trust.is_tainting("list_credentials"), "本机保险箱清单 → 可信")


async def _fake():
    return "ok"

# 自建技能（origin=skill）一律按最坏假设
registry.register_spec(registry.ToolSpec(
    name="_test_trust_skill", description="测试技能", input_schema={},
    handler=_fake, origin="skill"))
check(trust.is_tainting("_test_trust_skill"), "自建技能一律 tainting（最坏假设）")
registry.unregister("_test_trust_skill")

# ── 2. 污染闸状态机 ───────────────────────────────────────────────────────────
print("[2] 污染闸")
t = trust.TaintTracker()
t.new_user_turn()

ok, _ = t.check("run_workflow")
check(ok, "干净回合里 write_external 放行（危险在污染后，不在动作本身）")

t.absorb("read_document")   # 读了一份第三方文档
check(t.tainted, "读外部内容后回合被标污染")

ok, msg = t.check("run_workflow")
check(not ok, "污染后 write_external 被拦")
check("read_document" in msg, "拦截消息点名污染来源")
check("未执行" in msg, "拦截消息告知未执行")

ok, _ = t.check("remember_fact")
check(ok, "污染后写本机（存记忆）不受影响——危险在「对外」不在「落本机」")
ok, _ = t.check("list_credentials")
check(ok, "污染后只读不受影响")
ok, _ = t.check("delete_document")
check(not ok, "污染后 irreversible（≥write_external）同样被拦")

t.new_user_turn()
check(not t.tainted, "新用户回合污染清零")
ok, _ = t.check("run_workflow")
check(ok, "干净新回合里用户明确指示的对外动作可执行")

# 非 tainting 工具不引入污染
t2 = trust.TaintTracker()
t2.absorb("recall")
t2.absorb("list_credentials")
check(not t2.tainted, "只用可信工具不产生污染")

# 污染来源记首个（多来源时报告第一个引入者）
t3 = trust.TaintTracker()
t3.absorb("weather")
t3.absorb("read_document")
_, msg3 = t3.check("run_workflow")
check("weather" in msg3, "多来源时报告首个污染者")

# ── 3. 与 effects 的协同（lethal trifecta 的完整闭合）────────────────────────
print("[3] 双闸协同")
# trifecta 场景：读外部文档（含注入指令）→ 试图跑对外工作流 → 污染闸拦；
# 即便用户下一轮确认，delete 类还要再过 effects 确认闸——两闸独立叠加。
check(effects.effect_of("run_workflow") == effects.WRITE_EXTERNAL,
      "run_workflow 在 effects 里是 write_external（两套分级一致）")
g = effects.ConfirmGate()
g.new_user_turn()
ok_e, _ = g.check("delete_credential", "{}")
t4 = trust.TaintTracker()
t4.absorb("read_document")
ok_t, _ = t4.check("delete_credential")
check(not ok_e and not ok_t, "删证件在两闸各自都过不去（叠加防御）")

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_trust 全部通过")
sys.exit(0)
