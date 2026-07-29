"""prospecting/breeze_smoketest.py — 在本机验证 breeze.ask 全链路(只读)。

沙箱无法驱动 Ned 的登录态浏览器,所以这条得在【你本机的 .venv】里跑:

    cd ~/Desktop/jarvis
    source .venv/bin/activate           # 确保装了 playwright:pip install playwright && playwright install chrome

    # 首次或登录掉了 —— 先登录(开可见窗口,手动登进 HubSpot,会自动存回 profile):
    python -m prospecting.breeze_smoketest login

    # 然后跑验证(默认问"有多少 Prospecting 账户";也可自带问句):
    python -m prospecting.breeze_smoketest
    python -m prospecting.breeze_smoketest "Summarize account XXX's recent activity"

若不先手动登录也行:验证时若发现登录态失效,会自动转到手动登录、登完接着测。
全程只读,不写任何数据。

若报「找不到 Breeze widget」:多半是顶栏 Assistant 开关按钮选择器要调——
截图看真实按钮,把更准的选择器补进 breeze._OPEN_SIDEBAR_SELECTORS。
"""
from __future__ import annotations

import sys
from pathlib import Path

from prospecting import hubspot_worker as w
from prospecting import breeze


def _make_browser():
    try:
        import config
        data_dir = config.DATA_DIR
    except Exception:
        data_dir = Path.home() / ".jarvis"
    paths = w.resolve_paths(data_dir / "hubspot")
    w.ensure_directories(paths)
    logger = w.setup_logger(paths.log_file)
    return w.HubSpotBrowser(paths, logger), paths, logger


def do_login() -> int:
    """开可见窗口,等你手动登进 HubSpot,session 存回 profile。"""
    browser, paths, _ = _make_browser()
    print(f"[login] 打开可见浏览器,请在窗口里登录 HubSpot(profile={paths.profile_dir})…")
    try:
        browser.login_bootstrap()   # 可见,轮询等手动登录成功,存回 profile
        print("[login] ✅ 登录成功,session 已存回 profile。现在可跑验证:")
        print("        python -m prospecting.breeze_smoketest")
        return 0
    except w.SessionExpiredError as e:
        print(f"[login] ❌ 登录超时/失败:{e}")
        return 4
    finally:
        browser.stop()


def do_test(prompt: str) -> int:
    browser, paths, logger = _make_browser()
    print(f"[smoketest] 启动可见浏览器(profile={paths.profile_dir})…")
    try:
        browser.start(run_mode="interactive")   # 可见、非 headless
    except w.SessionExpiredError:
        # 登录掉了 —— 释放当前上下文,转手动登录,再重开测试
        print("[smoketest] ⚠ 登录态失效,转手动登录…")
        browser.stop()
        if do_login() != 0:
            return 4
        browser, paths, logger = _make_browser()
        browser.start(run_mode="interactive")

    try:
        print(f"[smoketest] 向 Breeze 提问:{prompt}")
        res = breeze.ask(browser.page, prompt, logger=logger, timeout_s=90)
        print("\n===== Breeze 答案(文本)=====")
        print(res["text"])
        if res["rows"]:
            print("\n===== 轻结构化解析(前 20 行)=====")
            for r in res["rows"][:20]:
                print(" ", r)
        print("\n[smoketest] ✅ 全链路通。")
        return 0
    except breeze.BreezeTimeout as e:
        print(f"\n[smoketest] ⏱ 超时:{e}")
        if e.partial:
            print("已渲染的部分:\n", e.partial)
        return 2
    except breeze.BreezeError as e:
        print(f"\n[smoketest] ❌ 驱动失败:{e}")
        return 3
    finally:
        browser.stop()


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg.strip().lower() == "login":
        return do_login()
    prompt = arg or "How many of my accounts are Prospecting type? Just give me the count."
    return do_test(prompt)


if __name__ == "__main__":
    raise SystemExit(main())
