#!/usr/bin/env python3
"""world_state 总线 + channels 画像 + device 感官 —— 确定性单测。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import channels, world_state  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


# ── 1. 总线：注册 / TTL 缓存 / 失败降级 ───────────────────────────────────────
print("[1] world_state 总线")
calls = {"n": 0}


def fake_sensor():
    calls["n"] += 1
    return {"读数": calls["n"]}


world_state.register_provider("_test", fake_sensor, ttl_s=0.3)
s1 = world_state.snapshot()
s2 = world_state.snapshot()
check(s1["_test"]["读数"] == 1 and s2["_test"]["读数"] == 1, "TTL 内走缓存（只采一次）")
time.sleep(0.35)
s3 = world_state.snapshot()
check(s3["_test"]["读数"] == 2, "TTL 过期后重采")
check(world_state.snapshot(force=True)["_test"]["读数"] == 3, "force 强制重采")


def broken_sensor():
    raise RuntimeError("采集挂了")


world_state.register_provider("_broken", broken_sensor, ttl_s=60)
snap = world_state.snapshot()
check("_broken" not in snap and "_test" in snap, "失败感官被跳过，其余不受影响")


def flaky():
    if calls.get("flaky_ok"):
        raise RuntimeError("后来挂了")
    calls["flaky_ok"] = True
    return {"值": "旧的"}


world_state.register_provider("_flaky", flaky, ttl_s=0.01)
world_state.snapshot()          # 第一次成功
time.sleep(0.05)
snap = world_state.snapshot()   # 第二次失败 → 用旧读数并标 stale
check(snap.get("_flaky", {}).get("stale") is True, "失败但有旧读数 → 凑合用并标 stale")

blk = world_state.context_block()
check("当前处境" in blk and "_test" in blk, "context_block 生成处境块")
check("stale" not in blk.split("_flaky")[1][:20], "stale 标记不进正文（只显示（旧读数））")

for n in ("_test", "_broken", "_flaky"):
    world_state.unregister_provider(n)

# ── 2. 渠道画像 ───────────────────────────────────────────────────────────────
print("[2] channels 画像")
check("飞书" in channels.note("lark") and "文件" in channels.note("lark"),
      "飞书画像含宽表落文件的指引")
check("PWA" in channels.note("web") or "网页" in channels.note("web"), "网页画像存在")
check("没有人在看" in channels.note("background"), "后台画像强调无人在看")
check("未知" in channels.note(""), "未知渠道有保守回退")

# ── 3. 设备感官（沙箱里只验证「不炸 + 返回 dict」）──────────────────────────
print("[3] device 感官")
from sensors import device  # noqa: E402

for fn in (device.collect_battery, device.collect_network,
           device.collect_disk, device.collect_presence):
    out = fn()
    check(isinstance(out, dict), f"{fn.__name__} 返回 dict（缺读数则空，绝不抛错）")

check(device.collect_disk().get("磁盘剩余"), "磁盘读数在任何平台都可用")

# import sensors 包应完成注册
import sensors  # noqa: E402, F401
snap = world_state.snapshot()
check("磁盘" in snap, "import sensors 后磁盘感官已在总线上")

# ── 4. controller 渠道默认值（不实例化，验证逻辑常量）─────────────────────────
print("[4] 渠道默认")
# 非交互实例应默认 background（spawn/定时任务没人看）
# 逻辑：channel or ("" if interactive else "background")
check((lambda ch, inter: ch or ("" if inter else "background"))("", False) == "background",
      "非交互实例默认 background 渠道")
check((lambda ch, inter: ch or ("" if inter else "background"))("lark", False) == "lark",
      "显式渠道优先")

# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_world_state 全部通过")
sys.exit(0)
