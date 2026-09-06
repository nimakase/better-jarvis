#!/usr/bin/env python3
"""能力边界与求助纪律 —— system prompt 硬规则存在性 · 确定性单测。

收敛哲学落地：把"何时自己上、何时停下交接"从口头共识变成 prompt 里的规则。
本测试钉死该块每轮都被注入、且承载关键条款（交接而非 spawn、机械触发、四段简报）。
"""
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


from core import controller  # noqa: E402

print("[1] 常量非空 + 关键条款")
pol = controller.ESCALATION_POLICY
check(bool(pol.strip()), "ESCALATION_POLICY 非空")
check("交接" in pol, "含『交接』（升级=交接给 Ned，非自动 spawn）")
check("spawn" in pol and "不是" in pol, "明确 spawn 不是难活的出口")
check("PROTECTED" in pol, "机械触发含『改 PROTECTED 核心文件』")
check("根因" in pol, "交接简报要求含根因（对齐『先诊断再动手』）")

print("[2] 每轮注入 system prompt")
sysprompt = controller._build_system_prompt()
check("【能力边界与求助纪律】" in sysprompt, "该块出现在拼好的 system prompt 里")
check(pol in sysprompt, "整段被完整注入")

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_escalation_policy 全部通过")
sys.exit(0)
