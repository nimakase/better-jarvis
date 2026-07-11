"""
core/self_iteration.py — 自我迭代执行器（阶段 3 核心机制 · 自身受保护）

把「一条优化提案」安全地落地：
  区位判定 → 文件快照 → 先红后绿验证 → 全量 gate → 通过则保留(+git 提交) / 失败则回滚。
PROTECTED 一律不写、转人工提案通道。

设计要点：
- 与「谁生成代码/测试」解耦——execute() 接收**已生成**的 new_code/test_code，
  生成（调 LLM）在上层反思工作流里做。核心机制因此可被确定性单测覆盖。
- 安全靠机械护栏，不靠模型自觉：
  1) 只写 OPEN 路径（is_writable_by_self_iteration），PROTECTED 物理拒写；
  2) 自动测试只能**新建** test_auto_*.py，**绝不覆盖/删除**既有测试（防拆安全网）；
  3) 先红后绿：新测试在改前必须失败、改后必须通过——挡掉「空测试」；
  4) 全量 gate（run_all）必须通过——挡掉「改坏了别处」；
  5) 任一步失败 → 精确回滚到原内容。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from core import self_model

AUTO_TEST_PREFIX = "test_auto_"


@dataclass
class Proposal:
    path: str            # 要改的文件（相对仓库根），如 'core/calendar.py'
    new_code: str        # 改后的完整文件内容
    test_code: str       # 配套测试脚本（约定：失败时退出码非零）
    rationale: str = ""  # 为什么改（动机）
    impact: str = ""     # 改了会怎样（影响）
    category: str = ""   # 缺陷类别：bugfix/robustness/correctness/readability/style/dedup
    severity: str = ""   # 严重度：high/medium/low —— 必要性闸据此判定是否自动落地
    defect: str = ""     # 诊断出的具体缺陷（必要性的依据，不是泛泛"可以更好"）
    test_name: str = ""  # 自动测试文件名(test_auto_*.py)；留空按 path 派生


@dataclass
class Outcome:
    status: str          # applied | rejected | failed | needs_human
    path: str
    reason: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "applied"


class SelfIterator:
    """可注入依赖，便于在临时仓库里做确定性单测。"""

    def __init__(self, repo: Path | None = None, classify=None,
                 gate: str = "tests/run_all.py", py: str | None = None,
                 git_commit: bool = True):
        self.repo = Path(repo or self_model.REPO_ROOT).resolve()
        self.tests_dir = self.repo / "tests"
        self.classify = classify or self_model.classify
        self.gate = gate
        self.py = py or sys.executable
        self.git_commit = git_commit

    # —— 内部小工具 ——
    def _run_script(self, rel_or_abs: str | Path) -> tuple[bool, str]:
        """跑一个脚本，返回 (退出码为0?, 合并输出)。

        关键：每次用全新的 PYTHONPYCACHEPREFIX，杜绝 .pyc 字节码缓存——
        否则「改 .py 后立刻 import」会因 mtime 同秒而读到旧缓存，导致先红后绿误判。
        """
        p = Path(rel_or_abs)
        target = p if p.is_absolute() else self.repo / p
        pycache = tempfile.mkdtemp(prefix="selfiter_pyc_")
        env = {**os.environ, "PYTHONPYCACHEPREFIX": pycache, "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            r = subprocess.run([self.py, str(target)], cwd=str(self.repo),
                               capture_output=True, text=True, env=env)
            return (r.returncode == 0, (r.stdout or "") + (r.stderr or ""))
        finally:
            shutil.rmtree(pycache, ignore_errors=True)

    def _test_path_for(self, prop: Proposal) -> Path:
        if prop.test_name:
            name = prop.test_name
        else:
            stem = Path(prop.path).stem
            name = f"{AUTO_TEST_PREFIX}{stem}.py"
        return self.tests_dir / name

    @staticmethod
    def _mechanical_test_ok(test_code: str, target_path: str) -> tuple[bool, str]:
        """轻量机械检查：挡掉明显无效的测试。"""
        code = test_code or ""
        if len(code.strip()) < 40:
            return (False, "测试过短，疑似空测试")
        if "sys.exit" not in code:
            return (False, "测试未用 sys.exit 表达失败（无法被 gate 判红）")
        # 必须提及被改模块（最低限度的"测到点子上"）
        stem = Path(target_path).stem
        if stem not in code:
            return (False, f"测试未引用被改模块 {stem}")
        # 禁止纯 assert True 之类的恒真占位
        if "assert True" in code:
            return (False, "包含 assert True 占位")
        return (True, "")

    # —— 主流程 ——
    def execute(self, prop: Proposal) -> Outcome:
        rel = Path(prop.path).as_posix()

        # 护栏 1：区位——PROTECTED 一律不写
        zone, reason = self.classify(rel)
        if zone != "open":
            return Outcome("needs_human", rel,
                           "核心/受保护路径，禁止自动修改", reason)

        target = self.repo / rel
        if not target.exists():
            return Outcome("failed", rel, "目标文件不存在")

        test_path = self._test_path_for(prop)
        # 护栏 2：自动测试只能落在 tests/、且必须是 test_auto_ 前缀（绝不碰既有测试）
        if test_path.parent.resolve() != self.tests_dir.resolve():
            return Outcome("rejected", rel, "测试路径越界，只能写入 tests/")
        if not test_path.name.startswith(AUTO_TEST_PREFIX):
            return Outcome("rejected", rel,
                           f"自动测试文件名必须以 {AUTO_TEST_PREFIX} 开头")

        # 护栏 3：机械检查
        ok, why = self._mechanical_test_ok(prop.test_code, rel)
        if not ok:
            return Outcome("rejected", rel, f"测试未通过机械检查：{why}")

        # 快照（精确到本次会触碰的两个文件）
        code_backup = target.read_text(encoding="utf-8")
        test_existed = test_path.exists()
        test_backup = test_path.read_text(encoding="utf-8") if test_existed else None

        def _restore():
            target.write_text(code_backup, encoding="utf-8")
            if test_existed:
                test_path.write_text(test_backup, encoding="utf-8")
            elif test_path.exists():
                test_path.unlink()

        try:
            # 1) 先写测试，跑在【改前】代码上 → 必须红（失败）
            test_path.write_text(prop.test_code, encoding="utf-8")
            passed_before, out_before = self._run_script(test_path)
            if passed_before:  # 改前就绿 = 没测到要改的东西
                _restore()
                return Outcome("rejected", rel,
                               "测试在改动前即通过（空测试，未先红）", out_before[-800:])

            # 2) 应用代码改动，跑测试 → 必须绿（通过）
            target.write_text(prop.new_code, encoding="utf-8")
            passed_after, out_after = self._run_script(test_path)
            if not passed_after:
                _restore()
                return Outcome("failed", rel,
                               "改动后配套测试未通过", out_after[-800:])

            # 3) 全量 gate（run_all）→ 必须绿（挡回归）
            gate_ok, out_gate = self._run_script(self.gate)
            if not gate_ok:
                _restore()
                return Outcome("failed", rel,
                               "全量测试 gate 回归（改坏了别处）", out_gate[-800:])

        except BaseException as e:  # noqa: BLE001 — 含 Ctrl-C/SIGTERM：先回滚再处理
            _restore()
            if isinstance(e, Exception):
                return Outcome("failed", rel, f"执行异常，已回滚：{e}")
            raise  # KeyboardInterrupt / SystemExit：已回滚，继续中断传播

        # 成功：best-effort git 提交留痕
        commit_note = ""
        if self.git_commit:
            commit_note = self._commit(rel, test_path, prop)
        return Outcome("applied", rel, "已通过先红后绿 + 全量 gate 并落地", commit_note)

    def _commit(self, rel: str, test_path: Path, prop: Proposal) -> str:
        try:
            msg = f"self-iter: {rel}\n\n动机: {prop.rationale}\n影响: {prop.impact}"
            subprocess.run(["git", "add", rel, str(test_path.relative_to(self.repo))],
                           cwd=str(self.repo), capture_output=True, text=True)
            r = subprocess.run(["git", "commit", "-m", msg],
                               cwd=str(self.repo), capture_output=True, text=True)
            return "git committed" if r.returncode == 0 else f"git commit skipped: {r.stderr.strip()[:120]}"
        except Exception as e:  # noqa: BLE001
            return f"git commit error: {e}"
