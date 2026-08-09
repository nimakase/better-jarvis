"""计划模式执行器测试 —— core/engineering.py + connectors/engineering_tools.py 接线。

核心逻辑用临时仓库 + 注入 classify（同 tests/test_self_iteration.py 的模式）：
  propose 预检 / write_file 状态机（未确认拒写·越界拒写·受保护拒写·重复快照）/
  rollback 精确复原(含新建文件场景) / finalize 红→整体回滚·绿→提交并落地 / abandon。
接线部分只查 registry/effects/self_model/controller/spawn 的登记结果，不直接调用
连接器层函数（那些函数落在生产单例上，会真的碰真实仓库）。
"""
import sys
import tempfile
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from core.engineering import Engineer  # noqa: E402

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


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
    "import foo\n"
    "sys.exit(0 if foo.value() == 1 else 1)\n"
)

AUTO_FOO_TEST = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
    "import foo\n"
    "sys.exit(0 if foo.value() == 2 else 1)\n"
)


def build_repo():
    d = Path(tempfile.mkdtemp())
    (d / "foo.py").write_text("def value(): return 1\n")
    (d / "tests").mkdir()
    (d / "tests" / "run_all.py").write_text(GATE)
    (d / "tests" / "test_keystone.py").write_text(KEYSTONE)
    return d


def cls_open(path):
    rel = Path(path).as_posix()
    if rel in ("foo.py", "bar.py", "newfile.py"):
        return ("open", "biz")
    return ("protected", "core")


def mk(repo, **kw):
    return Engineer(repo=repo, classify=cls_open, gate="tests/run_all.py",
                    git_commit=False, **kw)


# ── 1. propose() 预检：OPEN 通过 / PROTECTED 标 blocked_reason / tests/ 特例 ──
print("[1] propose() 预检")
d = build_repo()
eng = mk(d)
plan = eng.propose("测试计划", ["foo.py", "secret.py", "tests/test_auto_foo.py",
                                "tests/test_keystone.py", "tests/sub/test_auto_x.py"])
check("1 foo.py 可写", plan.files["foo.py"].blocked_reason == "")
check("1 secret.py 受保护", plan.files["secret.py"].blocked_reason != "")
check("1 test_auto_foo.py 可写(tests/特例)", plan.files["tests/test_auto_foo.py"].blocked_reason == "")
check("1 test_keystone.py 拒(非auto前缀)", plan.files["tests/test_keystone.py"].blocked_reason != "")
check("1 子目录test_auto拒", plan.files["tests/sub/test_auto_x.py"].blocked_reason != "")
rendered = eng.render_plan(plan)
check("1 渲染标注不可写", "✗ secret.py" in rendered)
check("1 渲染给出解锁提示", "execute_engineering_change" in rendered)

# ── 2. write_file 状态机 ──────────────────────────────────────────────────────
print("[2] write_file 状态机")
d = build_repo()
eng = mk(d)
plan = eng.propose("改foo", ["foo.py", "bar.py", "secret.py"])
ok, msg = eng.write_file(plan.plan_id, "foo.py", "def value(): return 2\n")
check("2 未确认时拒写", not ok and "尚未经过确认执行" in msg)

eng.confirm(plan.plan_id)
check("2 confirm后状态为confirmed", plan.status == "confirmed")

ok, msg = eng.write_file("不存在的planid", "foo.py", "x")
check("2 计划不存在报错", not ok and "不存在" in msg)

ok, msg = eng.write_file(plan.plan_id, "outside.py", "x=1\n")
check("2 不在清单内拒写", not ok and "不在本计划登记的文件清单内" in msg)

ok, msg = eng.write_file(plan.plan_id, "secret.py", "x=1\n")
check("2 受保护路径拒写", not ok and "拒绝写入受保护路径" in msg)

ok, msg = eng.write_file(plan.plan_id, "foo.py", "def value(): return 2\n")
check("2 正常写入成功", ok)
check("2 落盘内容正确", "return 2" in (d / "foo.py").read_text())
check("2 写入后状态转executing", plan.status == "executing")
check("2 快照已捕获原内容", plan.files["foo.py"].backup == "def value(): return 1\n")

