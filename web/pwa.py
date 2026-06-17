"""前端页面与 PWA 静态资源路由（manifest / service worker / 图标，根作用域）。"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, FileResponse

import config

router = APIRouter()
FRONTEND_DIR = config.FRONTEND_DIR


@router.get("/", response_class=HTMLResponse)
async def index():
    html_path = FRONTEND_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# 情报台 / 定时任务的独立页已下线，内容并入主页右侧抽屉（frontend/index.html）。
# 对应 API（/api/intel/*、/api/schedules/*）保留，由抽屉面板复用。


@router.get("/reports", response_class=HTMLResponse)
async def reports_page():
    """报告中心（按日期归档的报告快照）。"""
    return HTMLResponse((FRONTEND_DIR / "reports.html").read_text(encoding="utf-8"))


@router.get("/manifest.webmanifest")
async def pwa_manifest():
    return FileResponse(
        FRONTEND_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@router.get("/sw.js")
async def pwa_service_worker():
    # Service Worker 必须从根路径返回，才能接管整个站点；不缓存 SW 本身
    return FileResponse(
        FRONTEND_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/push.js")
async def pwa_push_js():
    return FileResponse(
        FRONTEND_DIR / "push.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/icon-192.png")
async def pwa_icon_192():
    return FileResponse(FRONTEND_DIR / "icon-192.png", media_type="image/png")


@router.get("/icon-512.png")
async def pwa_icon_512():
    return FileResponse(FRONTEND_DIR / "icon-512.png", media_type="image/png")


@router.get("/icon-maskable-512.png")
async def pwa_icon_maskable():
    return FileResponse(FRONTEND_DIR / "icon-maskable-512.png", media_type="image/png")
