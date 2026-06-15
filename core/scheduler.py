"""
定时任务调度器

架构：
  - APScheduler AsyncIOScheduler 挂在 FastAPI 事件循环内
  - 每个任务存在 schedules/<name>/config.json
  - 触发时创建独立 JarvisController 实例执行，不污染用户对话历史
  - 结果通过飞书推送或写入本地文件

config.json 格式：
{
    "name": "components_daily",
    "description": "每日电子元器件市场情报",
    "status": "active",              // active | paused
    "cron": "0 8 * * *",             // 标准 cron 表达式
    "prompt": "搜索今日电子元器件...",  // 发给贾维斯的指令
    "delivery": {
        "type": "feishu",            // feishu | file
        "receive_id": "xxx",         // 飞书接收方 ID（type=feishu 时必填）
        "receive_id_type": "open_id" // open_id | user_id | chat_id
    },
    "created_at": "2026-06-13T..."
}
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from core.safety import safe_name, is_safe_name

logger = logging.getLogger("jarvis.scheduler")

SCHEDULES_DIR = config.SCHEDULES_DIR
SCHEDULES_DIR.mkdir(exist_ok=True)

# 全局调度器实例
_scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


# ── 任务执行 ──────────────────────────────────────────────────────────────────

async def _run_job(name: str, prompt: str, delivery: dict):
    """执行一个定时任务，使用独立的 Controller 实例。"""
    from core.controller import JarvisController

    logger.info(f"定时任务触发：{name}")

    try:
        sc = JarvisController()
        result = ""
        async for chunk in sc.chat(prompt):
            # 过滤掉工具调用状态行（⚙️ 调用工具...）
            if not chunk.startswith("\n⚙️"):
                result += chunk

        result = result.strip()
        if not result:
            result = "（任务执行完毕，无输出）"

        await _deliver(name, result, delivery)
        logger.info(f"定时任务完成：{name}")

    except Exception as e:
        logger.error(f"定时任务 {name} 执行失败：{e}")
        await _deliver(name, f"任务执行出错：{e}", delivery)


async def _deliver(name: str, content: str, delivery: dict):
    """推送结果到指定渠道。"""
    delivery_type = delivery.get("type", "file")

    if delivery_type == "feishu":
        from connectors.feishu import send_feishu_message
        receive_id = delivery.get("receive_id", "")
        receive_id_type = delivery.get("receive_id_type", "open_id")
        if not receive_id:
            logger.error(f"任务 {name}：飞书 receive_id 未配置")
            return
        header = f"📋 【{name}】定时报告\n{datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'─' * 30}\n"
        await send_feishu_message(receive_id, header + content, receive_id_type)

    elif delivery_type == "file":
        report_dir = Path.home() / "jarvis_data" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{name}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        path = report_dir / filename
        path.write_text(content, encoding="utf-8")
        logger.info(f"任务 {name} 报告已保存：{path}")


# ── 任务管理 ──────────────────────────────────────────────────────────────────

def _schedule_dir(name: str) -> Path:
    return SCHEDULES_DIR / safe_name(name)   # safe_name 防路径穿越，非法名抛 ValueError


def _load_config(name: str) -> Optional[dict]:
    try:
        path = _schedule_dir(name) / "config.json"
    except ValueError:
        return None
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_config(name: str, cfg: dict):
    d = _schedule_dir(name)
    d.mkdir(exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _register_job(cfg: dict):
    """向 APScheduler 注册一个任务。"""
    name = cfg["name"]
    _scheduler.add_job(
        _run_job,
        trigger=CronTrigger.from_crontab(cfg["cron"], timezone="Asia/Shanghai"),
        kwargs={"name": name, "prompt": cfg["prompt"], "delivery": cfg.get("delivery", {})},
        id=name,
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info(f"已注册定时任务：{name}（{cfg['cron']}）")


def load_all_active_schedules():
    """启动时加载所有 active 状态的定时任务。"""
    if not SCHEDULES_DIR.exists():
        return
    for d in SCHEDULES_DIR.iterdir():
        if not d.is_dir():
            continue
        cfg = _load_config(d.name)
        if cfg and cfg.get("status") == "active":
            try:
                _register_job(cfg)
            except Exception as e:
                logger.error(f"加载定时任务 {d.name} 失败：{e}")


def create_schedule(
    name: str,
    description: str,
    cron: str,
    prompt: str,
    delivery_type: str = "file",
    receive_id: str = "",
    receive_id_type: str = "open_id",
) -> tuple[bool, str]:
    """创建并启动一个新定时任务。"""
    if not is_safe_name(name):
        return False, f"任务名非法：{name!r}（只允许字母、数字、下划线、连字符，长度 1-64）"
    # 验证 cron 表达式
    try:
        CronTrigger.from_crontab(cron, timezone="Asia/Shanghai")
    except Exception as e:
        return False, f"cron 表达式无效：{e}"

    cfg = {
        "name": name,
        "description": description,
        "status": "active",
        "cron": cron,
        "prompt": prompt,
        "delivery": {
            "type": delivery_type,
            "receive_id": receive_id,
            "receive_id_type": receive_id_type,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_config(name, cfg)

    try:
        _register_job(cfg)
    except Exception as e:
        return False, f"注册任务失败：{e}"

    return True, f"定时任务「{name}」已创建，执行时间：{cron}"


def delete_schedule(name: str) -> tuple[bool, str]:
    """删除定时任务。"""
    import shutil
    try:
        d = _schedule_dir(name)
    except ValueError as e:
        return False, str(e)
    if not d.exists():
        return False, f"任务 {name} 不存在"

    if _scheduler.get_job(name):
        _scheduler.remove_job(name)

    shutil.rmtree(d)
    return True, f"任务 {name} 已删除"


def pause_schedule(name: str) -> tuple[bool, str]:
    cfg = _load_config(name)
    if not cfg:
        return False, f"任务 {name} 不存在"
    cfg["status"] = "paused"
    _save_config(name, cfg)
    if _scheduler.get_job(name):
        _scheduler.pause_job(name)
    return True, f"任务 {name} 已暂停"


def resume_schedule(name: str) -> tuple[bool, str]:
    cfg = _load_config(name)
    if not cfg:
        return False, f"任务 {name} 不存在"
    cfg["status"] = "active"
    _save_config(name, cfg)
    _register_job(cfg)
    return True, f"任务 {name} 已恢复"


def list_schedules() -> list[dict]:
    result = []
    if not SCHEDULES_DIR.exists():
        return result
    for d in sorted(SCHEDULES_DIR.iterdir()):
        if not d.is_dir():
            continue
        cfg = _load_config(d.name)
        if cfg:
            job = _scheduler.get_job(cfg["name"])
            next_run = str(job.next_run_time) if job and job.next_run_time else "—"
            result.append({
                "name": cfg["name"],
                "description": cfg.get("description", ""),
                "status": cfg.get("status", "unknown"),
                "cron": cfg.get("cron", ""),
                "delivery_type": cfg.get("delivery", {}).get("type", ""),
                "next_run": next_run,
            })
    return result


# ── 公开调度器实例（供 main.py 启动/停止）──────────────────────────────���─────

def get_scheduler() -> AsyncIOScheduler:
    return _scheduler
