#!/usr/bin/env python3
"""统一测试入口 —— 自我迭代闭环的「测试绿」判据（单一 gate）。

发现并运行 tests/test_*.py（每个都是独立脚本，约定：失败时退出码非零），
聚合结果；任一文件失败则整体退出码非零。后续自我迭代闭环在「自动应用周边改动」
前会调用本入口，绿了才落地、红了就放弃/回滚。

本机用法（仓库根，已装项目依赖）：
    python tests/run_all.py
退出码：0 = 全绿；1 = 有失败。
"""
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
PY = sys.executable


def main() -> int:
    files = sorted(f for f in TESTS.glob("test_*.py"))
    if not files:
        print("没有发现 tests/test_*.py")
        return 1
    failed = []
    for f in files:
        print(f"\n===== {f.name} =====")
        r = subprocess.run([PY, str(f)], cwd=str(REPO))
        if r.returncode != 0:
            failed.append(f.name)
    print("\n" + "=" * 52)
    if failed:
        print(f"❌ {len(failed)}/{len(files)} 个测试文件失败：{failed}")
        return 1
    print(f"✅ 全部 {len(files)} 个测试文件通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
