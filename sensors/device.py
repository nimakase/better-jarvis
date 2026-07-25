"""
sensors/device.py — 设备/环境感官（阶段 2 · ⑩ 最便宜的第一批「眼」）

采集本机的基础处境：电量、网络连通、磁盘余量、用户是否在电脑前（空闲时长）。
全部只读、best-effort：任一采集失败返回 {}（world_state 会跳过），
绝不抛错、绝不拖慢对话（有 TTL 缓存，命令都带超时）。

跨平台策略：macOS 用 pmset/ioreg；其它平台能采多少采多少，缺的直接不报。
"""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
import sys

from core import world_state

_IS_MAC = sys.platform == "darwin"
_CMD_TIMEOUT = 3  # 秒；本机命令超时即放弃本次读数


def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_CMD_TIMEOUT)
        return r.stdout or ""
    except Exception:
        return ""


# ── 电量（macOS: pmset）──────────────────────────────────────────────────────

def collect_battery() -> dict:
    if not _IS_MAC:
        return {}
    out = _run(["pmset", "-g", "batt"])
    m = re.search(r"(\d+)%;\s*(\w+)", out)
    if not m:
        return {}
    pct, state = int(m.group(1)), m.group(2)
    zh = {"charging": "充电中", "discharging": "用电池", "charged": "已充满",
          "finishing": "即将充满", "AC": "接电源"}.get(state, state)
    d = {"电量": f"{pct}%", "电源": zh}
    if state == "discharging" and pct <= 20:
        d["低电量"] = "是"
    return d


# ── 网络连通（TCP 探测，不发数据）────────────────────────────────────────────

def collect_network() -> dict:
    for host, port in (("223.5.5.5", 53), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return {"网络": "在线"}
        except OSError:
            continue
    return {"网络": "离线"}


# ── 磁盘余量 ──────────────────────────────────────────────────────────────────

def collect_disk() -> dict:
    try:
        u = shutil.disk_usage("/")
        free_gb = u.free / 1e9
        d = {"磁盘剩余": f"{free_gb:.0f}GB"}
        if free_gb < 10:
            d["磁盘紧张"] = "是"
        return d
    except Exception:
        return {}


# ── 用户在不在（macOS: HID 空闲时长）────────────────────────────────────────

def collect_presence() -> dict:
    if not _IS_MAC:
        return {}
    out = _run(["ioreg", "-c", "IOHIDSystem"])
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    if not m:
        return {}
    idle_s = int(m.group(1)) / 1e9   # 纳秒 → 秒
    if idle_s < 120:
        return {"用户": "在电脑前"}
    if idle_s < 1800:
        return {"用户": f"离开约 {int(idle_s // 60)} 分钟"}
    return {"用户": "长时间不在（勿扰式交付为宜）"}


# ── 注册（import 即生效；TTL 按变化频率分档）─────────────────────────────────

world_state.register_provider("电池", collect_battery, ttl_s=120)
world_state.register_provider("网络", collect_network, ttl_s=60)
world_state.register_provider("磁盘", collect_disk, ttl_s=600)
world_state.register_provider("在场", collect_presence, ttl_s=30)
