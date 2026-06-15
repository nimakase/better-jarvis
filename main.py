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

from core import registry
from core.context import AppContext
from core.tool_builder import register_meta_tools, load_all_active_skills
from core.scheduler import get_scheduler, load_all_active_schedules

# Web 路由模块
from web import pwa, chat
from web import credentials as web_credentials
from web import skills as web_skills
from web import memory as web_memory
from web import files as web_files

# 注册工具：先内置元工具，再自动发现 connectors/ 下所有连接器
register_meta_tools()
registry.discover_connectors()

# 启动时加载所有已激活的自建技能
load_all_active_skills()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：加载定时任务并启动调度器
    load_all_active_schedules()
    scheduler = get_scheduler()
    scheduler.start()
    yield
    # 关闭：停止调度器
    scheduler.shutdown(wait=False)


app = FastAPI(title="贾维斯", lifespan=lifespan)

# 进程内共享上下文（会话管理等），供各路由通过 app.state.ctx 取用
app.state.ctx = AppContext()

# 装配路由
app.include_router(pwa.router)
app.include_router(chat.router)
app.include_router(web_credentials.router)
app.include_router(web_skills.router)
app.include_router(web_memory.router)
app.include_router(web_files.router)


# ── 启动 ──────────────────────────────────────────────────────────────────────

def main():
    """控制台入口（pip 安装后可用 `jarvis` 命令启动）。"""
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
