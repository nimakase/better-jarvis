#!/usr/bin/env python3
"""core/effects 动作效应模型 + 确认闸 —— 确定性单测（不依赖模型/联网）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import effects  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. 等级与序数 ─────────────────────────────────────────────────────────────
print("[1] 等级定义")
check(effects.LEVELS == ["read_local", "read_external", "write_local",
                         "write_external", "irreversible"], "五级顺序正确")
check(effects.rank("read_local") < effects.rank("irreversible"), "序数单调")
check(effects.rank("不存在的等级") == effects.rank("irreversible"),
      "未知等级按最高危对待（fail-safe）")
check(effects.at_least("write_external", "write_local"), "at_least 正向")
check(not effects.at_least("read_local", "write_local"), "at_least 反向")

# ── 2. 分级查询 ───────────────────────────────────────────────────────────────
print("[2] 分级查询")
check(effects.effect_of("delete_credential") == effects.IRREVERSIBLE,
      "删证件 → irreversible")
check(effects.effect_of("delete_tool") == effects.IRREVERSIBLE,
      "删工具 → irreversible")
check(effects.effect_of("calendar_delete_event") == effects.IRREVERSIBLE,
      "删日历事件 → irreversible")
check(effects.effect_of("list_credentials") == effects.READ_LOCAL,
      "列证件 → read_local")
check(effects.effect_of("run_workflow") == effects.WRITE_EXTERNAL,
      "工作流 → write_external")
check(effects.effect_of("从未见过的工具xyz") == effects.DEFAULT_EFFECT,
      "未知工具 → 默认等级（不误触闸，也不算只读）")

# ── 2b. 注册声明优先于内置表 ──────────────────────────────────────────────────
print("[2b] 注册声明优先")
from core import registry  # noqa: E402


async def _fake():
    return "ok"

registry.register_spec(registry.ToolSpec(
    name="_test_effects_declared", description="测试用", input_schema={},
    handler=_fake, effect=effects.IRREVERSIBLE))
check(effects.effect_of("_test_effects_declared") == effects.IRREVERSIBLE,
      "ToolSpec.effect 声明生效")
registry.unregister("_test_effects_declared") or registry._SPECS.pop("_test_effects_declared", None)

# ── 3. 确认闸：核心状态机 ─────────────────────────────────────────────────────
print("[3] 确认闸")
g = effects.ConfirmGate()
g.new_user_turn()  # 用户回合 1

ok, msg = g.check("delete_document", '{"name":"合同A"}')
check(not ok, "首次调用不可逆工具 → 拦下")
check("未执行" in msg, "拦截消息告知未执行")

ok2, _ = g.check("delete_document", '{"name":"合同A"}')
check(not ok2, "同一回合内重调仍拦（必须隔用户回合，防模型自问自答绕闸）")

g.new_user_turn()  # 用户回合 2（用户已看到复述并回话）
ok3, _ = g.check("delete_document", '{"name":"合同A"}')
check(ok3, "隔一个用户回合后同名同参 → 放行")

ok4, _ = g.check("delete_document", '{"name":"合同A"}')
check(not ok4, "票据一次性：放行后立即重调 → 重新拦")

# 参数变化 = 新动作，必须重新确认
g2 = effects.ConfirmGate()
g2.new_user_turn()
g2.check("delete_document", '{"name":"合同A"}')
g2.new_user_turn()
ok5, _ = g2.check("delete_document", '{"name":"合同B"}')
check(not ok5, "参数不同 → 视为新动作，重新拦")

# 授权过期：拦下后隔了两个用户回合才调 → 作废
g3 = effects.ConfirmGate()
g3.new_user_turn()
g3.check("delete_tool", '{"name":"x"}')
g3.new_user_turn()   # 晋级 granted
g3.new_user_turn()   # granted 过期
ok6, _ = g3.check("delete_tool", '{"name":"x"}')
check(not ok6, "授权只活一个回合，陈年授权不复活")

# 非不可逆工具完全不受影响
g4 = effects.ConfirmGate()
ok7, msg7 = g4.check("list_credentials", "{}")
check(ok7 and msg7 == "", "非 irreversible 工具直接放行（零行为变化）")

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_effects 全部通过")
sys.exit(0)
