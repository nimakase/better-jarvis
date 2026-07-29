"""prospecting/show_review.py — 直接在终端打印最新冷启动复核清单(Core-无-deal)。

免得去 Finder 的 Library 里翻文件。

    python -m prospecting.show_review
"""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path


def _dir() -> Path:
    try:
        import config
        return config.DATA_DIR / "customer_loop"
    except Exception:
        return Path(os.path.expanduser("~/.jarvis/customer_loop"))


def main() -> int:
    files = sorted(glob.glob(str(_dir() / "coldstart_plan_*.json")))
    if not files:
        print("没找到冷启动计划文件。先跑一次:python -m prospecting.customer_loop_workflow_smoketest")
        return 1
    f = files[-1]
    data = json.load(open(f, encoding="utf-8"))
    rows = data.get("复核清单_core无deal", [])
    print(f"文件:{f}")
    print(f"共 {len(rows)} 个 Core-无-deal(最死排最前):\n")
    print(f"{'序':>3}  {'最后活动':<26}{'分类':<26}账户")
    print("-" * 90)
    for i, r in enumerate(rows, 1):
        print(f"{i:>3}  {str(r.get('最后活动','')):<26}{str(r.get('分类','')):<26}{r.get('账户','')}")
    print(f"\n(完整文件路径见上;想在 Finder 打开该目录:open \"{_dir()}\")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
