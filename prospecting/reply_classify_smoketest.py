"""prospecting/reply_classify_smoketest.py — 本机验证 Breeze 读回信分类(只读)。

拿一个真账户,让 Breeze 读它最新入站回信、归类、抽信息,再打印【分类 + 路由提议】。
只读,不写任何东西(提议本来就走日报待确认)。

    python -m prospecting.reply_classify_smoketest "Ericsson Brazil"

若报"未登录",先 `python -m prospecting.breeze_smoketest login` 刷登录。
"""
from __future__ import annotations

import sys
from pathlib import Path

from prospecting import hubspot_worker as w
from prospecting import reply_classify as rc
from prospecting import reply_router as rr
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
    if len(sys.argv) < 2:
        print('用法: python -m prospecting.reply_classify_smoketest "<账户名>"')
        return 5
    account = sys.argv[1]

    browser, paths, logger = _make_browser()
    print(f"[reply] 启动浏览器…")
    try:
        browser.start(run_mode="interactive")
    except w.SessionExpiredError:
        print("[reply] ⚠ 登录态失效,转手动登录…")
        browser.stop()
        if do_login() != 0:
            return 4
        browser, paths, logger = _make_browser()
        browser.start(run_mode="interactive")

    try:
        from prospecting import breeze
        browser.page.goto(DEFAULT_VIEW_URL, wait_until="domcontentloaded")
        browser.page.wait_for_timeout(2000)

        # 步骤①:Breeze 抽事实(不判类别)
        print(f"[reply] 步骤① 让 Breeze 抽「{account}」最新回信的事实…(约 10-60s)")
        res = breeze.ask(browser.page, rc.build_extract_prompt(account), logger=logger, timeout_s=90)
        fact = rc.parse_extract(res.get("text", ""))
        if fact is None:
            print("[reply] 步骤①:无入站回信(found:false)。若确有回信,把 raw 贴回来:")
            print("  raw:", (res.get("text") or "")[:500])
            return 0
        print("\n===== 步骤① Breeze 抽的事实 =====")
        for k, v in fact.items():
            print(f"  {k}: {v}")

        # 步骤②:贾维斯自己的 LLM 判类别
        print("\n[reply] 步骤② 贾维斯 LLM 判类别…")
        verdict = rc.classify_reply_text(fact["reply_text"], account)
        if verdict is None:
            print("[reply] 判成 none —— OOO/自动回复/无实质(或 LLM 未配)→ 非真回复,不生成提议。")
            print("        (若这条其实是真回复被误判,说明 LLM 侧要调;若 LLM 没配会静默降级成 None)")
            return 0
        print("\n===== 步骤② 贾维斯 LLM 判类别 =====")
        for k, v in verdict.items():
            print(f"  {k}: {v}")
        prop = rr.route(verdict["category"], stock_wake_days=verdict.get("stock_wake_days"),
                        account_name=account)
        print("\n===== 路由提议(将进日报待确认,不自动写)=====")
        for k, v in prop.items():
            print(f"  {k}: {v}")
        print("\n[reply] ✅ 抽事实→LLM 判→提议 全链路通(只读,未写)。")
        return 0
    except Exception as e:
        print(f"[reply] ❌ 失败:{type(e).__name__}: {e}")
        return 2
    finally:
        browser.stop()


if __name__ == "__main__":
    raise SystemExit(main())
