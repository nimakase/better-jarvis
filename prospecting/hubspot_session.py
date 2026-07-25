"""
HubSpot 会话管理 —— 全系统唯一的「拉起浏览器 + 拿到已登录 page」入口。

之前有三套各自为政的登录/拉起逻辑：
  - prospect 轨走 HubSpotBrowser.start()（绑死默认搜索视图，异常类型有三种，
    只 catch 一种就会连有头登录窗口都不打开——screener 实测踩过）
  - oem_ems_screener 自己写了一套（headless 先试 → 有头轮询等登录）——已验证可靠
  - login_manager 又是一套（login_bootstrap）
profile 目录本来就共用（JARVIS_DATA_DIR/hubspot，登录一次全家都认），
没共用的只是代码。本模块把 screener 那套【已验证】的流程提出来，三处统一走这里。

核心约束：Chrome persistent profile 有进程锁（ProcessSingleton），同一时刻只能有
一个 context 拿着它。所以：
  1. 模块内一把锁串行化本进程内的 launch，避免自己人打架；
  2. 拿不到锁（别的工具正开着）时 launch 明确报"profile 可能被占用"，
     调用方按自己的策略处理（prospect 轨：存盘待续跑；screener：报错给用户）。

本模块只依赖 hubspot_worker 的 HubSpotBrowser（借 detect_auth_state）与
STEALTH_CHROMIUM_ARGS；playwright 惰性 import，导入本模块零依赖。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

# 与 screener 实测校准过的参数
_LAUNCH_RETRIES = 3
_PROFILE_UNLOCK_COOLDOWN = 2.5   # 关掉 context 后 Chrome 释放 profile 锁是异步的
_LOGIN_WAIT_SECONDS = 300
_LOGIN_POLL_SECONDS = 2
# 登录态判定必须【轮询】而不是单拍：HubSpot 是 SPA，goto(domcontentloaded) 返回时
# 应用壳还在渲染，搜索框/表格都没出现、URL 又没跳 login → detect_auth_state 只能
# 返回 "unknown"。单拍拿到 unknown 就判未登录，会在【cookie 完全有效】的情况下
# 误报——这正是 prospect preflight 实跑翻车的原因（screener 侥幸没翻，是因为它
# headless 失败后会开有头窗口再轮询 300 秒，页面渲染完就自愈了）。
# 45s 与 worker.start() 的 STARTUP_AUTH_WAIT_SECONDS 同档；已登录时通常几秒内返回，
# 真未登录时 URL 跳 login 页也很快能判出来，不会白等满 45 秒。
_AUTH_RESOLVE_SECONDS = 45
_DEFAULT_VIEWPORT = {"width": 1440, "height": 960}

# 本进程内串行化 launch（跨进程靠 Chrome 自己的 ProcessSingleton 报错兜底）
_LAUNCH_LOCK = threading.Lock()


class Session:
    """一次已就绪的浏览器会话。用完必须 close()（或用 with）。"""

    def __init__(self, playwright, context, page, note: str = ""):
        self.playwright = playwright
        self.context = context
        self.page = page
        self.note = note          # 如"本次需要手动登录，会话已保存"

    def attach(self, browser) -> None:
        """把会话挂到 HubSpotBrowser 实例上，复用它的取数/匹配方法。"""
        browser.playwright = self.playwright
        browser.context = self.context
        browser.page = self.page

    def close(self) -> None:
        for obj, fn in ((self.context, "close"), (self.playwright, "stop")):
            try:
                if obj is not None:
                    getattr(obj, fn)()
            except Exception:
                pass
        self.context = self.playwright = self.page = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def launch(paths, headless: bool, viewport: Optional[dict] = None):
    """启动 persistent context，返回 (pw, ctx, page)。

    带退避重试：刚关掉的 Chrome 释放 profile 锁是异步的，紧接着再启动会报
    ProcessSingleton——这正是「窗口一闪就没」的常见成因（screener 实测）。
    """
    from playwright.sync_api import sync_playwright
    from prospecting.hubspot_worker import STEALTH_CHROMIUM_ARGS

    last = None
    with _LAUNCH_LOCK:
        for attempt in range(1, _LAUNCH_RETRIES + 1):
            pw = sync_playwright().start()
            try:
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=str(paths.profile_dir),
                    channel="chrome",
                    headless=headless,
                    ignore_default_args=["--enable-automation"],
                    args=STEALTH_CHROMIUM_ARGS,
                    viewport=viewport or _DEFAULT_VIEWPORT,
                )
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.set_default_timeout(30000)
                page.set_default_navigation_timeout(60000)
                return pw, ctx, page
            except Exception as e:
                last = e
                try:
                    pw.stop()
                except Exception:
                    pass
                if attempt < _LAUNCH_RETRIES:
                    time.sleep(_PROFILE_UNLOCK_COOLDOWN * attempt)
    raise RuntimeError(f"启动浏览器失败（profile 可能被别的工具占用）：{last}")


def _close_quietly(pw, ctx) -> None:
    for obj, fn in ((ctx, "close"), (pw, "stop")):
        try:
            if obj is not None:
                getattr(obj, fn)()
        except Exception:
            pass


def acquire(paths, lg, view_url: str, *,
            headed_login: bool = True,
            login_wait_seconds: int = _LOGIN_WAIT_SECONDS,
            viewport: Optional[dict] = None) -> Session:
    """拿到一个【已登录、已停在 view_url 上】的会话。

    流程（screener 验证过的那套）：
      1. headless 先试——已登录时零打扰；
      2. 未登录且 headed_login=True → 开有头窗口停在目标视图，【轮询】等手动登录
         （绝不用 input()：服务进程没有 stdin，会 EOFError 或永久挂起）；
      3. headed_login=False（无人值守场景）→ 直接抛 SessionExpiredError，
         由调用方善后（如 prospect 轨的存盘待续跑）。

    失败一律抛异常；成功返回 Session（调用方负责 close）。
    """
    from prospecting.hubspot_worker import HubSpotBrowser, SessionExpiredError

    probe = HubSpotBrowser(paths, lg)   # 只借 detect_auth_state，不调 start()

    # 快路径：headless 试一次。【启动本身也要包在 try 里】——profile 被占用等
    # 原因会让 launch 直接抛；若它在 try 外面就会跳过有头兜底，表现为
    # 「什么窗口都没弹、只回一句报错」。
    pw = ctx = None
    try:
        pw, ctx, page = launch(paths, headless=True, viewport=viewport)
        page.goto(view_url, wait_until="domcontentloaded")
        probe.page = page
        # 必须轮询等 SPA 渲染出可判定的状态（见 _AUTH_RESOLVE_SECONDS 注释）；
        # wait_for_auth_ready 见到 login_page/expired 会立刻返回，不会白等。
        state = probe.wait_for_auth_ready(timeout_seconds=_AUTH_RESOLVE_SECONDS)
        if state == "ok":
            lg.info("hubspot_session: 已登录，headless 直接进入视图")
            return Session(pw, ctx, page)
        lg.info("hubspot_session: headless 探测到未登录（state=%s）", state)
    except Exception as e:
        lg.warning("hubspot_session: headless 阶段失败（%s）", e)

    _close_quietly(pw, ctx)

    if not headed_login:
        raise SessionExpiredError(
            "HubSpot 会话无效，且当前场景不允许弹出登录窗口（无人值守）。"
            "请在网页「本地调试」里登录后重试。")

    time.sleep(_PROFILE_UNLOCK_COOLDOWN)   # 等 Chrome 放开 profile 锁

    pw, ctx, page = launch(paths, headless=False, viewport=viewport)
    probe.page = page
    try:
        page.goto(view_url, wait_until="domcontentloaded")
        page.bring_to_front()   # macOS 上从工作线程启动时窗口可能不抢焦点
    except Exception as e:
        lg.warning("hubspot_session: 有头窗口导航失败：%s", e)

    lg.info("hubspot_session: 已打开可见浏览器窗口，等待手动登录（上限 %ds）",
            login_wait_seconds)
    deadline = time.time() + login_wait_seconds
    while time.time() < deadline:
        try:
            if probe.detect_auth_state() == "ok":
                lg.info("hubspot_session: 检测到登录成功")
                return Session(pw, ctx, page,
                               note="本次需要手动登录，会话已保存，下次可无人值守。")
        except Exception:
            pass
        time.sleep(_LOGIN_POLL_SECONDS)

    _close_quietly(pw, ctx)
    raise SessionExpiredError(
        f"已打开浏览器窗口并等待 {login_wait_seconds} 秒，但仍未检测到登录成功")


def check(paths, lg) -> str:
    """headless 探测会话状态，探完即关。返回 "ok" / "expired" / "unknown"。"""
    from prospecting.hubspot_worker import HubSpotBrowser, HUBSPOT_SEARCH_URL

    pw = ctx = None
    try:
        pw, ctx, page = launch(paths, headless=True)
        page.goto(HUBSPOT_SEARCH_URL, wait_until="domcontentloaded")
        probe = HubSpotBrowser(paths, lg)
        probe.page = page
        # 同 acquire：轮询而不是单拍，否则 SPA 渲染完成前必误判 expired
        state = probe.wait_for_auth_ready(timeout_seconds=_AUTH_RESOLVE_SECONDS)
        if state == "ok":
            return "ok"
        if state in ("login_page", "expired_alert"):
            return "expired"
        return "unknown"
    except Exception as e:
        lg.warning("hubspot_session.check 失败：%s", e)
        return "unknown"
    finally:
        _close_quietly(pw, ctx)
