"""自我迭代执行器的安全机制测试 —— core/self_iteration。

用临时仓库 + 注入 classify，验证四条关键路径与若干护栏：
  applied（先红后绿+gate 全绿）/ rejected（空测试未先红）/
  failed+回滚（gate 回归）/ needs_human（受保护拒写）。
只依赖 self_iteration + self_model（轻），沙箱可跑。
"""
import sys
import tempfile
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from core.self_iteration import SelfIterator, Proposal  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


GATE = (
    "import subprocess, sys\n"
    "from pathlib import Path\n"
    "T = Path(__file__).resolve().parent\n"
    "PY = sys.executable\n"
    "bad = []\n"
    "for f in sorted(T.glob('test_*.py')):\n"
    "    if subprocess.run([PY, str(f)], cwd=str(T.parent)).returncode != 0:\n"
    "        bad.append(f.name)\n"
    "sys.exit(1 if bad else 0)\n"
)

KEYSTONE = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
    "import calc\n"
    "sys.exit(0 if calc.const() == 42 else 1)\n"
)

# 配套测试：断言 label()=='new'（引用 calc、含 sys.exit、够长）
AUTO_GOOD = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
    "import calc\n"
    "sys.exit(0 if calc.label() == 'new' else 1)\n"
)
# 空测试：改前改后都为真
AUTO_VACUOUS = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
    "import calc\n"
    "sys.exit(0 if calc.label() in ('old', 'new') else 1)\n"
)

NEW_OK = 'def label(): return "new"\ndef const(): return 42\n'   # 保留 const → 不回归
NEW_BREAKS = 'def label(): return "new"\n'                       # 丢了 const → gate 回归


def build_repo(label="old"):
    d = Path(tempfile.mkdtemp())
    (d / "calc.py").write_text(f'def label(): return "{label}"\ndef const(): return 42\n')
    (d / "tests").mkdir()
    (d / "tests" / "run_all.py").write_text(GATE)
    (d / "tests" / "test_keystone.py").write_text(KEYSTONE)
    return d


def cls_open_calc(path):
    return ("open", "biz") if Path(path).as_posix() == "calc.py" else ("protected", "core")


def mk(repo):
    return SelfIterator(repo=repo, classify=cls_open_calc, git_commit=False)


# —— 1) 正常落地：先红后绿 + gate 全绿 ——
d = build_repo()
o = mk(d).execute(Proposal("calc.py", NEW_OK, AUTO_GOOD, "改文案", "无外部影响"))
check("1 applied 状态", o.status == "applied")
check("1 代码已更新为 new", 'return "new"' in (d / "calc.py").read_text())
check("1 自动测试已落盘", (d / "tests" / "test_auto_calc.py").exists())

# —— 2) 空测试（未先红）→ rejected，文件不变 ——
d = build_repo()
o = mk(d).execute(Proposal("calc.py", NEW_OK, AUTO_VACUOUS))
check("2 rejected 状态", o.status == "rejected")
check("2 代码未被改动", 'return "old"' in (d / "calc.py").read_text())
check("2 未残留自动测试", not (d / "tests" / "test_auto_calc.py").exists())

# —— 3) gate 回归（改坏 const）→ failed 且精确回滚 ——
d = build_repo()
o = mk(d).execute(Proposal("calc.py", NEW_BREAKS, AUTO_GOOD))
check("3 failed 状态", o.status == "failed")
check("3 代码已回滚到 old", 'return "old"' in (d / "calc.py").read_text())
check("3 const 仍在（回滚干净）", "const" in (d / "calc.py").read_text())
check("3 未残留自动测试", not (d / "tests" / "test_auto_calc.py").exists())

# —— 4) 受保护路径 → needs_human，绝不写 ——
d = build_repo()
o = mk(d).execute(Proposal("core/controller.py", "x=1\n", AUTO_GOOD))
check("4 needs_human 状态", o.status == "needs_human")

# —— 5) 机械检查：测试未引用模块/过短 → rejected ——
d = build_repo()
bad_test = "import sys\nsys.exit(0)\n"  # 太短 + 未引用 calc
o = mk(d).execute(Proposal("calc.py", NEW_OK, bad_test))
check("5 机械检查 rejected", o.status == "rejected")

# —— 6) 自动测试文件名必须 test_auto_ 前缀（防覆盖既有测试）——
d = build_repo()
o = mk(d).execute(Proposal("calc.py", NEW_OK, AUTO_GOOD, test_name="test_keystone.py"))
check("6 非 auto 前缀被拒", o.status == "rejected")
check("6 keystone 未被覆盖", "const() == 42" in (d / "tests" / "test_keystone.py").read_text())

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
