"""
HubSpot 登录管理器 —— 本地调试块的后端。

让你在网页上点一下就能：首次登录 / 会话过期后重新登录 / 检测会话 / 关闭登录窗口，
不用敲代码。登录会在**运行 jarvis 的这台机器**上弹出有头 Chrome 供你手动登录，
登录态持久化到浏览器 profile，之后无人值守复用。

浏览器内核(hubspot_worker, 依赖 playwright)惰性引入——本模块导入零依赖、可安全随启动加载。
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger("jarvis.hubspot_login")


class LoginManager:
    _instance: Optional["LoginManager"] = None

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._status: str = "unknown"
        self._browser = None
        self._stop = threading.Event()

    @classmethod
    def get(cls) -> "LoginManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _runtime(self):
        """惰性准备 (worker 模块, paths, logger)。"""
        from prospecting import hubspot_worker as w
        from intel import signal_library as sl
        paths = w.resolve_paths(sl._DATA_DIR / "hubspot")
        w.ensure_directories(paths)
        return w, paths, w.setup_logger(paths.log_file)

    # ── 登录（开有头浏览器，等你手动登） ──
    def start_login(self) -> str:
        if self._thread and self._thread.is_alive():
            return "login_in_progress"
        self._status = "login_in_progress"
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        return "login_in_progress"

    def _worker(self):
        browser = None
        try:
            w, paths, lg = self._runtime()
            browser = w.HubSpotBrowser(paths, lg)
            self._browser = browser
            browser.login_bootstrap()          # 有头浏览器，等待手动登录
            self._status = "ok"
            logger.info("HubSpot 登录完成，窗口保持打开直到手动关闭")
            self._stop.wait()                  # 保持打开直到 close
        except Exception as e:
            self._status = "failed"
            logger.warning("HubSpot 登录失败：%s", e)
        finally:
            self._browser = None
            try:
                if browser is not None:
                    browser.stop()
            except Exception:
                pass

    def stop_login(self) -> str:
        self._stop.set()
        return "closing"

    def get_status(self) -> str:
        if self._thread and self._thread.is_alive():
            return "ok" if self._status == "ok" else "login_in_progress"
        return self._status

    # ── 检测会话是否有效（无头跑一下） ──
    def check_session(self) -> str:
        if self._browser is not None:           # 登录窗口开着，返回当前态
            return self._status
        browser = None
        try:
            w, paths, lg = self._runtime()
            browser = w.HubSpotBrowser(paths, lg)
            try:
                browser.start(run_mode="background")
                self._status = "ok"
                return "ok"
            except w.SessionExpiredError:
                self._status = "expired"
                return "expired"
            except Exception:
                return "unknown"
            finally:
                browser.stop()
        except Exception as e:
            logger.warning("check_session 失败：%s", e)
            return "unknown"
