"""prospecting/account_write_smoketest.py — 单账户写入验证(③ 的本机验证)。

沙箱碰不到真实写入,故在第一次全量夜跑前,先用这个在【一个账户】上验写入机制。

用法(本机 .venv):
    # dry-run(走到填好值就 Cancel,不落库,验流程):
    python -m prospecting.account_write_smoketest "<视图URL>" "Ericsson Brazil" priority warm

    # 真写(会改这一个账户,可逆):末尾加 apply
    python -m prospecting.account_write_smoketest "<视图URL>" "Ericsson Brazil" priority warm apply

field ∈ priority(值 hot/warm/cold)| type(值 core/prospecting)。
先 dry-run 跑通(确认能选中账户、进弹窗、填对值、Cancel 干净),再 apply 真写一个,
去 HubSpot 眼见为实。任一步选择器不对,把输出贴回来即可修。
"""
from __future__ import annotations

import sys
from pathlib import Path

from prospecting import hubspot_worker as w
from prospecting import account_writer as writer
from prospecting.breeze_smoketest import do_login


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


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 4 or not args[0].lower().startswith("http"):
        print('用法: python -m prospecting.account_write_smoketest '
              '"<视图URL>" "<账户名>" <priority|type> <值> [apply]')
        return 5
    view_url, account, field, value = args[0], args[1], args[2], args[3]
    apply = len(args) >= 5 and args[4].lower() == "apply"

    browser, paths, logger = _make_browser()
    print(f"[write] 启动浏览器…(mode={'APPLY 真写' if apply else 'dry-run 不写'})")
    try:
        browser.start(run_mode="interactive")
    except w.SessionExpiredError:
        print("[write] ⚠ 登录态失效,转手动登录…")
        browser.stop()
        if do_login() != 0:
            return 4
        browser, paths, logger = _make_browser()
        browser.start(run_mode="interactive")

    try:
        browser.page.goto(view_url, wait_until="domcontentloaded")
        browser.page.wait_for_timeout(2500)
        print(f"[write] {'真写' if apply else 'dry-run'}: {account} 的 {field} → {value}")
        res = writer.set_property(browser, account, field, value,
                                  view_url=view_url, apply=apply, logger=logger)
        print("结果:", res)
        if res.get("ok"):
            print("[write] ✅", "已写入,去 HubSpot 核对。" if apply else "dry-run 流程通(未落库)。")
            return 0
        print("[write] ❌ 未成功:", res.get("reason"))
        return 2
    finally:
        browser.stop()


if __name__ == "__main__":
    raise SystemExit(main())
