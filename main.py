"""
贾维斯 - 入口

启动：python main.py
然后浏览器打开 http://localhost:8000

本文件只负责「装配」：注册工具、建应用上下文、挂载各路由模块。
具体端点实现见 web/ 包；对话主循环见 core/controller.py。
"""

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

import config
from core import registry
from core.context import AppContext
from core.tool_builder import load_all_active_skills
from core.scheduler import get_scheduler, load_all_active_schedules, register_builtin_jobs

# Web 路由模块
from web import pwa, chat
from web import credentials as web_credentials
from web import documents as web_documents
from web import skills as web_skills
from web import history as web_history
from web import files as web_files
from web import schedules as web_schedules
from web import push as web_push
from web import intel as web_intel
from web import hubspot as web_hubspot
from web import reports as web_reports
from web import workflows as web_workflows
from web import calendar as web_calendar

# 注册工具：导入 tool_builder 即注册元工具；自动发现 connectors/ 下所有连接器
registry.discover_connectors()

# 注册工作流（导入即注册：prospect_daily 等）
from intel import workflow_defs as _workflow_defs  # noqa: F401

# 启动时加载所有已激活的自建技能
load_all_active_skills()

# 一次性迁移：旧 delivery_state.json 的休假区间 → 内置日历 rest 事件（幂等）
try:
    from core import delivery as _delivery
    _migrated = _delivery.migrate_rest_to_calendar()
    if _migrated:
        import logging as _logging
        _logging.getLogger("jarvis").info("已迁移 %d 段休假到内置日历。", _migrated)
except Exception as _e:
    import logging as _logging
    _logging.getLogger("jarvis").warning("休假迁移失败（不影响启动）：%s", _e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：加载定时任务并启动调度器
    load_all_active_schedules()
    register_builtin_jobs()          # 内置提醒巡检（每 1 分钟）
    scheduler = get_scheduler()
    scheduler.start()

    # 飞书（Lark）实时通道：仅在配置了 App 凭据时启动。整段用 try 包裹——
    # 飞书桥任何问题（未装 lark-oapi、凭据错、网络）都【绝不能】拖垮主服务启动。
    app.state.lark_bridge = None
    if config.FEISHU_APP_ID and config.FEISHU_APP_SECRET:
        try:
            from lark_bridge import LarkBridge

            async def _get_lark_controller(open_id: str):
                # 每个飞书用户一个独立会话（lark_ 前缀，与网页会话隔离）
                return app.state.ctx.sessions.get(f"lark_{open_id}")

            bridge = LarkBridge(
                config.FEISHU_APP_ID, config.FEISHU_APP_SECRET,
                get_controller=_get_lark_controller,
            )
            await bridge.start()
            app.state.lark_bridge = bridge
        except Exception as _e:
            import logging as _logging
            _logging.getLogger("jarvis").warning("飞书桥启动失败（不影响主服务）：%s", _e)

    yield

    # 关闭：先停飞书桥（若有），再停调度器
    if getattr(app.state, "lark_bridge", None):
        try:
            await app.state.lark_bridge.stop()
        except Exception:
            pass
    scheduler.shutdown(wait=False)


app = FastAPI(title="贾维斯", lifespan=lifespan)

# 进程内共享上下文（会话管理等），供各路由通过 app.state.ctx 取用
app.state.ctx = AppContext()

# 装配路由
app.include_router(pwa.router)
app.include_router(chat.router)
app.include_router(web_credentials.router)
app.include_router(web_documents.router)
app.include_router(web_skills.router)
app.include_router(web_history.router)
app.include_router(web_files.router)
app.include_router(web_schedules.router)
app.include_router(web_push.router)
app.include_router(web_intel.router)
app.include_router(web_hubspot.router)
app.include_router(web_reports.router)
app.include_router(web_workflows.router)
app.include_router(web_calendar.router)


# ── 启动 ──────────────────────────────────────────────────────────────────────

def main():
    """控制台入口（pip 安装后可用 `jarvis` 命令启动）。"""
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=False)


if __name__ == "__main__":
    main()
