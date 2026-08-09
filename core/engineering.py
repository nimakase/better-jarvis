"""
core/engineering.py — 计划模式执行器（阶段 4 · 自身受保护）

让贾维斯能安全地执行"人已经想清楚、内容已确定的多文件改动"——不是自主生成
提案（那是 self_iteration/self_review 的职责，服务于"不确定提案是否有效，
需要先红后绿证明"这个问题）；本模块服务于不同的问题：内容已由用户/Claude
在对话里确定，贾维斯只需要**机械地安全落地**：一次确认覆盖整个多文件计划、
逐文件落盘前留快照、全量测试 gate 过了才提交，任一步不满足就把计划整体
回滚——不留半成品，也不需要逐文件单独测红测绿。

与已有机制的关系（复用边界判定与提交模式，不重复发明）：
  - `core.self_model.classify()`：判断路径是否可写（PROTECTED 一律拒绝，
    与 self_iteration 同一套边界，不允许本模块另开口子绕过）。
  - `tests/` 目录延用 self_iteration 的 AUTO_TEST_PREFIX 约定：计划只能
    新建顶层 `test_auto_*.py`，不能碰既有测试或建子目录。
  - `git add` + `git commit` 提交留痕，格式仿 self_iteration._commit。

三条机械护栏（不靠模型自觉）：
  1) 只能写 propose() 时登记过的文件路径，且必须 self_model 判为 OPEN
     （tests/ 走上面的特例判断）——PROTECTED 一律拒绝，不生成 code_review
     二次审核提案（这条通道假设内容已确定，真要碰 PROTECTED 请求走其它
     方式，不是这条通道的职责）。
  2) 首次写入前 snapshot 原内容（不存在的文件记 existed=False），
     rollback() 精确复原到计划开始前的状态。
  3) finalize() 跑全量 gate（默认 tests/run_all.py）——绿才提交；
     红则整个计划涉及的全部文件一次性回滚，不留半成品。

生命周期：proposed → confirmed（连接器层 ConfirmGate 放行后）→ executing
（写过至少一个文件）→ applied / rolled_back。

设计仿 `core.self_iteration.SelfIterator`：核心逻辑装进可注入依赖的
`Engineer` 类（repo/classify/gate 均可覆盖），便于在临时仓库里做确定性
单测，不污染真实仓库、不会递归跑自己。生产用的模块级函数（`propose`/
`write_file`/... ）委托给一个惰性构建的默认单例，`connectors/engineering_tools.py`
直接用这些模块级函数，行为与直接用 `Engineer()` 完全一致。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core import self_model
from core.self_iteration import AUTO_TEST_PREFIX

DEFAULT_GATE = "tests/run_all.py"


@dataclass
class PlannedFile:
    path: str
    status: str = "pending"      # pending | written | rolled_back
    existed: bool = False        # 落盘前该路径是否已存在
    backup: str = ""             # 落盘前的原内容（existed=False 时无意义）
    snapshotted: bool = False    # 是否已经做过一次快照（只做一次，避免二次写入覆盖快照）
    blocked_reason: str = ""     # propose() 时预检出的拒绝理由（非空=这个文件写不了）


@dataclass
class Plan:
    plan_id: str
    summary: str
    rationale: str
    files: dict[str, PlannedFile] = field(default_factory=dict)
    status: str = "proposed"     # proposed | confirmed | executing | applied | rolled_back
    created_at: str = ""


class Engineer:
    """可注入依赖，便于在临时仓库里做确定性单测（同 SelfIterator 的理由）。"""

    def __init__(self, repo: Path | None = None, classify=None,
                 gate: str = DEFAULT_GATE, py: str | None = None,
                 git_commit: bool = True):
        self.repo = Path(repo or self_model.REPO_ROOT).resolve()
        self.classify = classify or self_model.classify
        self.gate = gate
        self.py = py or sys.executable
        self.git_commit_enabled = git_commit
        self.plans: dict[str, Plan] = {}

    # ── 路径检查 ─────────────────────────────────────────────────────────────
    def check_path(self, rel: str) -> tuple[bool, str]:
        """一个路径能不能被本模块写入。tests/ 走特例（只许新建顶层
        test_auto_*.py），其余一律走 classify()（PROTECTED 拒绝）。"""
        p = Path(rel)
        if p.parts and p.parts[0] == "tests":
            if len(p.parts) == 2 and p.name.startswith(AUTO_TEST_PREFIX):
                return True, ""
            return False, f"tests/ 下只能新建顶层 {AUTO_TEST_PREFIX}*.py，不能碰其它测试或建子目录"
        zone, reason = self.classify(rel)
        if zone != "open":
            return False, reason
        return True, ""

    def _is_allowed_test_path(self, rel: str) -> bool:
        """run_script() 允许跑的脚本：self.gate 本身，或 tests/test_auto_*.py
        顶层文件——不是通用脚本执行器，防止被当成任意代码执行口子。"""
        if rel == self.gate:
            return True
        p = Path(rel)
        return len(p.parts) == 2 and p.parts[0] == "tests" and p.name.startswith(AUTO_TEST_PREFIX)

    # ── 计划的生命周期 ──────────────────────────────────────────────────────
    def propose(self, summary: str, files: list[str], rationale: str = "") -> Plan:
        """登记一个新计划，不碰任何文件。每个文件路径预检一次，结果存进
        PlannedFile.blocked_reason（非空 = 这个文件将来写不了），供
        render_plan() 提前示警。"""
        plan_id = uuid.uuid4().hex[:8]
        planned: dict[str, PlannedFile] = {}
        for raw in files:
            rel = Path(raw).as_posix()
            ok, reason = self.check_path(rel)
            planned[rel] = PlannedFile(path=rel, blocked_reason="" if ok else reason)
        plan = Plan(
            plan_id=plan_id, summary=summary.strip(), rationale=rationale.strip(),
            files=planned, created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.plans[plan_id] = plan
        return plan

    def get(self, plan_id: str) -> "Plan | None":
        return self.plans.get(plan_id)

    def render_plan(self, plan: Plan) -> str:
        lines = [f"【计划 {plan.plan_id}】{plan.summary}"]
        if plan.rationale:
            lines.append(f"动机：{plan.rationale}")
        lines.append("涉及文件：")
        for pf in plan.files.values():
            if pf.blocked_reason:
                lines.append(f"  ✗ {pf.path} — 不可写：{pf.blocked_reason}")
            else:
                lines.append(f"  · {pf.path}")
        blocked = [pf.path for pf in plan.files.values() if pf.blocked_reason]
        if blocked:
            lines.append(f"注意：以上 {len(blocked)} 个文件受保护，这条通道写不了它们，"
                         f"需要人工另外处理；其余文件仍可正常执行。")
        lines.append(f"状态：{plan.status}。确认执行请调用 "
                     f"execute_engineering_change(plan_id=\"{plan.plan_id}\")。")
        return "\n".join(lines)

    def confirm(self, plan_id: str) -> "Plan | None":
        """ConfirmGate 放行后调用：把计划标记为可写状态。"""
        plan = self.get(plan_id)
        if plan is None:
            return None
        if plan.status == "proposed":
            plan.status = "confirmed"
        return plan

    def write_file(self, plan_id: str, path: str, content: str) -> tuple[bool, str]:
        plan = self.get(plan_id)
        if plan is None:
            return False, f"计划 {plan_id} 不存在，请先调用 propose_engineering_change。"
        if plan.status == "applied":
            return False, "该计划已经落地过了，不能再写（如需再改，请开一个新计划）。"
        if plan.status not in ("confirmed", "executing"):
            return False, "该计划尚未经过确认执行（先调用 execute_engineering_change 并等待用户确认）。"
        rel = Path(path).as_posix()
        pf = plan.files.get(rel)
        if pf is None:
            return False, f"{rel} 不在本计划登记的文件清单内（只能写 propose 时列出的文件）。"
        if pf.blocked_reason:
            return False, f"拒绝写入受保护路径 {rel}：{pf.blocked_reason}"
        target = self.repo / rel
        if not pf.snapshotted:
            pf.existed = target.exists()
            pf.backup = target.read_text(encoding="utf-8") if pf.existed else ""
            pf.snapshotted = True
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        pf.status = "written"
        plan.status = "executing"
        return True, f"已写入 {rel}（{len(content)} 字符）。"

    def rollback(self, plan_id: str) -> None:
        """把计划涉及的、已写入的文件精确复原到计划开始前的状态。"""
        plan = self.get(plan_id)
        if plan is None:
            return
        for pf in plan.files.values():
            if pf.status != "written" or not pf.snapshotted:
                continue
            target = self.repo / pf.path
            if pf.existed:
                target.write_text(pf.backup, encoding="utf-8")
            elif target.exists():
                target.unlink()
            pf.status = "rolled_back"
        plan.status = "rolled_back"

    def abandon(self, plan_id: str) -> tuple[bool, str]:
        """用户中途改主意：不跑 gate，直接回滚已写入的部分（若有）并作废计划。"""
        plan = self.get(plan_id)
        if plan is None:
            return False, f"计划 {plan_id} 不存在。"
        if plan.status == "applied":
            return False, "该计划已经落地提交，无法用 abandon 撤销（需要人工用 git revert）。"
        written = [pf.path for pf in plan.files.values() if pf.status == "written"]
        self.rollback(plan_id)
        if written:
            return True, f"已放弃计划 {plan_id}，并回滚了 {len(written)} 个已写入的文件。"
        return True, f"已放弃计划 {plan_id}（尚未写入任何文件）。"

    # ── 测试执行 + 收尾 ──────────────────────────────────────────────────────
    def run_script(self, rel: str) -> tuple[bool, str]:
        """跑一个受限范围内的脚本（self.gate 或 tests/test_auto_*.py），
        返回 (是否成功, 合并输出)。每次用全新 PYTHONPYCACHEPREFIX，避免
        .pyc 缓存让"刚写完立刻跑"读到旧字节码（同 self_iteration 的理由）。"""
        if not self._is_allowed_test_path(rel):
            return False, f"{rel} 不是允许的测试路径（只能是 {self.gate} 或 tests/{AUTO_TEST_PREFIX}*.py）"
        target = self.repo / rel
        if not target.exists():
            return False, f"{rel} 不存在"
        pycache = tempfile.mkdtemp(prefix="engineering_pyc_")
        env = {**os.environ, "PYTHONPYCACHEPREFIX": pycache, "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            r = subprocess.run([self.py, str(target)], cwd=str(self.repo),
                               capture_output=True, text=True, env=env)
            return (r.returncode == 0, (r.stdout or "") + (r.stderr or ""))
        finally:
            shutil.rmtree(pycache, ignore_errors=True)

    def _commit(self, plan: Plan, written: list[str]) -> str:
        try:
            msg = f"engineering: {plan.summary}\n\n动机: {plan.rationale}\nplan_id: {plan.plan_id}"
            subprocess.run(["git", "add", *written], cwd=str(self.repo),
                           capture_output=True, text=True)
            r = subprocess.run(["git", "commit", "-m", msg], cwd=str(self.repo),
                               capture_output=True, text=True)
            return "已 git 提交。" if r.returncode == 0 else f"git commit 跳过：{r.stderr.strip()[:150]}"
        except Exception as e:  # noqa: BLE001
            return f"git commit 出错：{e}"

    def finalize(self, plan_id: str, commit: bool = True) -> tuple[bool, str]:
        """跑全量 gate；绿→（可选）提交并标记 applied；红→整计划回滚。"""
        plan = self.get(plan_id)
        if plan is None:
            return False, f"计划 {plan_id} 不存在。"
        if plan.status == "applied":
            return True, "该计划早已落地，无需重复 finalize。"
        written = [p for p, pf in plan.files.items() if pf.status == "written"]
        if not written:
            return False, "该计划还没有任何文件被写入，先调用 write_open_file。"
        ok, out = self.run_script(self.gate)
        if not ok:
            self.rollback(plan_id)
            return False, (f"全量测试 {self.gate} 未通过，已回滚本计划涉及的 {len(written)} 个文件"
                           f"（不留半成品）：\n{out[-1500:]}")
        note = self._commit(plan, written) if (commit and self.git_commit_enabled) else "未提交（commit=False）。"
        plan.status = "applied"
        return True, f"计划已落地：{len(written)} 个文件通过全量测试。{note}"


# ── 生产默认单例（供 connectors/engineering_tools.py 用）────────────────────
_default: "Engineer | None" = None


def _get_default() -> Engineer:
    global _default
    if _default is None:
        _default = Engineer()
    return _default


def propose(summary: str, files: list, rationale: str = "") -> Plan:
    return _get_default().propose(summary, files, rationale)


def get(plan_id: str) -> "Plan | None":
    return _get_default().get(plan_id)


def render_plan(plan: Plan) -> str:
    return _get_default().render_plan(plan)


def confirm(plan_id: str) -> "Plan | None":
    return _get_default().confirm(plan_id)


def write_file(plan_id: str, path: str, content: str) -> tuple[bool, str]:
    return _get_default().write_file(plan_id, path, content)


def rollback(plan_id: str) -> None:
    _get_default().rollback(plan_id)


def abandon(plan_id: str) -> tuple[bool, str]:
    return _get_default().abandon(plan_id)


def run_script(rel: str) -> tuple[bool, str]:
    return _get_default().run_script(rel)


def finalize(plan_id: str, commit: bool = True) -> tuple[bool, str]:
    return _get_default().finalize(plan_id, commit=commit)