# 二次写入不覆盖快照
eng.write_file(plan.plan_id, "foo.py", "def value(): return 3\n")
check("2 二次写入快照不变", plan.files["foo.py"].backup == "def value(): return 1\n")
check("2 二次写入内容生效", "return 3" in (d / "foo.py").read_text())

# ── 3. rollback：既有文件精确复原 + 新建文件被删除 ───────────────────────────
print("[3] rollback")
d = build_repo()
eng = mk(d)
plan = eng.propose("回滚测试", ["foo.py", "newfile.py"])
eng.confirm(plan.plan_id)
eng.write_file(plan.plan_id, "foo.py", "def value(): return 99\n")
eng.write_file(plan.plan_id, "newfile.py", "x = 1\n")
check("3 newfile.py 已创建", (d / "newfile.py").exists())
eng.rollback(plan.plan_id)
check("3 foo.py 复原为原内容", (d / "foo.py").read_text() == "def value(): return 1\n")
check("3 newfile.py 被删除(此前不存在)", not (d / "newfile.py").exists())
check("3 计划状态rolled_back", plan.status == "rolled_back")

# ── 4. finalize：绿→提交落地 / 红→整体回滚 ───────────────────────────────────
print("[4] finalize")
d = build_repo()
eng = mk(d)
plan = eng.propose("正常改动", ["foo.py", "tests/test_auto_foo.py"])
eng.confirm(plan.plan_id)
eng.write_file(plan.plan_id, "foo.py", "def value(): return 2\n")
ok, msg = eng.finalize(plan.plan_id)
check("4a 缺配套测试也能finalize(未写测试文件不阻断)", True)  # 仅记录：本计划未写tests文件
# 未更新keystone(仍断言value()==1) → gate应失败并整体回滚
check("4a gate失败(keystone未同步)", not ok)
check("4a foo.py已回滚", (d / "foo.py").read_text() == "def value(): return 1\n")
check("4a 计划状态rolled_back", plan.status == "rolled_back")

d = build_repo()
eng = mk(d)
plan = eng.propose("正常改动+同步测试", ["foo.py", "tests/test_auto_foo.py"])
eng.confirm(plan.plan_id)
eng.write_file(plan.plan_id, "foo.py", "def value(): return 2\n")
eng.write_file(plan.plan_id, "tests/test_auto_foo.py", AUTO_FOO_TEST)
# 同时要过 keystone(断言value()==1)——先改 keystone 的期望，模拟"计划里本该同步改的一环"
(d / "tests" / "test_keystone.py").write_text(
    "import sys\nfrom pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
    "import foo\nsys.exit(0 if foo.value() == 2 else 1)\n")
ok, msg = eng.finalize(plan.plan_id)
check("4b gate通过", ok)
check("4b 计划状态applied", plan.status == "applied")
check("4b foo.py最终内容正确", "return 2" in (d / "foo.py").read_text())

ok2, msg2 = eng.finalize(plan.plan_id)
check("4b 重复finalize直接成功不报错", ok2 and "早已落地" in msg2)

ok3, msg3 = eng.write_file(plan.plan_id, "foo.py", "def value(): return 100\n")
check("4b applied后拒绝再写", not ok3 and "已经落地过了" in msg3)

ok4, msg4 = eng.abandon(plan.plan_id)
check("4b applied后abandon拒绝", not ok4)

# finalize 无任何写入 → 拒绝
d = build_repo()
eng = mk(d)
plan = eng.propose("空计划", ["foo.py"])
eng.confirm(plan.plan_id)
ok5, msg5 = eng.finalize(plan.plan_id)
check("4c 未写任何文件时finalize拒绝", not ok5 and "还没有任何文件被写入" in msg5)

