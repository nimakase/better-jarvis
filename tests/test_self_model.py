"""边界不变量测试 —— core/self_model 是整个自我迭代安全模型的基石。

这些断言一旦红，意味着「核心/周边」的护栏被破坏：必须人工查，绝不放行自动迭代。
零重依赖（self_model 只用 pathlib），任何环境可跑。
"""
import sys
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from core import self_model as sm  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# 核心文件 → protected
for p in [
    "core/controller.py", "core/registry.py", "core/tool_builder.py",
    "core/memory.py", "core/safety.py", "core/context.py", "core/results.py",
    "core/workflow.py", "core/scheduler.py", "config.py", "main.py",
    "connectors/vault.py", "connectors/credentials.py", "connectors/cred_ocr.py",
    "web/chat.py", "web/credentials.py",
]:
    check(f"protected: {p}", sm.classify(p)[0] == "protected")

# 周边文件 → open
for p in [
    "core/calendar.py", "core/reports.py", "core/intel_cards.py", "core/history.py",
    "connectors/document.py", "connectors/doc_vault.py", "connectors/calendar_tools.py",
    "intel/signal_library.py", "intel/workflow_defs.py",
    "prospecting/pipeline.py", "prospecting/hubspot_worker.py",
    "skills/weather_query/tool.py",
]:
    check(f"open: {p}", sm.classify(p)[0] == "open")

# —— 三条关键不变量 ——
check("fail-safe: 未知文件 -> protected", sm.classify("core/brand_new_file.py")[0] == "protected")
check("fail-safe: data/ -> protected", sm.classify("data/anything.json")[0] == "protected")
check("不变量: self_model 自身受保护", sm.classify("core/self_model.py")[0] == "protected")
check("不变量: tests/ 受保护(防作弊)", sm.classify("tests/test_x.py")[0] == "protected")

# is_writable_by_self_iteration 只对 open 为真
check("可写仅 open: calendar.py", sm.is_writable_by_self_iteration("core/calendar.py") is True)
check("不可写: controller.py", sm.is_writable_by_self_iteration("core/controller.py") is False)
check("不可写: vault.py", sm.is_writable_by_self_iteration("connectors/vault.py") is False)
check("不可写: tests/", sm.is_writable_by_self_iteration("tests/t.py") is False)
check("不可写: self_model", sm.is_writable_by_self_iteration("core/self_model.py") is False)
check("不可写: 未知路径", sm.is_writable_by_self_iteration("whatever/x.py") is False)

# 绝对路径与仓库外路径都不应崩
check("绝对路径可归类", sm.classify(JARVIS + "/core/controller.py")[0] == "protected")
check("仓库外路径不崩", sm.classify("/etc/passwd")[0] in ("protected", "open"))

# PROTECTED 与 OPEN 不应有重叠 key
overlap = set(sm.PROTECTED) & set(sm.OPEN)
check("PROTECTED/OPEN 无重叠", not overlap)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
