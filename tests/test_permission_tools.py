#!/usr/bin/env python3
"""connectors/permission_tools.py（审计/批准/撤销/列出 权限授权 的对话工具）——
确定性单测（隔离临时库）。跑：.venv/bin/python tests/test_permission_tools.py"""
import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_permtools_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

# 造一个假的"已注册工具"，模拟 skills/ 动态加载(不进 sys.modules)那种情况，
# 用来验证审计/批准链路不依赖 sys.modules 反查——这正是本轮开发中真发现过的坑。
tmp_mod_path = _TMP / "fake_conn.py"
tmp_mod_path.write_text(
    "import config\nfrom core.results import ToolResult\n\n"
    "async def fake_handler2(x: str) -> str:\n    return x\n",
    encoding="utf-8")
spec = importlib.util.spec_from_file_location("jarvis_test_fake_conn", tmp_mod_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from core import registry  # noqa: E402
registry.register_spec(registry.ToolSpec(
    name="_test_fake_conn_tool", description="测试用",
    input_schema={"type": "object", "properties": {}},
    handler=mod.fake_handler2, group="general", origin="skill"))

import connectors.permission_tools as pt  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


async def _main():
    out1 = await pt.audit_permission_grants(only_uncovered=True)
    check("jarvis_test_fake_conn" in out1, "audit 工具列出假模块")
    check("core.results" in out1, "audit 工具展示 building block")
    check("import config" in out1, "audit 工具展示 import:config 权限点(渲染成人话)")

    out_full = await pt.audit_permission_grants(only_uncovered=False)
    check("jarvis_test_fake_conn" in out_full, "only_uncovered=False 也能看到")

    out2 = await pt.approve_permission_grant("jarvis_test_fake_conn", note="单测批准")
    check("已记录批准" in out2, f"approve 工具成功, got {out2!r}")

    out3 = await pt.list_permission_grants()
    check("jarvis_test_fake_conn" in out3, "list 工具显示已批准记录")
    check("单测批准" in out3, "list 工具显示备注")

    out4 = await pt.audit_permission_grants(only_uncovered=True)
    check("jarvis_test_fake_conn" not in out4, "批准后 only_uncovered 视图里不再出现")

    # 批准后代码若发生变化(即便变得更少)，也要重新走审批——不允许"变严格就自动放行"
    tmp_mod_path.write_text(
        "import config\n\nasync def fake_handler2(x: str) -> str:\n    return x\n",
        encoding="utf-8")
    out5 = await pt.audit_permission_grants(only_uncovered=True)
    check("jarvis_test_fake_conn" in out5, "代码变化(即使只是少用了一个权限点)后需要重新审批")

    out6 = await pt.revoke_permission_grant("jarvis_test_fake_conn")
    check("已撤销" in out6, "revoke 工具成功")
    out7 = await pt.revoke_permission_grant("jarvis_test_fake_conn")
    check("本来就没有" in out7, "撤销不存在的授权给出合理提示而非报错")

    out8 = await pt.approve_permission_grant("不存在的模块.xyz")
    check("批准失败" in out8, "批准一个注册表里查不到的模块名，给出明确失败原因而非崩溃")


asyncio.run(_main())

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_permission_tools 全部通过")
sys.exit(0)
