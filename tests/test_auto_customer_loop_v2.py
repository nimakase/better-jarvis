#!/usr/bin/env python3
"""客户循环 v1→v2 迁移(按轮数拆 view + customer_loop_tools 切换)——自动生成，绝不覆盖既有测试文件。

背景：用户在另一个对话里跟贾维斯规划了一次迁移——customer_loop_nightly 工作流从
v1(prospecting.nightly.run，直接写 HubSpot priority/type 属性）切到 v2
(prospecting.view_manager.run_view_cycle，名单法写私有 view 成员 + Bitable，不碰
HubSpot 属性）；随后又把 view 从按状态(开发中/待处理·换人或放弃/已回复·待跟进)
拆成按轮数(未开发/发1轮/发2轮/发3轮/已回复)。贾维斯当时没有直接改第一方代码的
能力，只能反复绕圈子最终撞上 DeepSeek 空补全崩溃，这次由 Claude 直接落地。

覆盖：
  1. outreach_state.view_for()：replied/reply_pending 优先级、按轮数 0/1/2/3/>3/
     负数(异常兜底)的映射。
  2. outreach_store.accounts_by_view()：非 core 记录按 state+rounds_done 现算
     （不信任存量 view 字段）；core 记录(state=="core")仍用自身 view 字段。
  3. customer_loop_tools._render_cycle_notification()：error/skipped/正常三种
     分支的文案与严重度，正常分支里各类可选提示(回复提议/核维护/breeze失败/
     core无deal/处置解读/缺名/bitable失败)分别触发与不触发。
  4. 接线检查：make_customer_loop_runtime 源码里已经在用 view_manager.run_view_cycle
     而不是 nightly.run；register_workflow 的描述文案不再提 priority。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["JARVIS_CUSTOMER_LOOP_DIR"] = tempfile.mkdtemp()  # 隔离 store 目录

import config  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


from prospecting import outreach_state  # noqa: E402
from prospecting import outreach_store  # noqa: E402
import connectors.customer_loop_tools as clt  # noqa: E402

# ── 1. outreach_state.view_for() ──────────────────────────────────────────────
print("[1] view_for() 映射")

check(outreach_state.view_for("replied") == "已回复", "replied → 已回复")
check(outreach_state.view_for("replied", 3) == "已回复", "replied 优先级最高，不管轮数")
check(outreach_state.view_for("reply_pending") == "待分类回复", "reply_pending → 待分类回复")
check(outreach_state.view_for("reply_pending", 2) == "待分类回复", "reply_pending 优先于轮数")
check(outreach_state.view_for("not_started", 0) == "未开发", "0 轮 → 未开发")
check(outreach_state.view_for("sequencing", None) == "未开发", "rounds_done=None → 按 0 算 → 未开发")
check(outreach_state.view_for("sequencing", 1) == "发1轮", "1 轮 → 发1轮")
check(outreach_state.view_for("round_due", 1) == "发1轮", "round_due 同 sequencing 归段")
check(outreach_state.view_for("sequencing", 2) == "发2轮", "2 轮 → 发2轮")
check(outreach_state.view_for("exhausted", 3) == "发3轮", "3 轮 → 发3轮")
check(outreach_state.view_for("exhausted", 4) == "发3轮", "超过 3 轮就近兜底按 3 算，不凭空造发4轮")
check(outreach_state.view_for("exhausted", 999) == "发3轮", "异常大值同样兜底按 3 算")
check(outreach_state.view_for("weird_state", -1) == "未开发", "负数 rounds_done 兜底按 0 算 → 未开发")


# ── 2. outreach_store.accounts_by_view() ──────────────────────────────────────
print("[2] accounts_by_view() 现算 vs 信任存量")

outreach_store.upsert_account_state("Acme Inc", {"state": "exhausted", "rounds_done": 3, "view": "过期旧值"})
outreach_store.upsert_account_state("Beta LLC", {"state": "sequencing", "rounds_done": 1, "view": "过期旧值"})
outreach_store.upsert_account_state("CoreCo", {"state": "core", "view": "维护到点"})
outreach_store.upsert_account_state("CoreCo2", {"state": "core", "view": None})  # 未到维护点，view=None

by = outreach_store.accounts_by_view()
check("Acme Inc" in by.get("发3轮", []), f"非 core 记录按 state+rounds_done 现算(不信旧 view), got {by}")
check("Beta LLC" in by.get("发1轮", []), "另一条非 core 记录同样现算正确")
check("过期旧值" not in by, "存量里的过期 view 字段完全不出现在结果里")
check("CoreCo" in by.get("维护到点", []), "core 记录信任自身 view 字段(不经 view_for)")
check(all("CoreCo2" not in v for v in by.values()), "core 记录 view=None(未到维护点)不进任何段")


# ── 3. customer_loop_tools._render_cycle_notification() ───────────────────────
print("[3] _render_cycle_notification()")

content, sev = clt._render_cycle_notification({"error": "未登录或会话不可用"})
check(sev == "high" and "未完成" in content and "未登录" in content, "error 分支：高严重度+原因")

content, sev = clt._render_cycle_notification({"skipped": "读取不完整,跳过本次(重跑)", "read": "80/100"})
check(sev == "high" and "跳过" in content and "80/100" in content, "skipped 分支：高严重度+读取进度")

base = {
    "prospecting_total": 100, "core_total": 20,
    "processed_prospecting": 5, "processed_core": 2,
    "cumulative_segments": {"未开发": 40, "发1轮": 10},
    "applied_mode": "dry-run",
}
content, sev = clt._render_cycle_notification(base)
check(sev == "normal", "正常分支：普通严重度")
check("5/100" in content and "2/20" in content, "处理进度数字正确(本轮/总数)")
check("未开发 40" in content and "发1轮 10" in content, "累计各段文案正确")
check("dry-run" in content, "dry-run 模式如实标注")
check("待你确认" not in content and "维护提醒点" not in content and "Breeze 查询失败" not in content,
      "没触发的可选提示一条都不出现(不制造噪音)")

full = {
    **base, "applied_mode": "APPLY",
    "reply_proposals": 2, "core_due": 3,
    "breeze_errors": [{"account": "X", "error": "exc:TimeoutError"}],
    "core_no_deal_fyi": ["Y Corp"],
    "dispositions_understood": [{"account": "Z", "intent": "pause"}],
    "nameless_count": 1,
    "bitable": {"error": "写入超时"},
}
content2, sev2 = clt._render_cycle_notification(full)
check("已写" in content2, "APPLY 模式如实标注")
check("2 条回复待你确认" in content2, "回复提议数提示")
check("3 个 Core 账户到维护提醒点" in content2, "core 维护到点提示")
check("1 个账户 Breeze 查询失败" in content2, "breeze 失败数提示")
check("1 个 Core 账户没有 deal" in content2, "core 无 deal FYI 提示")
check("读懂了 1 条你写的处置备注" in content2, "处置解读数提示")
check("1 个账户导入缺名" in content2, "缺名数提示")
check("Bitable 写入失败" in content2 and "写入超时" in content2, "bitable 失败原因透出")


# ── 4. 接线检查 ─────────────────────────────────────────────────────────────────
print("[4] 接线检查")
import inspect  # noqa: E402
from core import workflow_registry as wr  # noqa: E402

src = inspect.getsource(clt.make_customer_loop_runtime)
check("run_view_cycle" in src, "make_customer_loop_runtime 源码里已经在用 run_view_cycle(v2)")
check("nightly.run(" not in src, "不再调用 nightly.run(v1)")
check("VIEW_URL_MAP" in src, "接入了 view_config.VIEW_URL_MAP")

wf_info = wr.get("customer_loop_nightly")
check(wf_info is not None, "customer_loop_nightly 工作流确实已注册")
if wf_info is not None:
    check("priority" not in wf_info.get("description", ""),
          "工作流描述文案不再提 priority(v1 遗留措辞已清)")
    check(wf_info.get("dispatch") == "detach", "仍是 detach 派发(长耗时不占对话)")


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_customer_loop_v2 全部通过")
sys.exit(0)
