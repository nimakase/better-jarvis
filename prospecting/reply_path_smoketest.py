"""prospecting/reply_path_smoketest.py — 本机验证整条回复路(只读,不写不存)。

读全书 → 找"最近 N 天有 engagement"的候选 → 对每个让 Breeze 读回信分类 → 路由出提议 → 打印。
只读:不写 HubSpot、也不存进待确认库(用 build_proposals,不走 run 的存储)。

    python -m prospecting.reply_path_smoketest            # 默认最多分类前 5 个候选
    python -m prospecting.reply_path_smoketest 10         # 前 10 个
    python -m prospecting.reply_path_smoketest 0          # 全部候选(可能很多、很慢)

Breeze 每个候选约 10-60s,故默认限 5 个。若报"未登录",先 breeze_smoketest login。
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
    limit = 5
    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
        except ValueError:
            pass

    browser, paths, logger = _make_browser()
    print("[reply_path] 启动浏览器…")
    try:
        browser.start(run_mode="interactive")
    except w.SessionExpiredError:
        print("[reply_path] ⚠ 登录态失效,转手动登录…")
        browser.stop()
        if do_login() != 0:
            return 4
        browser, paths, logger = _make_browser()
        browser.start(run_mode="interactive")

    try:
        browser.page.goto(DEFAULT_VIEW_URL, wait_until="domcontentloaded")
        browser.page.wait_for_timeout(2000)
        print("[reply_path] 读全书 + 找回信候选…")
        report = reader.grade_all(browser)
        if not report.get("complete", True):
            print(f"⚠ 读取不完整({report['total']}/{report.get('expected_total')}),先重跑")
            return 2
        cands = reply_path.find_reply_candidates(report.get("records", []))
        print(f"最近有 engagement 的候选:{len(cands)} 个")
        pick = cands if limit == 0 else cands[:limit]
        print(f"这次分类前 {len(pick)} 个(Breeze 逐个读回信,慢)…\n")

        def _classify(acct):
            print(f"  · 分类 {acct} …")
            return reply_classify.classify(browser.page, acct, logger=logger, timeout_s=90)

        props = reply_path.build_proposals(pick, _classify)   # 不走 store,只看提议
        print(f"\n===== 出了 {len(props)} 条提议(将进日报待确认,未写)=====")
        for p in props:
            c = p["classification"]
            pr = p["proposal"]
            print(f"\n[{p['account']}] {c['category']}  回信 {c.get('reply_date')}")
            print(f"   说了:{c.get('summary')}")
            print(f"   提议:Note={pr['make_note']} tag='{pr['note_tag']}' "
                  f"task={pr['task']} priority={pr['priority']} "
                  f"opt_out={pr['sequence_opt_out']}")
            if pr.get("your_action"):
                print(f"   需你手动:{pr['your_action']}")
        no_reply = len(pick) - len(props)
        print(f"\n(其余 {no_reply} 个候选:无入站回信/无动作)")
        print("[reply_path] ✅ 只读跑通,未写未存。")
        return 0
    except Exception as e:
        print(f"[reply_path] ❌ 失败:{type(e).__name__}: {e}")
        return 2
    finally:
        browser.stop()


if __name__ == "__main__":
    raise SystemExit(main())
