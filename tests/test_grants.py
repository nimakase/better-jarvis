#!/usr/bin/env python3
"""core/grants.py（权限授权记录 + audit_registry）—— 确定性单测（隔离临时库）。
跑：.venv/bin/python tests/test_grants.py"""
import sys
import tempfile
import importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_grants_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

from core import grants  # noqa: E402
grants.init_db()

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


print("[1] grant/get/list/revoke 生命周期")
r = grants.grant("fake.module.a", {"import:config", "prospecting.hubspot_worker"},
                  "code-v1", "Ned", source_path="/x/a.py", note="测试")
check(r["ok"], "grant 返回 ok")
g = grants.get_grant("fake.module.a")
check(g["permissions"] == ["import:config", "prospecting.hubspot_worker"], "权限点排序落库")
check(g["approved_by"] == "Ned", "批准人落库")
check(g["note"] == "测试", "备注落库")
check(len(grants.list_grants()) == 1, "list_grants 能查到")
rr = grants.revoke("fake.module.a")
check(rr["ok"], "revoke 成功")
check(grants.get_grant("fake.module.a") is None, "撤销后查不到")
check(grants.revoke("fake.module.a")["ok"] is False, "撤销不存在的记录返回 ok=False")

print("[2] is_covered 逻辑")
grants.grant("fake.module.b", {"import:x"}, "code-v1", "Ned")
covered, reason = grants.is_covered("fake.module.b", {"import:x"}, "code-v1")
check(covered, "完全匹配 → covered")
covered2, reason2 = grants.is_covered("fake.module.b", {"import:x"}, "code-v2")
check(not covered2 and "hash" in reason2, f"代码变了(即使权限没变)→需要重新审批, got {reason2!r}")
covered3, reason3 = grants.is_covered("fake.module.b", {"import:x", "import:y"}, "code-v1")
check(not covered3 and "import:y" in reason3, f"多用了未授权权限→不覆盖, got {reason3!r}")
covered4, reason4 = grants.is_covered("fake.module.c_never_granted", set(), "any")
check(not covered4 and "未审批" in reason4, "从没批准过 → 未审批")

print("[3] audit_registry 与真实注册表联动")
tmp_mod_path = _TMP / "fake_tool_module.py"
tmp_mod_path.write_text(
    "import config\nfrom prospecting.hubspot_worker import HubSpotBrowser\n\n"
    "async def fake_handler(x: str) -> str:\n    return x\n",
    encoding="utf-8")
spec = importlib.util.spec_from_file_location("jarvis_test_fake_tool_module", tmp_mod_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from core import registry  # noqa: E402
registry.register_spec(registry.ToolSpec(
    name="_test_fake_tool", description="测试用",
    input_schema={"type": "object", "properties": {}},
    handler=mod.fake_handler, group="general", origin="skill"))

entries = grants.audit_registry()
entry = next((e for e in entries if e["module"] == "jarvis_test_fake_tool_module"), None)
check(entry is not None, "audit_registry 找到了刚注册的假工具所在模块")
check(entry is not None and "_test_fake_tool" in entry["tools"], "tools 列表含工具名")
check(entry is not None and "import:config" in entry["detected_permissions"], "检出 import:config")
check(entry is not None and "prospecting.hubspot_worker" in entry["detected_permissions"],
      "检出 building block")
check(entry is not None and entry["covered"] is False, "还没批准 → 未覆盖")

code_text = tmp_mod_path.read_text(encoding="utf-8")
grants.grant("jarvis_test_fake_tool_module", entry["detected_permissions"], code_text, "Ned")
entries2 = grants.audit_registry()
entry2 = next(e for e in entries2 if e["module"] == "jarvis_test_fake_tool_module")
check(entry2["covered"] is True, "批准后 → 覆盖")
check(entry2["newly_added"] == [], "覆盖后 newly_added 为空")

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_grants 全部通过")
sys.exit(0)
