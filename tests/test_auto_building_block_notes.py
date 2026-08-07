#!/usr/bin/env python3
"""self_review/造技能写回 building block 实践验证笔记（任务 #12）——自动生成，绝不覆盖既有测试文件。

覆盖：
  1. core/building_block_notes 存储层：增/查/上限淘汰最老一条
  2. core/skill_policy.used_building_block_modules：AST 识别代码里实际 import 过的
     BUILDING_BLOCKS 模块（from-import / import 两种写法），无关 import 不误判
  3. building_blocks_api_text() 渲染出的卡片包含已记的验证笔记
  4. auto_module_api_text() 同样能带出验证笔记（哪怕这个模块不在 BUILDING_BLOCKS 里）
  5. core/tool_builder._write_back_building_block_notes：真实调用一次，验证笔记落到
     正确的模块名下、evidence 带技能名；无相关 import 时不写入任何东西
  6. _author_verified_loop 的源码里确实接了这一步（连线检查）
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_bbnotes_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


from core import building_block_notes as bbn  # noqa: E402

bbn.init_db()

# ── 1. 存储层 ─────────────────────────────────────────────────────────────────
print("[1] core/building_block_notes 存储层")

_MOD = "_test.fake.module"
r = bbn.add_note(_MOD, "第一条笔记", evidence="skill:foo")
check(r["ok"], "add_note 成功返回 ok=True")
r_empty = bbn.add_note("", "x")
check(not r_empty["ok"], "module 为空 → 拒绝")
r_empty2 = bbn.add_note(_MOD, "")
check(not r_empty2["ok"], "note 为空 → 拒绝")

notes = bbn.notes_for(_MOD)
check(len(notes) == 1 and notes[0]["note"] == "第一条笔记", "notes_for 能查到刚写的笔记")
check(_MOD in bbn.verified_modules(), "verified_modules 包含刚记过笔记的模块")

for i in range(2, bbn.MAX_NOTES_PER_MODULE + 5):
    bbn.add_note(_MOD, f"第{i}条笔记")
notes_after = bbn.notes_for(_MOD)
check(len(notes_after) == bbn.MAX_NOTES_PER_MODULE,
      f"超过上限后自动淘汰最老的，稳定在 {bbn.MAX_NOTES_PER_MODULE} 条")
check(notes_after[0]["note"] == f"第{bbn.MAX_NOTES_PER_MODULE + 4}条笔记",
      "淘汰的是最老的，保留的是最新的（第一条是最新写入的）")

block = bbn.build_block(_MOD)
check("实践验证记录" in block and "第一条笔记" not in block,
      "build_block 渲染出验证记录标题，且最老那条已被淘汰不再出现")
check(bbn.build_block("_从没记过笔记的模块_") == "", "无笔记的模块 → build_block 返回空串")


# ── 2. used_building_block_modules ────────────────────────────────────────────
print("[2] skill_policy.used_building_block_modules")
from core import skill_policy  # noqa: E402

real_mod = next(iter(skill_policy.BUILDING_BLOCKS))  # 取一个真实登记过的模块名
symbols = skill_policy.BUILDING_BLOCKS[real_mod].get("symbols") or []
symbol = symbols[0] if symbols else "SomeClass"

code_from_import = f"from {real_mod} import {symbol}\n\nasync def handler():\n    return {symbol}()\n"
hit1 = skill_policy.used_building_block_modules(code_from_import)
check(real_mod in hit1, f"from-import 写法能识别出 {real_mod}")

code_plain_import = f"import {real_mod}\n\nasync def handler():\n    return 1\n"
hit2 = skill_policy.used_building_block_modules(code_plain_import)
check(real_mod in hit2, "plain import 写法同样能识别")

code_unrelated = "import json\nfrom pathlib import Path\n\nasync def handler():\n    return json.dumps({})\n"
hit3 = skill_policy.used_building_block_modules(code_unrelated)
check(hit3 == set(), "无关的标准库 import 不会被误判为 building block")

hit4 = skill_policy.used_building_block_modules("def(: syntax error")
check(hit4 == set(), "语法错误的代码 → 安全返回空集，不抛异常")


# ── 3. building_blocks_api_text() 带出验证笔记 ──────────────────────────────────
print("[3] building_blocks_api_text() 渲染验证笔记")
bbn.add_note(real_mod, f"造技能真实用过 {real_mod}，经隔离冒烟验证可用", evidence="skill:_test_probe")
text = skill_policy.building_blocks_api_text()
check(real_mod in text, "该模块的卡片本身在输出里")
check("实践验证记录" in text and f"造技能真实用过 {real_mod}" in text,
      "该模块的验证笔记被渲染进最终喂给生成器的文本里")


# ── 4. auto_module_api_text() 同样带出验证笔记 ──────────────────────────────────
print("[4] auto_module_api_text() 渲染验证笔记")
_AUTO_MOD = "core.tool_timeout"   # 不在 BUILDING_BLOCKS 里的普通模块
bbn.add_note(_AUTO_MOD, "自动抽取模块也能挂验证笔记", evidence="skill:_test_probe2")
auto_text = skill_policy.auto_module_api_text(_AUTO_MOD)
check("未经人工核实用法" in auto_text, "自动抽取仍保留原有免责声明（结构层不因验证笔记而消失）")
check("实践验证记录" in auto_text and "自动抽取模块也能挂验证笔记" in auto_text,
      "非 BUILDING_BLOCKS 模块也能挂验证笔记——两条轨道独立")


# ── 5. tool_builder._write_back_building_block_notes ───────────────────────────
print("[5] tool_builder 写回")
from core import tool_builder as tb  # noqa: E402

_MOD2 = real_mod
before = len(bbn.notes_for(_MOD2))
tb._write_back_building_block_notes("_test_skill_xyz", "测试用的技能描述", code_from_import)
after = bbn.notes_for(_MOD2)
check(len(after) == before + 1, "写回后该模块笔记数 +1")
check(after[0]["evidence"] == "skill:_test_skill_xyz", "笔记的 evidence 带上了技能名")
check("_test_skill_xyz" in after[0]["note"], "笔记正文里点名是哪个技能验证的")

before2 = len(bbn.notes_for(_MOD2))
tb._write_back_building_block_notes("_test_skill_unrelated", "无关技能", code_unrelated)
check(len(bbn.notes_for(_MOD2)) == before2, "没用到任何 building block 的代码 → 不写入任何笔记")


# ── 6. _author_verified_loop 接线检查 ───────────────────────────────────────────
print("[6] _author_verified_loop 接线检查")
import inspect  # noqa: E402
src = inspect.getsource(tb._author_verified_loop)
check("_write_back_building_block_notes" in src,
      "_author_verified_loop 的源码里确实在冒烟成功路径上调用了写回函数")


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_building_block_notes 全部通过")
sys.exit(0)
