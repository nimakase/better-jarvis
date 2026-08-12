#!/usr/bin/env python3
"""core/grants.check_tool()（⑤二期：运行时权限拦截闸的全部判断逻辑）——
确定性单测（隔离临时库）。跑：.venv/bin/python tests/test_grants_enforcement.py

只测 check_tool() 本身（controller.py 里只是十几行胶水，调这个函数），覆盖：
  1. 未注册工具名 → 放行（不是本闸职责，交 _execute_tool 的"未知工具"分支）。
  2. ALWAYS_ALLOWED_TOOLS（审批工具自身）→ 无条件放行，即便它们所在模块从未审批
     过——这是为了避免"权限管理工具自己被权限闸锁住"的自锁场景。
  3. 一个动态加载(不进 sys.modules，模拟 skills/ 加载方式)的工具：未审批时拦截，
     审批后放行，代码变化后重新拦截，撤销后再次拦截——与 test_permission_tools.py
     里对 audit/approve 链路的验证呼应，这里验证的是"闸门"这一端读到的结果一致。
"""
import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_grants_enforce_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

tmp_mod_path = _TMP / "fake_gate_conn.py"
tmp_mod_path.write_text(
    "import config\nfrom core.results import ToolResult\n\n"
    "async def fake_gate_handler(x: str) -> str:\n    return x\n",
    encoding="utf-8")
spec = importlib.util.spec_from_file_location("jarvis_test_fake_gate_conn", tmp_mod_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from core import registry  # noqa: E402
registry.register_spec(registry.ToolSpec(
    name="_test_fake_gate_tool", description="测试用",
    input_schema={"type": "object", "properties": {}},
    handler=mod.fake_gate_handler, group="general", origin="skill"))

from core import grants  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"❌ {msg}")
    else:
        print(f"✅ {msg}")


async def _main():
    # 1. 未注册工具名 → 放行
    allowed, msg = grants.check_tool("_totally_unregistered_tool_xyz")
    check(allowed, "未注册工具名不是本闸职责，放行")

    # 2. 审批工具自身 → 始终放行（即便它们自己所在模块从未被审批）
    for name in grants.ALWAYS_ALLOWED_TOOLS:
        allowed, msg = grants.check_tool(name)
        check(allowed, f"权限管理工具 {name} 自身永远放行（防自锁）")

    # 3a. 从未审批过的动态加载工具 → 拦截
    allowed, msg = grants.check_tool("_test_fake_gate_tool")
    check(not allowed, "未审批模块的工具被拦截")
    check("权限闸拦截" in msg and "jarvis_test_fake_gate_conn" in msg,
          "拦截提示包含模块名，方便定位")

    # 3b. 批准后 → 放行
    code = tmp_mod_path.read_text(encoding="utf-8")
    from core import permission_scan
    detected = permission_scan.scan_code(code)
    grants.grant("jarvis_test_fake_gate_conn", detected, code, approved_by="Ned",
                  source_path=str(tmp_mod_path), note="单测批准")
    allowed, msg = grants.check_tool("_test_fake_gate_tool")
    check(allowed, "批准覆盖当前代码后放行")

    # 3c. 代码变化 → 重新拦截（即便新代码用到的权限点更少）
    tmp_mod_path.write_text(
        "async def fake_gate_handler(x: str) -> str:\n    return x\n",
        encoding="utf-8")
    allowed, msg = grants.check_tool("_test_fake_gate_tool")
    check(not allowed, "代码变化后（即使只是少用了权限点）重新拦截")
    check("重新审批" in msg, "拦截提示说明是代码变化导致")

    # 3d. 撤销后 → 再次拦截
    code2 = tmp_mod_path.read_text(encoding="utf-8")
    detected2 = permission_scan.scan_code(code2)
    grants.grant("jarvis_test_fake_gate_conn", detected2, code2, approved_by="Ned")
    allowed, _ = grants.check_tool("_test_fake_gate_tool")
    check(allowed, "重新批准新版代码后放行")
    grants.revoke("jarvis_test_fake_gate_conn")
    allowed, msg = grants.check_tool("_test_fake_gate_tool")
    check(not allowed, "撤销授权后重新拦截")


asyncio.run(_main())

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_grants_enforcement 全部通过")
sys.exit(0)
