"""反思闭环编排 + 必要性闸测试 —— core/self_review。

用假模型 + 临时仓库 + 注入执行器，验证：
  解析(含 category/severity)、必要性闸(真缺陷才自动落地)、冷却期、区位路由、写复盘。
不依赖真实模型/联网。
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from core import self_review as sr  # noqa: E402
from core.self_iteration import SelfIterator, Proposal  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def build_repo():
    d = Path(tempfile.mkdtemp())
    (d / "calc.py").write_text('def label(): return "old"\ndef const(): return 42\n')
    (d / "helper.py").write_text("# helper\nY = 1\n")
    (d / "core_thing.py").write_text("X = 1\n")
    (d / "tests").mkdir()
    (d / "tests" / "run_all.py").write_text(
        "import subprocess, sys\nfrom pathlib import Path\n"
        "T=Path(__file__).resolve().parent\nPY=sys.executable\nbad=[]\n"
        "for f in sorted(T.glob('test_*.py')):\n"
        "    if subprocess.run([PY,str(f)],cwd=str(T.parent)).returncode!=0: bad.append(f.name)\n"
        "sys.exit(1 if bad else 0)\n")
    return d


def cls(path):
    rel = Path(path).as_posix()
    return ("open", "biz") if rel in ("calc.py", "helper.py") else ("protected", "core")


NEW_OK = 'def label(): return "new"\ndef const(): return 42\n'
AUTO_GOOD = (
    "import sys\nfrom pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
    "import calc\nsys.exit(0 if calc.label() == 'new' else 1)\n"
)

PAYLOAD = [
    {"path": "calc.py", "defect": "label 返回了错误的值", "category": "bugfix",
     "severity": "high", "rationale": "修正返回值", "impact": "无外部影响",
     "new_code": NEW_OK, "test_code": AUTO_GOOD},
    {"path": "helper.py", "defect": "命名可更清晰", "category": "readability",
     "severity": "low", "rationale": "改名", "impact": "无",
     "new_code": "# changed\nY = 1\n", "test_code": "noop"},
    {"path": "core_thing.py", "defect": "常量应为 2", "category": "correctness",
     "severity": "high", "rationale": "调常量", "impact": "影响核心",
     "new_code": "X = 2\n", "test_code": "import sys\nsys.exit(0)\n"},
    {"path": "calc.py", "rationale": "缺字段，应被解析跳过"},
]
FAKE_OUTPUT = "```json\n" + json.dumps(PAYLOAD, ensure_ascii=False) + "\n```"


async def fake_model(prompt):
    assert "先红后绿" in prompt and "PROTECTED" in prompt and "先诊断" in prompt
    return FAKE_OUTPUT


# —— 解析 ——
props = sr.parse_proposals(FAKE_OUTPUT)
check("解析得 3 条(坏的跳过)", len(props) == 3)
check("解析带出 category", any(p.category == "bugfix" for p in props))

# —— necessity_verdict 直接单测 ——
def P(cat, sev, defect="x"):
    return Proposal("a.py", "", "", category=cat, severity=sev, defect=defect)

check("bugfix/high -> auto", sr.necessity_verdict(P("bugfix", "high")) == "auto")
check("correctness/medium -> auto", sr.necessity_verdict(P("correctness", "medium")) == "auto")
check("bugfix/low -> defer", sr.necessity_verdict(P("bugfix", "low")) == "defer")
check("readability/high -> defer", sr.necessity_verdict(P("readability", "high")) == "defer")
check("缺 defect -> defer", sr.necessity_verdict(P("bugfix", "high", defect="")) == "defer")


async def main():
    # —— 一轮闭环：真缺陷落地 / 非必要降级 / 受保护送审 ——
    d = build_repo()
    it = SelfIterator(repo=d, classify=cls, git_commit=False)
    rdir = d / "docs" / "self_review"
    res = await sr.run_cycle(fake_model, iterator=it, review_dir=rdir,
                             focus_source={"calc.py": "..."}, module_map="(map)")

    check("真缺陷(bugfix/high) 自动落地", res["applied"] == ["calc.py"])
    check("calc.py 确实改了", 'return "new"' in (d / "calc.py").read_text())
    check("非必要(readability/low) 降级 deferred", res["deferred"] == ["helper.py"])
    check("helper.py 未被改动", (d / "helper.py").read_text() == "# helper\nY = 1\n")
    check("受保护送人工", res["needs_human"] == ["core_thing.py"])
    check("core_thing 未改动", (d / "core_thing.py").read_text() == "X = 1\n")
    check("仅受保护产生送审动作", len(res["review_actions"]) == 1)
    check("复盘写盘", Path(res["summary_path"]).exists())

    # —— 冷却期：上轮已改过 calc.py，本轮再提应被跳过 ——
    d2 = build_repo()
    it2 = SelfIterator(repo=d2, classify=cls, git_commit=False)
    rdir2 = d2 / "docs" / "self_review"
    rdir2.mkdir(parents=True, exist_ok=True)
    (rdir2 / "2026-06-01_000000.md").write_text("# 旧复盘\n- [✅ 已落地] `calc.py` — 上轮改过\n")

    async def only_calc(prompt):
        return json.dumps([{"path": "calc.py", "defect": "又想改", "category": "bugfix",
                            "severity": "high", "rationale": "r", "impact": "i",
                            "new_code": NEW_OK, "test_code": AUTO_GOOD}], ensure_ascii=False)

    res2 = await sr.run_cycle(only_calc, iterator=it2, review_dir=rdir2,
                              focus_source={"x": "y"}, module_map="(map)")
    check("冷却期内被跳过", res2["skipped"] == ["calc.py"])
    check("冷却期未落地", res2["applied"] == [])
    check("冷却期 calc.py 未改", 'return "old"' in (d2 / "calc.py").read_text())


asyncio.run(main())
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
