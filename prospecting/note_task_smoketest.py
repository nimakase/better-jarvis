"""prospecting/note_task_smoketest.py — 单账户验证 Note / Task 写入。

    # dry-run(只填不存):
    python -m prospecting.note_task_smoketest "Lorex Technology" note "Test note body"
    python -m prospecting.note_task_smoketest "Lorex Technology" task "Follow up" 90

    # 真写(末尾加 apply,会在该账户真建一条 Note/Task,可手动删):
    python -m prospecting.note_task_smoketest "Lorex Technology" note "Test note body" apply
    python -m prospecting.note_task_smoketest "Lorex Technology" task "Follow up" 90 apply
"""
from __future__ import annotations

import sys
from pathlib import Path

from prospecting import hubspot_worker as w
from prospecting import account_writer as aw
from prospecting.breeze_smoketest import do_login
from connectors.customer_loop_tools import DEFAULT_VIEW_URL


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
    a = sys.argv[1:]
    if len(a) < 3:
        print('用法: note_task_smoketest "<账户>" <note|task> "<文本/标题>" [wake_days(task)] [apply]')
        return 5
    account, kind, text = a[0], a[1].lower(), a[2]
    apply = "apply" in [x.lower() for x in a]
    wake = 90
    for x in a[3:]:
        if x.isdigit():
            wake = int(x)

    browser, paths, logger = _make_browser()
    print(f"[nt] 启动…（{'APPLY 真写' if apply else 'dry-run'}）")
    try:
        browser.start(run_mode="interactive")
    except w.SessionExpiredError:
        print("[nt] ⚠ 登录态失效,转手动登录…")
        browser.stop()
        if do_login() != 0:
            return 4
        browser, paths, logger = _make_browser()
        browser.start(run_mode="interactive")

    try:
        if kind == "note":
            res = aw.write_note(browser, account, text, DEFAULT_VIEW_URL, apply=apply, logger=logger)
        elif kind == "task":
            res = aw.create_task(browser, account, text, wake, DEFAULT_VIEW_URL, apply=apply, logger=logger)
        else:
            print("kind 只能是 note 或 task")
            return 5
        print("结果:", res)
        if res.get("ok"):
            print("[nt] ✅", "已写,去 HubSpot 记录页核对。" if apply else "dry-run 流程通(未存)。")
            return 0
        print("[nt] ❌", res.get("reason"))
        return 2
    finally:
        browser.stop()


if __name__ == "__main__":
    raise SystemExit(main())
