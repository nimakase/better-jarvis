#!/usr/bin/env python3
"""长耗时工作流收编（detach 派发）+ 子 agent 模型开关 —— 确定性单测。"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_dispatch_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. 注册表 dispatch 字段 ───────────────────────────────────────────────────
print("[1] 注册表")
from core import workflow_registry as wr  # noqa: E402
from core import workflow as wf  # noqa: E402
import intel.workflow_defs  # noqa: E402, F401  （生产由 main 启动装载；测试手动触发注册）

check(wr.get("signal_collection") and wr.get("signal_collection")["dispatch"] == "detach",
      "signal_collection 已标 detach")
check(wr.get("prospect_daily") and wr.get("prospect_daily")["dispatch"] == "detach",
      "prospect_daily 已标 detach")

wr.register_workflow("_test_sync", "同步测试流", "x",
                     lambda: [wf.Step("s", lambda ctx: {"ok": 1})], confirm=False)
check(wr.get("_test_sync")["dispatch"] == "sync", "默认 dispatch=sync（老行为不变）")

# ── 2. detach 派发即返回 + 完成推送 ──────────────────────────────────────────
print("[2] detach 派发")
import connectors.workflow_tools as wt  # noqa: E402
from core import artifacts  # noqa: E402
artifacts.LIBRARY_DIR = _TMP / "图书馆"
artifacts.init_db()

delivered = []


async def _main():
    # 注册一条慢工作流：跑 0.2s 后产出一个文件
    out_file = _TMP / "名单.txt"

    async def slow_step(ctx):
        await asyncio.sleep(0.2)
        out_file.write_text("产出", encoding="utf-8")
        return {"path": str(out_file)}

    wr.register_workflow("_test_slow", "慢测试流", "x",
                         lambda: [wf.Step("output", slow_step)],
                         confirm=False, dispatch="detach")

    # 截获 delivery
    from core import delivery
    orig_deliver = delivery.deliver

    def fake_deliver(track, title, content, severity="normal", **kw):
        delivered.append({"track": track, "title": title, "severity": severity,
                          "attachments": kw.get("attachments")})
        return {"delivered": True}

    delivery.deliver = fake_deliver
    try:
        import time as _t
        t0 = _t.monotonic()
        msg = await wt.run_workflow("_test_slow")
        elapsed = _t.monotonic() - t0
        check(elapsed < 0.15, f"派发立即返回（{elapsed*1000:.0f}ms），主对话不被扣押")
        check("已派发" in msg and "推送" in msg, "返回派发说明")
        check("_test_slow" in wt._RUNNING, "运行集登记")

        # 跑着的时候重复派发 → 拒绝
        msg2 = await wt.run_workflow("_test_slow")
        check("已经在后台跑" in msg2, "重复派发被拒（防并发双跑）")

        await asyncio.sleep(0.5)   # 等后台跑完
        check("_test_slow" not in wt._RUNNING, "跑完从运行集移除")
        # 收敛后契约（2026-07-25）：成功不再由框架 _notify_done 发卡——业务卡由工作流
        # 步骤自己发，避免"一次运行两条飞书"。_test_slow 步骤不自发 → 框架也不发 → 零投递。
        check(len(delivered) == 0, "成功不再由框架代发卡（重复投递已消除）")

        regs = artifacts.list_artifacts(producer="workflow:_test_slow")
        check(len(regs) == 1, "产出仍登记进图书馆（producer=workflow:…）")

        # 双发守卫：工作流步骤自己发一次业务卡时，框架不叠加第二次 → 全程恰好一条
        out_file2 = _TMP / "名单2.txt"

        async def self_deliver_step(ctx):
            out_file2.write_text("产出2", encoding="utf-8")
            delivery.deliver("_test_selfdeliver", "业务卡", "内容",
                             severity="normal", attachments=[str(out_file2)])
            return {"path": str(out_file2)}

        wr.register_workflow("_test_selfdeliver", "自发测试流", "x",
                             lambda: [wf.Step("output", self_deliver_step)],
                             confirm=False, dispatch="detach")
        await wt.run_workflow("_test_selfdeliver")
        await asyncio.sleep(0.3)
        sd = [d for d in delivered if d["track"] == "_test_selfdeliver"]
        check(len(sd) == 1, "步骤自发一次 + 框架不叠加 = 恰好一条（无重复投递）")

        # 失败的 detach 流 → high 级兜底告警（步骤没来得及发，靠框架兜底、不静默）
        async def boom(ctx):
            raise RuntimeError("炸了")

        wr.register_workflow("_test_boom", "失败测试流", "x",
                             lambda: [wf.Step("s", boom)],
                             confirm=False, dispatch="detach")
        await wt.run_workflow("_test_boom")
        await asyncio.sleep(0.3)
        check(any(d["severity"] == "high" and d["track"] == "_test_boom"
                  for d in delivered), "失败 → high 级兜底告警（不静默）")

        # 同步流老路径不变
        msg3 = await wt.run_workflow("_test_sync")
        check("运行结束" in str(msg3), "sync 工作流仍同步返回结果")

        # 状态工具
        st = await wt.workflow_status()
        check("没有在跑" in st or "正在后台" in st, "workflow_status 可查")
    finally:
        delivery.deliver = orig_deliver


asyncio.run(_main())

# ── 3. 子 agent 模型开关 ──────────────────────────────────────────────────────
print("[3] 模型开关")
from core import spawn  # noqa: E402

os.environ.pop("JARVIS_SUBAGENT_MODEL", None)
check(spawn._subagent_model() == "", "未配置时空串（= 跟主模型）")
os.environ["JARVIS_SUBAGENT_MODEL"] = "deepseek/cheap-model"
check(spawn._subagent_model() == "deepseek/cheap-model", "env 覆盖生效")
os.environ.pop("JARVIS_SUBAGENT_MODEL", None)

if not getattr(config, "OPENROUTER_API_KEY", None):
    config.OPENROUTER_API_KEY = "test-key"
for _v in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
           "HTTP_PROXY", "http_proxy"):
    os.environ.pop(_v, None)
from core.controller import JarvisController  # noqa: E402

c1 = JarvisController(interactive=False)
check(c1.model == config.CLAUDE_MODEL, "默认用全局主模型")
c2 = JarvisController(interactive=False, model="x/y-model")
check(c2.model == "x/y-model", "实例级覆盖生效")

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_workflow_dispatch 全部通过")
sys.exit(0)
