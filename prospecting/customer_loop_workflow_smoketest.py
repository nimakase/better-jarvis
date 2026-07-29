"""prospecting/customer_loop_workflow_smoketest.py — 端到端跑一次夜间工作流(默认 dry-run)。

真走 customer_loop_nightly 全链路:开会话 → 读全书分级对账 → 出计划 → 飞书交付。
**默认 dry-run(不写)**;要真写,先 `export JARVIS_CUSTOMER_LOOP_APPLY=1`。

    # dry-run(推荐先跑这个;不写):
    python -m prospecting.customer_loop_workflow_smoketest "<视图URL>"

    # 真写(= 第一次正式夜跑;写 460 自动集):
    export JARVIS_CUSTOMER_LOOP_APPLY=1
    python -m prospecting.customer_loop_workflow_smoketest "<视图URL>"

说明:
  - 这是无人值守模式(headed_login=False):若登录态掉了会直接报"未登录",
    此时先跑 `python -m prospecting.breeze_smoketest login` 刷新登录,再来。
  - 飞书未配也没关系,notify 是 best-effort(on_error=skip),不影响主流程。
  - 走的是真实工作流入口(workflow_registry.run),和定时触发同一条路。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

import connectors.customer_loop_tools  # noqa: F401,E402  触发工作流注册
from core import workflow_registry as wr  # noqa: E402


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1].lower().startswith("http"):
        os.environ["JARVIS_GRADE_VIEW_URL"] = sys.argv[1]
    # 不传也行:连接器有默认视图(connectors.customer_loop_tools.DEFAULT_VIEW_URL)
    print("[wf] 使用视图:", connectors.customer_loop_tools._view_url())

    apply = connectors.customer_loop_tools._apply_enabled()   # 读 config(.env)+ os.environ 兜底
    print(f"[wf] 跑 customer_loop_nightly（{'APPLY 真写' if apply else 'dry-run 不写'}）…")

    res = asyncio.run(wr.run("customer_loop_nightly"))
    run = res.get("run", {})
    print("\n结果 ok:", res.get("ok"))
    print("状态:", run.get("status"), "| 耗时:", run.get("seconds"), "s")
    for s in run.get("steps", []):
        print(f"  step {s['name']}: {s['status']}" + (f"  错误:{s['error']}" if s.get("error") else ""))
    ctx = res.get("context") or {}
    if ctx.get("nightly"):
        print("\nnightly 产出:", ctx["nightly"])
    if not res.get("ok"):
        print("\n[wf] ❌ 未成功,看上面 step 错误。")
        return 2
    print("\n[wf] ✅ 工作流跑通。" + ("已写自动集,去 HubSpot 抽查。" if apply else "dry-run,未写。"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
