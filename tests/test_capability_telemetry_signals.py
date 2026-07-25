#!/usr/bin/env python3
"""能力索引 + 遥测 + 监督信号 —— 确定性单测（隔离临时库，不碰真实数据）。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_ctsig_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. 能力索引：检索与查重 ───────────────────────────────────────────────────
print("[1] 能力索引")
from core import capability, registry  # noqa: E402
import connectors.calendar_tools  # noqa: E402, F401  （注册一些真实工具进索引）
import connectors.artifact_tools  # noqa: E402, F401

caps = capability.gather()
check(len(caps) >= 5, f"聚合到 {len(caps)} 项能力（工具/技能）")

hits = capability.search("查一下我的日历日程安排")
check(any("calendar" in h["name"] for h in hits), "「查日历日程」命中 calendar 工具")

hits2 = capability.search("图书馆里都存了什么产物")
check(any(h["name"] == "library_overview" for h in hits2), "「图书馆产物」命中 library_overview")

check(capability.search("量子色动力学晶格模拟") == [], "无关需求不乱命中")

# 查重闸：与已有工具高度重合的请求应被拦
dup = capability.check_duplicate("产物图书馆总览：按来源汇总登记过的所有产物的数量与大小")
check(dup is not None and "library_overview" in dup, "高度重合的造工具请求被拦并点名已有能力")
check(capability.check_duplicate("监控家里交换机端口流量并在掉线时告警") is None,
      "全新需求不误拦")

# ── 2. 遥测：记录与聚合 ───────────────────────────────────────────────────────
print("[2] 执行遥测")
from core import telemetry  # noqa: E402
telemetry.init_db()

telemetry.record("weather", True, 120)
telemetry.record("weather", True, 80)
telemetry.record("hubspot_session_check", False, 3000, "TimeoutError: page load")
telemetry.record("hubspot_session_check", False, 2800, "TimeoutError: page load")

st = {s["tool"]: s for s in telemetry.stats(days=1)}
check(st["weather"]["n"] == 2 and st["weather"]["failures"] == 0, "成功调用正确聚合")
check(st["hubspot_session_check"]["failures"] == 2, "失败次数正确")
check("TimeoutError" in st["hubspot_session_check"]["last_error"], "错误样本被保留")

rep = telemetry.trouble_report(days=1, min_failures=2)
check("hubspot_session_check" in rep and "weather" not in rep,
      "故障画像只含真出问题的工具")

telemetry.log_gap("监控交换机端口")
check(True, "能力缺口落库不抛异常")

# ── 3. 监督信号：保守检测 ─────────────────────────────────────────────────────
print("[3] 监督信号")
from core import signals  # noqa: E402
signals.init_db()

check(signals.detect("不是，我说的是上海的天气") == "correction", "开头「不是」→ correction")
check(signals.detect("不对，应该用毛利率") == "correction", "开头「不对」→ correction")
check(signals.detect("算了，不弄了") == "frustration", "开头「算了」→ frustration")
check(signals.detect("重新生成一遍") == "retry", "开头「重新」→ retry")
check(signals.detect("帮我查下天气") == "", "普通消息无信号")
check(signals.detect("他说这样做不对，但我觉得可以") == "",
      "「不对」在句中（转述）不算——保守优先")

kind = signals.tag("不是，我要的是季度数据", prev_assistant_text="这是年度数据…")
check(kind == "correction", "tag 落库并返回类型")
rows = signals.recent(kind="correction")
check(len(rows) == 1 and "季度" in rows[0]["user_text"], "落库内容可查回")
check(rows[0]["prev_excerpt"].startswith("这是年度"), "上一轮摘录一并保存（学习时要上下文）")

# ── 4. self_review 注入遥测 ──────────────────────────────────────────────────
print("[4] 反思 prompt 注入")
from core import self_review  # noqa: E402

p = self_review.build_reflection_prompt({}, "", "MAP", trouble="【故障画像】X 失败 7 次")
check("故障画像" in p and p.index("故障画像") < p.index("上一轮复盘"),
      "故障画像注入且排在复盘之前（优先级最高）")
p2 = self_review.build_reflection_prompt({}, "", "MAP")
check("真实运行故障画像" not in p2, "无故障时不注入空段落")

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_capability_telemetry_signals 全部通过")
sys.exit(0)
