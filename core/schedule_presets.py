"""
core/schedule_presets.py — 可启动的定时任务「预设目录」（前端可视化启动用）

每个预设是一个"值得定时跑的任务"：标题、说明、默认频率(cron)、以及发给贾维斯的
固定提示词(prompt，**服务端预置、不经前端**，避免提示词注入)。前端列出预设卡片 +
频率选择器，一键启动即按所选 cron 创建一个真实定时任务（复用 core.scheduler）。

本模块顶层零重依赖（catalog 是纯数据，可单测）；create_from_preset 惰性引 scheduler。
要新增可启动任务 = 往 PRESETS 加一条。
"""
from __future__ import annotations

from typing import Optional

# id -> 预设定义
PRESETS: dict[str, dict] = {
    "self_review": {
        "name": "self_review",
        "title": "自我迭代反思",
        "description": "定期审视周边代码：诊断出真缺陷才自动修(先红后绿+可回滚)，"
                       "核心改动只生成提案送你审。无真缺陷则零改动。",
        "default_cron": "0 9 * * 1",   # 每周一 09:00
        "prompt": "请运行 run_self_review 工具，跑一轮自我迭代反思，并把结果摘要发我。",
    },
    "signal_collection": {
        "name": "signal_collection",
        "title": "采集今日市场信号",
        "description": "联网广扫并入库今日市场信号，喂情报台看板与市场情报日报。",
        "default_cron": "0 8 * * *",   # 每天 08:00
        "prompt": "请运行 signal_collection 工作流，采集今日市场信号并入库。",
    },
}

_REQUIRED = ("name", "title", "description", "default_cron", "prompt")


def list_presets(existing_names: Optional[set] = None) -> list[dict]:
    """给前端的安全视图（不含 prompt）；started 标记该预设是否已创建为定时任务。"""
    existing = existing_names or set()
    return [{
        "id": pid,
        "title": p["title"],
        "description": p["description"],
        "default_cron": p["default_cron"],
        "started": p["name"] in existing,
    } for pid, p in PRESETS.items()]


def get_preset(pid: str) -> Optional[dict]:
    return PRESETS.get(pid)


def create_from_preset(pid: str, cron: str = "") -> tuple[bool, str]:
    """按预设 + 所选 cron 创建定时任务。prompt 取服务端预置，前端无法篡改。"""
    p = get_preset(pid)
    if not p:
        return False, f"未知预设：{pid}"
    from core import scheduler  # 惰性引（避免顶层拉起 config/apscheduler）
    return scheduler.create_schedule(
        name=p["name"],
        description=p["title"],
        cron=(cron or "").strip() or p["default_cron"],
        prompt=p["prompt"],
    )
