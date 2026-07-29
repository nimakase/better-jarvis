"""prospecting/customer_loop_check.py — 无浏览器接线自检(秒级)。

确认:① customer_loop_nightly 工作流是否注册上;② 关键 env 配置;③ 状态库(冷启动是否已应用)。
不开浏览器、不读 HubSpot、不写任何东西。

    python -m prospecting.customer_loop_check
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

# 导入连接器 → 触发工作流注册(平时由 core/registry 自动 import)
import connectors.customer_loop_tools  # noqa: F401,E402
from core import workflow_registry as wr  # noqa: E402
from prospecting import customer_loop_store as store  # noqa: E402


def main() -> int:
    # `reset` 参数:把冷启动开关重置回未应用(想重做全书冷启动时用)
    if len(sys.argv) > 1 and sys.argv[1].lower() == "reset":
        store.reset_coldstart()
        print("已重置:冷启动标记 → 未应用。下次夜跑会重新做全书冷启动对账。")
        return 0

    wfs = wr.list_workflows()
    hit = next((x for x in wfs if x["id"] == "customer_loop_nightly"), None)
    print("① 工作流注册:", "✅ 已注册" if hit else "❌ 未注册")
    if hit:
        print("   ", {k: hit[k] for k in ("name", "dispatch", "confirm", "needs")})

    apply = os.environ.get("JARVIS_CUSTOMER_LOOP_APPLY", "")
    print("② 配置:")
    print("   生效视图 =", connectors.customer_loop_tools._view_url(),
          "(env 未设 → 用默认)" if not os.environ.get("JARVIS_GRADE_VIEW_URL") else "(来自 env)")
    print("   JARVIS_CUSTOMER_LOOP_APPLY=", apply or "(未设 → dry-run,不会真写)")

    print("③ 状态库:")
    print("   冷启动已应用 =", store.is_coldstart_applied())
    lr = store.last_run("coldstart") or store.last_run("incremental")
    print("   最近一次运行 =", lr or "(无)")

    ok = bool(hit)
    print("\n" + ("✅ 接线自检通过(可跑端到端)" if ok else "❌ 工作流没注册上,先看报错"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
