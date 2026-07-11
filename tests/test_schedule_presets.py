"""可启动任务预设目录测试 —— core/schedule_presets。

只测纯数据/视图层（catalog 完整性、started 标记、安全视图不漏 prompt）；
create_from_preset 涉及真实 scheduler(重依赖)，留给本机集成验证，这里不触发。
"""
import sys
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from core import schedule_presets as sp  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# 每个预设字段齐全、cron 是 5 段
for pid, p in sp.PRESETS.items():
    check(f"{pid} 字段齐全", all(k in p and p[k] for k in sp._REQUIRED))
    check(f"{pid} cron 为 5 段", len(p["default_cron"].split()) == 5)
    check(f"{pid} name 即 id", p["name"] == pid)

check("含 self_review 预设", "self_review" in sp.PRESETS)

# list_presets：安全视图不应泄露 prompt
view = sp.list_presets(existing_names=set())
check("list 返回全部预设", len(view) == len(sp.PRESETS))
check("视图不含 prompt(防泄露)", all("prompt" not in v for v in view))
check("视图含必要字段", all({"id", "title", "description", "default_cron", "started"} <= set(v) for v in view))
check("未创建时 started=False", all(v["started"] is False for v in view))

# started 标记：已存在同名任务则为 True
view2 = sp.list_presets(existing_names={"self_review"})
sr = next(v for v in view2 if v["id"] == "self_review")
check("已创建则 started=True", sr["started"] is True)
other = next(v for v in view2 if v["id"] != "self_review")
check("未创建的仍 False", other["started"] is False)

# get_preset
check("get_preset 命中", sp.get_preset("self_review") is not None)
check("get_preset 未知返回 None", sp.get_preset("nope") is None)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
