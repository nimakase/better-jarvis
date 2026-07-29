"""prospecting/reply_apply_smoketest.py — 回复路整条 + 批准写入(默认 dry-run)。

读全书 → 找回信候选 → Breeze 分类 → 存待确认提议 → 批准落地(建 Task + 设 priority)。
"他说了什么"主存贾维斯库,不进 CRM;CRM 只写 Task/priority(Opt-Out/对外动作进"你手动"清单)。

    python -m prospecting.reply_apply_smoketest            # dry-run(分类+出提议,不真写)
    python -m prospecting.reply_apply_smoketest apply      # 真写 Task/priority(可标完成下沉)

Breeze 逐候选慢;若报"未登录"先 breeze_smoketest login。
"""
from __future__ import annotations

import sys
from pathlib import Path

from prospecting import hubspot_worker as w
from prospecting import account_reader as reader
from prospecting import reply_path, reply_classify
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
    apply = "apply" in [a.lower() for a in sys.argv[1:]]
    browser, paths, logger = _make_browser()
    print(f"[reply_apply] 启动…（{'APPLY 真写' if apply else 'dry-run'}）")
    try:
        browser.start(run_mode="interactive")
    except w.SessionExpiredError:
        print("[reply_apply] ⚠ 登录态失效,转手动登录…")
        browser.stop()
        if do_login() != 0:
            return 4
        browser, paths, logger = _make_browser()
        browser.start(run_mode="interactive")

    try:
        browser.page.goto(DEFAULT_VIEW_URL, wait_until="domcontentloaded")
        browser.page.wait_for_timeout(2000)
        print("[reply_apply] 读全书 + 分类候选(存待确认提议)…")
        report = reader.grade_all(browser)
        if not report.get("complete", True):
            print(f"⚠ 读取不完整({report['total']}/{report.get('expected_total')}),重跑")
            return 2

        def _classify(acct):
            print(f"  · 分类 {acct} …")
            return reply_classify.classify(browser.page, acct, logger=logger, timeout_s=90)

        run_res = reply_path.run(report.get("records", []), _classify)
        print(f"\n候选 {run_res['candidates']} → 新提议 {len(run_res['proposals'])} 条")
        for p in run_res["proposals"]:
            print(f"  [{p['account']}] {p['classification']['category']} → "
                  f"task={p['proposal']['task']} priority={p['proposal']['priority']}")

        print(f"\n[reply_apply] {'批准写入' if apply else 'dry-run 落地流程'}…")
        ap = reply_path.apply_proposals(browser, DEFAULT_VIEW_URL, apply=apply, logger=logger)
        print(f"  Task {ap['tasks']} / priority {ap['priorities']} / 失败 {len(ap['failed'])}")
        if ap["manual"]:
            print("  需你手动:")
            for m in ap["manual"]:
                print(f"    - {m['account']}: {m['do']}")
        for f in ap["failed"][:8]:
            print("  失败:", f)
        print("\n[reply_apply] ✅", "已写(去记录页/任务看)。" if apply else "dry-run 通(未写)。")
        return 0
    except Exception as e:
        print(f"[reply_apply] ❌ {type(e).__name__}: {e}")
        return 2
    finally:
        browser.stop()


if __name__ == "__main__":
    raise SystemExit(main())
