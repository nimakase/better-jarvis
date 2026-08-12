#!/usr/bin/env python3
"""过程记忆(core/procedures.py) —— 确定性单测（隔离临时库）。
跑：.venv/bin/python tests/test_procedures.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_procedures_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

from core import procedures  # noqa: E402
procedures.init_db()

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


print("[1] 生命周期")
r1 = procedures.add_procedure(
    "pydantic ModuleNotFoundError（沙盒跑测试时）",
    "沙盒 python3 没装 pydantic，是环境限制不是代码 bug；改用真机 .venv/bin/python 跑",
    evidence="2026-08 步骤①②反复遇到，见项目记忆 jarvis-architecture-migration-plan")
check(r1["ok"], "带依据新增")
pid = r1["id"]

procs = procedures.list_procedures()
check(procs[0]["evidence"].startswith("2026-08"), "evidence 落库")
check(procs[0]["times_confirmed"] == 1, "新增即计 1 次确认")

procedures.confirm_procedure(pid)
check(procedures.list_procedures()[0]["times_confirmed"] == 2, "confirm 累加确认次数")

r_dup = procedures.add_procedure(
    "pydantic ModuleNotFoundError（沙盒跑测试时）",
    "随便什么解法")
check(r_dup["id"] == pid and "已存在" in r_dup["message"], "完全相同 problem 降级为确认")

r2 = procedures.add_procedure("某个会被软删的问题", "某个解法")
check(r2["ok"], "第二条新增成功")
procedures.supersede_procedure(r2["id"], reason="测试软删")
check(len(procedures.list_procedures()) == 1, "软删后不出现在活跃清单")
check(len(procedures.list_procedures(include_superseded=True)) == 2, "软删的仍可查回")
check(procedures.count() == 1, "软删不占 MAX_PROCEDURES 名额")
check("测试软删" in [p for p in procedures.list_procedures(include_superseded=True)
                    if p["id"] == r2["id"]][0]["supersede_reason"], "软删理由留档")

procedures.restore_procedure(r2["id"])
check(len(procedures.list_procedures()) == 2, "restore 恢复软删经验")

procedures.update_procedure(pid, method="改进后的解法：优先看 python -c 'import pydantic' 是否报错")
check("改进后的解法" in procedures.list_procedures()[0]["method"], "update_procedure 只改 method")
check(procedures.list_procedures()[0]["problem"].startswith("pydantic"), "update_procedure 未误改 problem")

print("[2] 空/非法输入")
check(procedures.add_procedure("", "有解法没问题")["ok"] is False, "problem 空 → 拒绝")
check(procedures.add_procedure("有问题没解法", "")["ok"] is False, "method 空 → 拒绝")

print("[3] build_index / recall（渐进式披露）")
idx = procedures.build_index()
check("过程记忆索引" in idx, "索引标题存在")
check(f"#{pid}" in idx, "索引含条目编号")
check("改进后的解法" not in idx, "索引只含问题摘要，不含完整解法（细节按需展开）")

hits = procedures.recall("pydantic 报错")
check(len(hits) >= 1 and hits[0]["id"] == pid, "recall 按相关性查到对应条目")
check(procedures.recall("完全不相关的西班牙语查询xyz") == [], "recall 无关查询返回空列表")
check(len(procedures.recall("")) == len(procedures.list_procedures()), "recall 空 query 返回全部（截断到 limit）")

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_procedures 全部通过")
sys.exit(0)
