"""客户循环状态库单测(状态标记 / 运行记录 / 只覆盖自己写的守卫)。零第三方依赖。

    .venv/bin/python tests/test_customer_loop_store.py
"""
import json
import sys
import tempfile
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

# 用临时目录当存储目录,不碰真实数据
import os
_tmp = tempfile.mkdtemp(prefix="cl_store_")
os.environ["JARVIS_CUSTOMER_LOOP_DIR"] = _tmp

from prospecting import customer_loop_store as s   # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ── 冷启动标记 ──────────────────────────────────────
check("初始未应用", s.is_coldstart_applied() is False)
s.mark_coldstart_applied({"auto": 460})
check("标记后已应用", s.is_coldstart_applied() is True)

# ── 运行记录 ────────────────────────────────────────
s.record_run("coldstart", {"applied": True})
s.record_run("incremental", {"changed": 3})
check("last_run coldstart", s.last_run("coldstart") is not None)
check("last_run incremental changed=3", s.last_run("incremental")["summary"]["changed"] == 3)
check("last_run 未知类型 None", s.last_run("nope") is None)

# ── 只覆盖自己写的守卫 ──────────────────────────────
# 造一份 write_log.jsonl:jarvis 给 A 的 priority 真写过 warm
log = Path(_tmp) / "write_log.jsonl"
log.write_text(
    json.dumps({"account": "A", "field": "priority", "value": "warm", "applied": True}) + "\n"
    + json.dumps({"account": "B", "field": "priority", "value": "cold", "applied": False}) + "\n",  # dry-run 不算
    encoding="utf-8")

check("jarvis 写过 A/priority=warm", s.jarvis_last_write("A", "priority") == "warm")
check("dry-run 不计入(B)", s.jarvis_last_write("B", "priority") is None)
check("从没写过 C", s.jarvis_last_write("C", "priority") is None)

# 守卫:
check("A 现值==自己写的 warm → 可续改", s.should_auto_write("A", "priority", "warm") is True)
check("A 现值被人改成 hot → 退让不写", s.should_auto_write("A", "priority", "hot") is False)
check("C 没写过 + 现值空 → 填空白", s.should_auto_write("C", "priority", None) is True)
check("C 没写过 + 现值非空 → 不碰", s.should_auto_write("C", "priority", "hot") is False)

print("\n" + ("❌ 失败: " + str(fails) if fails else "✅ 全绿"))
sys.exit(1 if fails else 0)
