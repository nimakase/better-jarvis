#!/usr/bin/env python3
"""core/drift 漂移感知 + 和解闸 + sensors/health —— 确定性单测（临时 git 仓库）。"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_drift_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

from core import drift  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


# ── 0. 临时 git 仓库 ─────────────────────────────────────────────────────────
REPO = _TMP / "repo"
REPO.mkdir()
_git(REPO, "init", "-q")
_git(REPO, "config", "user.email", "t@t")
_git(REPO, "config", "user.name", "tester")
(REPO / "calc.py").write_text("def const():\n    return 42\n", encoding="utf-8")
_git(REPO, "add", "-A")
_git(REPO, "commit", "-qm", "init")

# ── 1. 工作树脏检测 ───────────────────────────────────────────────────────────
print("[1] 工作树")
check(drift.workdir_dirty(repo=REPO) == [], "干净仓库无脏文件")
(REPO / "calc.py").write_text("def const():\n    return 43\n", encoding="utf-8")
check(drift.workdir_dirty(repo=REPO) == ["calc.py"], "未提交改动被检出")
check(drift.workdir_dirty(paths=["other.py"], repo=REPO) == [], "限定路径过滤生效")
check(drift.workdir_dirty(repo=_TMP) == [], "非 git 目录安静返回空（不炸）")

# ── 2. 外部提交归因 ───────────────────────────────────────────────────────────
print("[2] 外部提交")
drift._STATE = _TMP / "drift_state.json"   # 状态文件隔离

ext0 = drift.external_commits_since_last_look(repo=REPO)
check(ext0 == [], "首次调用只记基线不报旧账")

_git(REPO, "commit", "-aqm", "外部工具改了 calc")          # 外部提交
(REPO / "calc.py").write_text("def const():\n    return 44\n", encoding="utf-8")
_git(REPO, "commit", "-aqm", "self-iter: calc 自改")       # 自我迭代提交

ext1 = drift.external_commits_since_last_look(repo=REPO)
check(len(ext1) == 1 and "外部工具" in ext1[0]["subject"],
      "新提交里只报外部的（self-iter: 前缀被归因为自改）")
check(ext1[0]["author"] == "tester", "外部提交带作者归因")
check(drift.external_commits_since_last_look(repo=REPO) == [],
      "看过即翻篇（同一批不重复报）")

check("calc.py" in drift.recently_touched(days=2, repo=REPO),
      "recently_touched 读真实 git（任何作者）")

# ── 3. 和解闸：self_iteration 对未提交改动让路 ───────────────────────────────
print("[3] 和解闸")
from core.self_iteration import Proposal, SelfIterator  # noqa: E402

(REPO / "tests").mkdir()
(REPO / "tests" / "run_all.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")

it = SelfIterator(repo=REPO, classify=lambda p: ("open", "测试注入"), git_commit=False)
prop = Proposal(path="calc.py", new_code="def const():\n    return 45\n",
                test_code="x" * 50 + "\nimport sys\n# calc\nsys.exit(0)\n",
                category="bugfix", severity="high", defect="d")

# 工作树脏（上面 44 的改动没提交？——已提交。制造新的未提交改动）
(REPO / "calc.py").write_text("def const():\n    return 99  # Ned 手改\n", encoding="utf-8")
out = it.execute(prop)
check(out.status == "needs_human" and "让路" in out.reason,
      "目标文件有未提交外部改动 → 让路转人工，绝不覆盖")
check("99" in (REPO / "calc.py").read_text(encoding="utf-8"),
      "外部改动分毫未动")

_git(REPO, "commit", "-aqm", "Ned 的手改落定")
out2 = it.execute(prop)
check(out2.status != "needs_human" or "让路" not in out2.reason,
      "外部改动提交落定后，和解闸放行（后续走正常先红后绿）")

# ── 4. 冷却合并 git 判据（self_review）──────────────────────────────────────
print("[4] 冷却判据")
from core import self_review  # noqa: E402
check(hasattr(self_review, "run_cycle"), "self_review 可导入（冷却逻辑在 run_cycle 内联）")
# 判据函数本身已在 [2] 验证；这里验证注入不炸——用空 model_fn 跑一轮
import asyncio  # noqa: E402


async def _empty_model(prompt):
    return "[]"

res = asyncio.run(self_review.run_cycle(
    _empty_model, iterator=it, focus_source={}, review_dir=_TMP / "sr",
    module_map="MAP"))
check(res["n_proposals"] == 0, "带 git 冷却的 run_cycle 空轮安全通过")

# ── 5. 健康感官 ───────────────────────────────────────────────────────────────
print("[5] 健康感官")
from sensors import health  # noqa: E402
from core import telemetry, workflow_registry as wr  # noqa: E402
telemetry.init_db()

# 工作流健康：注入运行记录
orig_recent = wr.recent_runs
wr.recent_runs = lambda limit=8: [
    {"id": "signal_collection", "name": "采集今日信号", "status": "failed"},
    {"id": "prospect_daily", "name": "今日潜客名单", "status": "success"},
]
try:
    h = health.collect_workflow_health()
    check("失败" in h.get("工作流", "") and "采集今日信号" in h["工作流"],
          "工作流失败进健康读数")
finally:
    wr.recent_runs = orig_recent

# 工具故障：写遥测再读
telemetry.record("hubspot_session_check", False, 1000, "TimeoutError")
telemetry.record("hubspot_session_check", False, 1200, "TimeoutError")
h2 = health.collect_tool_health()
check("hubspot_session_check" in h2.get("工具故障", ""), "工具故障进健康读数")

# 职责边界：时限类事件（证件/文档到期）归内置日历（时间真源），健康感官不重复实现
check(not hasattr(health, "collect_expiry"),
      "健康感官不含到期采集（归日历 calendar_providers，防重复实现）")
from core import calendar as _cal  # noqa: E402
import connectors.calendar_providers  # noqa: E402, F401
_src_names = {s.get("name") for s in getattr(_cal, "_SOURCES", []) if isinstance(s, dict)}
check("credentials_expiry" in _src_names and "documents_expiry" in _src_names,
      "证件/文档到期在日历来源注册表里（正确的家）")

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_drift_health 全部通过")
sys.exit(0)
