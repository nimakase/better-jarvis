#!/usr/bin/env python3
"""core/permission_scan.py —— 确定性单测（纯 AST，不依赖 config/pydantic）。
跑：python tests/test_permission_scan.py（沙盒/真机均可，无外部依赖）"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import permission_scan as ps  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


print("[1] 自由集：安全子集内的 import 不产生权限标签")
free_code = """
import json
import re
from datetime import datetime
from typing import Optional
import httpx
"""
check(ps.scan_code(free_code) == set(), "全是安全子集 → 空集")

print("[2] 内部模块 import → import:<module> 标签")
internal_code = """
import config
from core import registry
from core.effects import WRITE_LOCAL
"""
tags = ps.scan_code(internal_code)
check("import:config" in tags, "import config → import:config")
check("import:core" in tags, "import core → import:core")
check("import:core.effects" in tags, "from core.effects import X → import:core.effects")

print("[3] building block → 用登记的 key 作标签，不是 import:前缀")
bb_code = """
from prospecting.hubspot_worker import HubSpotBrowser
import prospecting.login_manager as lm
"""
tags2 = ps.scan_code(bb_code)
check("prospecting.hubspot_worker" in tags2, "building block 用登记 key")
check("import:prospecting.hubspot_worker" not in tags2, "building block 不重复标 import:前缀")
check("prospecting.login_manager" in tags2, "as 别名 import 同样识别")

print("[4] 非 building block 的 prospecting 子模块 → 走通用 import:标签")
non_bb_code = "from prospecting import outreach_store\n"
tags3 = ps.scan_code(non_bb_code)
check("import:prospecting.outreach_store" in tags3 or "import:prospecting" in tags3,
      f"未登记的 prospecting 子模块仍标记为需要审的权限点, got {tags3}")

print("[5] 语法错误代码 → 空集，不抛异常")
check(ps.scan_code("def f(:\n  pass") == set(), "语法错误安全返回空集")

print("[6] scan_file 从磁盘读")
with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
    f.write("import subprocess\n")
    fpath = f.name
check(ps.scan_file(fpath) == {"import:subprocess"}, "scan_file 读文件生效")
check(ps.scan_file("/tmp/不存在的文件_xyz123.py") == set(), "读不到文件 → 空集不报错")

print("[7] describe() 可读性")
check("hubspot" in ps.describe("prospecting.hubspot_worker").lower(),
      "building block 的 describe 含模块说明")
check("import" in ps.describe("import:config").lower(), "import:标签的 describe 提示需要审")

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_permission_scan 全部通过")
sys.exit(0)
