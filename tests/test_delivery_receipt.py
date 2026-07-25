#!/usr/bin/env python3
"""㉔ 动作回执/投递闭环 —— 确定性单测（隔离临时库）。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_receipt_test_"))
config.MEMORY_DB_PATH = _TMP / "memory.db"

from core import delivery, telemetry  # noqa: E402
telemetry.init_db()

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


_STATE = _TMP / "delivery_state.json"


# ── 1. deliver 诚实的 delivered + 回执落盘 ───────────────────────────────────
print("[1] 诚实送达 + 回执")


def ok_channel(title, content):
    return {"sent": True}


def boom_channel(title, content):
    raise RuntimeError("网络挂了")


# 全成功
res = delivery.deliver("report", "日报就绪", "内容", channels={"webpush": ok_channel},
                       routing={"normal": ["webpush"]}, state_path=_STATE)
check(res["delivered"] is True, "有渠道成功 → delivered=True")

# 全失败 → 诚实报 False（此前恒 True）
res2 = delivery.deliver("report", "又一条", "内容", channels={"webpush": boom_channel},
                        routing={"normal": ["webpush"]}, state_path=_STATE)
check(res2["delivered"] is False, "渠道全失败 → delivered=False（不再假装成功）")

# 渠道未注册 → 也算失败（静默失败被暴露）
res3 = delivery.deliver("report", "第三条", "内容", channels={},
                        routing={"normal": ["webpush"]}, state_path=_STATE)
check(res3["delivered"] is False and "webpush" in res3["missing_channels"],
      "渠道未注册 → delivered=False 且列入 missing")

# 部分成功 → 只要有一个成就算送达
res4 = delivery.deliver("report", "混合", "内容",
                        channels={"webpush": ok_channel, "lark": boom_channel},
                        routing={"normal": ["webpush", "lark"]}, state_path=_STATE)
check(res4["delivered"] is True, "部分渠道成功 → delivered=True")

# ── 2. 回执可查 ───────────────────────────────────────────────────────────────
print("[2] 回执查询")
receipts = telemetry.delivery_receipts(limit=10)
check(len(receipts) == 4, f"四次投递都落了回执（实际 {len(receipts)}）")
check(receipts[0]["channels"]["webpush"]["ok"] is True, "回执含逐渠道成败明细")

failed = telemetry.delivery_receipts(failed_only=True)
check(len(failed) == 2, "failed_only 只返回未送达的（全失败 2 次）")
check(telemetry.recent_delivery_failures(days=3) == 2, "近 3 天失败计数正确")

# 暂停也留回执（可解释「为什么没发」）
delivery.pause_track("report", state_path=_STATE)
res5 = delivery.deliver("report", "暂停期", "内容", channels={"webpush": ok_channel},
                        routing={"normal": ["webpush"]}, state_path=_STATE)
check(res5["delivered"] is False, "暂停轨 → 不投递")
paused_receipts = [r for r in telemetry.delivery_receipts(limit=10)
                   if "_paused" in (r.get("channels") or {})]
check(len(paused_receipts) == 1, "暂停也留回执（可解释为何没发）")

# ── 3. 健康感官发现投递失败 ──────────────────────────────────────────────────
print("[3] 健康感官")
from sensors import health  # noqa: E402
h = health.collect_delivery_health()
check("未送达" in h.get("投递", ""), "投递失败进健康读数（我以为发了其实没发→可见）")

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_delivery_receipt 全部通过")
sys.exit(0)
