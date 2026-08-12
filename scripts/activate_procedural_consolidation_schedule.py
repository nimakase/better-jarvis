#!/usr/bin/env python3
"""scripts/activate_procedural_consolidation_schedule.py — 一次性脚本：
按预设创建/更新"过程记忆巩固"定时任务(默认每周一凌晨 03:00, Asia/Shanghai)。

背景：我已经直接在 schedules/procedural_consolidation/config.json 里手写好了同样
内容的配置(2026-08-12)，Jarvis 下次重启时 load_all_active_schedules() 会自动加载
生效，不一定需要跑这个脚本。这个脚本是给你两种情况用的：
  1) 想立即校验 cron 表达式合法、或想改成别的时间——跑一下更保险(create_schedule
     内部会先校验 cron 再落盘);
  2) 不想等 Jarvis 重启，想在当前进程之外先把 config.json 校验/重建一遍。
注意：这个脚本本身跑完就退出，不会让"当前正在跑的 Jarvis 进程"立刻挂上定时——
定时真正生效仍要等 Jarvis 下次启动(或你在对话里让它跑 resume_schedule)。

用真实 venv 跑(需要 config/apscheduler，不能在沙盒里跑)：
    .venv/bin/python scripts/activate_procedural_consolidation_schedule.py

幂等：create_schedule 内部 replace_existing=True，重复跑不会建出两条。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import schedule_presets  # noqa: E402

ok, msg = schedule_presets.create_from_preset("procedural_consolidation")
print(msg)
sys.exit(0 if ok else 1)
