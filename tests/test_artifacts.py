#!/usr/bin/env python3
"""core/artifacts 产物登记表 + 图书馆 + 技能试用态 —— 确定性单测（隔离，不碰真实数据）。"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── 隔离：把 DB 和图书馆指到临时目录，绝不污染真实 memory.db ──────────────────
import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_artifacts_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

from core import artifacts  # noqa: E402  （import 时 init_db 已建在临时库上）

artifacts.LIBRARY_DIR = _TMP / "图书馆"
artifacts.init_db()

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. 登记 + 图书馆视图 ──────────────────────────────────────────────────────
print("[1] 登记与图书馆")
src = _TMP / "raw" / "screen_result.xlsx"
src.parent.mkdir(parents=True)
src.write_bytes(b"fake xlsx content " * 100)

rec = artifacts.register(src, producer="skill:oem_ems_screener", kind="清单",
                         label="OEM筛查_137家", state="kept")
check(rec["id"] and rec["size"] > 0, "登记返回 id 与大小")
lib = Path(rec["library_path"])
check(lib.exists(), "图书馆视图已创建（硬链接/副本）")
check("清单" in str(lib) and "OEM筛查_137家" in lib.name, "视图按类型分目录、文件名带标签")
check(src.exists(), "原文件不受影响")

fp = artifacts.footprint()
check(fp.get("skill:oem_ems_screener", {}).get("count") == 1, "足迹按 producer 汇总")

# ── 2. trial 产物 + 过期扫除 ──────────────────────────────────────────────────
print("[2] 试用产物与过期")
t_src = _TMP / "raw" / "trial_output.csv"
t_src.write_text("a,b\n1,2\n")
t_rec = artifacts.register(t_src, producer="skill:试验工具", kind="清单",
                           label="试跑结果", state="trial")
check(t_rec["ttl_days"] == artifacts.DEFAULT_TRIAL_TTL_DAYS, "trial 自动套默认保质期")

# 未到期：扫除不动它
swept = artifacts.sweep_expired()
check(len(swept) == 0, "未到期不扫")

# 到期：用未来时间扫 → 标 expired、移出图书馆，但【原文件保留】
future = datetime.now(timezone.utc) + timedelta(days=artifacts.DEFAULT_TRIAL_TTL_DAYS + 1)
swept = artifacts.sweep_expired(today=future)
check(len(swept) == 1, "到期 trial 被扫出")
after = artifacts.list_artifacts(producer="skill:试验工具")[0]
check(after["state"] == "expired", "状态转 expired")
check(not Path(t_rec["library_path"]).exists(), "图书馆视图已移除")
check(t_src.exists(), "原文件绝不自动删（删除必须走 purge + 确认闸）")

# ── 3. purge：先清点后删除 ────────────────────────────────────────────────────
print("[3] purge 两段式")
res = artifacts.purge_producer("skill:试验工具", delete_files=False)
check(len(res["items"]) == 1 and not res["deleted"], "不带 delete_files 只清点不删")
check(t_src.exists(), "清点后文件仍在")

res = artifacts.purge_producer("skill:试验工具", delete_files=True)
check(res["deleted"] and not t_src.exists(), "delete_files=True 才真删文件")
check(artifacts.list_artifacts(producer="skill:试验工具") == [], "登记同步清空")
check(src.exists(), "别的 producer 的产物不受影响")

# ── 4. purge_artifacts 工具必须声明为 irreversible（吃确认闸）─────────────────
print("[4] 工具效应声明")
from core import effects, registry  # noqa: E402
import connectors.artifact_tools  # noqa: E402, F401  （触发 @tool 注册）

check(effects.effect_of("purge_artifacts") == effects.IRREVERSIBLE,
      "purge_artifacts 声明为 irreversible（自动被确认闸拦）")
check(effects.effect_of("library_overview") == effects.READ_LOCAL,
      "library_overview 是只读")

# ── 5. 技能试用生命周期（tool_builder.keep_skill，隔离目录）──────────────────
print("[5] 技能 trial → active")
from core import tool_builder  # noqa: E402

_ORIG_SKILLS = tool_builder.SKILLS_DIR
tool_builder.SKILLS_DIR = _TMP / "skills"
try:
    d = tool_builder.SKILLS_DIR / "demo_tool"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"status": "trial"}), encoding="utf-8")

    ok, msg = tool_builder.keep_skill("demo_tool")
    check(ok, f"trial 技能可转正：{msg}")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    check(meta["status"] == "active" and meta.get("kept_at"), "meta 转 active 并记录 kept_at")

    ok2, _ = tool_builder.keep_skill("demo_tool")
    check(ok2, "已转正的重复 keep 幂等")

    (d / "meta.json").write_text(json.dumps({"status": "draft"}), encoding="utf-8")
    ok3, _ = tool_builder.keep_skill("demo_tool")
    check(not ok3, "draft 不能直接转正（必须先激活试用）")
finally:
    tool_builder.SKILLS_DIR = _ORIG_SKILLS

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_artifacts 全部通过")
sys.exit(0)