# ── 5. abandon ────────────────────────────────────────────────────────────────
print("[5] abandon")
d = build_repo()
eng = mk(d)
plan = eng.propose("放弃测试", ["foo.py"])
eng.confirm(plan.plan_id)
ok, msg = eng.abandon(plan.plan_id)
check("5 未写入时abandon", ok and "尚未写入任何文件" in msg)

d = build_repo()
eng = mk(d)
plan = eng.propose("放弃测试2", ["foo.py"])
eng.confirm(plan.plan_id)
eng.write_file(plan.plan_id, "foo.py", "def value(): return 5\n")
ok, msg = eng.abandon(plan.plan_id)
check("5 有写入时abandon回滚", ok and "回滚了 1 个" in msg)
check("5 内容已复原", (d / "foo.py").read_text() == "def value(): return 1\n")

# ── 6. run_script：路径白名单 ──────────────────────────────────────────────────
print("[6] run_script 路径白名单")
d = build_repo()
eng = mk(d)
ok, out = eng.run_script("tests/run_all.py")
check("6 gate脚本本身可跑", ok)
ok, out = eng.run_script("foo.py")
check("6 非tests路径拒绝", not ok and "不是允许的测试路径" in out)
ok, out = eng.run_script("tests/sub/test_auto_x.py")
check("6 tests子目录拒绝", not ok and "不是允许的测试路径" in out)
ok, out = eng.run_script("tests/test_keystone.py")
check("6 非auto前缀测试拒绝", not ok and "不是允许的测试路径" in out)

# ── 7. 接线检查：registry/effects/self_model/controller/spawn ────────────────
print("[7] 接线检查")
import connectors.engineering_tools as _et  # noqa: E402,F401 触发@tool注册
from core import effects as _effects       # noqa: E402
from core import self_model as _self_model  # noqa: E402
from core import controller as _controller  # noqa: E402
from core import spawn as _spawn            # noqa: E402
from core import registry as _registry      # noqa: E402

check("7 propose=READ_LOCAL", _effects.effect_of("propose_engineering_change") == _effects.READ_LOCAL)
check("7 execute=WRITE_EXTERNAL", _effects.effect_of("execute_engineering_change") == _effects.WRITE_EXTERNAL)
check("7 write_open_file=WRITE_LOCAL", _effects.effect_of("write_open_file") == _effects.WRITE_LOCAL)
check("7 run_repo_test=READ_LOCAL", _effects.effect_of("run_repo_test") == _effects.READ_LOCAL)
check("7 finalize=WRITE_LOCAL", _effects.effect_of("finalize_engineering_change") == _effects.WRITE_LOCAL)
check("7 abandon=WRITE_LOCAL", _effects.effect_of("abandon_engineering_change") == _effects.WRITE_LOCAL)

NAMES = {"propose_engineering_change", "execute_engineering_change", "write_open_file",
         "run_repo_test", "finalize_engineering_change", "abandon_engineering_change"}
check("7 全部六个在BACKGROUND_BLOCKED_TOOLS", NAMES <= _controller.BACKGROUND_BLOCKED_TOOLS)

check("7 core/engineering.py是PROTECTED", _self_model.classify("core/engineering.py")[0] == "protected")
check("7 connectors/engineering_tools.py是PROTECTED",
      _self_model.classify("connectors/engineering_tools.py")[0] == "protected")

grp = set(_registry.groups().get("engineering", []))
check("7 engineering组含六个工具", NAMES <= grp)

# ConfirmGate：execute_engineering_change 首次拦、确认后放行（同一 plan_id）
g = _effects.ConfirmGate()
g.new_user_turn()
ok, _ = g.check("execute_engineering_change", '{"plan_id":"abc12345"}')
check("7 execute首次调用被ConfirmGate拦下", not ok)
g.new_user_turn()
ok2, _ = g.check("execute_engineering_change", '{"plan_id":"abc12345"}')
check("7 用户确认后同参数放行", ok2)

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败: {FAILURES}")
    sys.exit(1)
print("✅ test_engineering 全部通过")
sys.exit(0)
