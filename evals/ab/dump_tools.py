"""
步骤 1：先看真实注册的工具名与分组，用来对齐评测集里的 expected_tools。
在仓库根目录跑：  python evals/ab/dump_tools.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 把仓库根加入 import 路径

import config
from core import registry

registry.discover_connectors()

g = registry.groups()
total = 0
for grp, names in g.items():
    print(f"[{grp}] ({len(names)})")
    for n in names:
        print("   ", n)
        total += 1
print("-" * 40)
print("TOTAL TOOLS:", total)
print("CORE_TOOL_NAMES:", config.CORE_TOOL_NAMES)
print("PROGRESSIVE_TOOLS(当前默认):", config.PROGRESSIVE_TOOLS)