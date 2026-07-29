"""prospecting/account_grading_smoketest.py — 本机验证 ②b 读取 + 分级对账(只读)。

在你本机 .venv 里跑:
    cd ~/Desktop/jarvis && source .venv/bin/activate
    python -m prospecting.account_grading_smoketest          # 默认只读前 2 页,安全
    python -m prospecting.account_grading_smoketest 5        # 读前 5 页
    python -m prospecting.account_grading_smoketest all      # 读全量

【只读】,不写任何数据(写回是 ③)。作用是让你肉眼核对:
  1) 列是否都匹配上了(缺列会明确报出来,去 Edit columns 加);
  2) 四桶计数 + 抽样,看分级和对账是否符合直觉。

前提:切到一个含这些列的视图再跑——
  Account name / Account type / Priority / Number of Associated Deals /
  Number of open deals / Last activity date / Last Engagement Date
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from prospecting import hubspot_worker as w
from prospecting import account_reader as reader
from prospecting.breeze_smoketest import do_login   # 复用登录步


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
    # 参数容错:http 开头→视图 URL;"all"→全量;数字→页数;其余忽略(zsh 会把 # 当参数传)
    view_url = os.environ.get("JARVIS_GRADE_VIEW_URL")
    max_pages = 2
    for a in sys.argv[1:]:
        a = a.strip()
        if a.lower().startswith("http"):
            view_url = a
        elif a.lower() == "all":
            max_pages = None
        else:
            try:
                max_pages = int(a)
            except ValueError:
                pass

    if not view_url:
        print("⚠ 没给视图 URL。HubSpotBrowser 默认打开的是老视图(缺 deal/engagement 列)。\n"
              "  把你新建那个全字段视图的 URL 传进来,例如:\n"
              "    python -m prospecting.account_grading_smoketest "
              "\"https://app.hubspot.com/contacts/9311334/objects/0-2/views/<你的视图ID>/list\"\n"
              "  (或设环境变量 JARVIS_GRADE_VIEW_URL)")
        return 5

    browser, paths, logger = _make_browser()
    print(f"[grade] 启动浏览器(profile={paths.profile_dir})…")
    try:
        browser.start(run_mode="interactive")
    except w.SessionExpiredError:
        print("[grade] ⚠ 登录态失效,转手动登录…")
        browser.stop()
        if do_login() != 0:
            return 4
        browser, paths, logger = _make_browser()
        browser.start(run_mode="interactive")

    try:
        print(f"[grade] 跳转到你的视图:{view_url}")
        browser.page.goto(view_url, wait_until="domcontentloaded")
        browser.page.wait_for_timeout(2500)   # 等表格渲染
        limit_txt = "全量" if max_pages is None else f"前 {max_pages} 页"
        print(f"[grade] 读取并分级({limit_txt})…")
        report = reader.grade_all(browser, max_pages=max_pages)

        print(f"\n共 {report['total']} 个账户")
        print("四桶计数:")
        for k, n in report["counts"].items():
            print(f"  {k:18s} {n}")

        # 每桶抽 5 个看看
        for bucket, items in report["buckets"].items():
            if not items:
                continue
            print(f"\n--- {bucket}(抽样)---")
            for it in items[:5]:
                c, e = it["computed"], it["existing"]
                print(f"  {it['account_name'][:32]:32s} "
                      f"算={c['type']}/{c['priority']}  现={e['type']}/{e['priority']}")
        print("\n[grade] ✅ 只读跑通。核对无误后我们再上 ③(对账式写回)。")
        return 0
    except RuntimeError as e:
        print(f"\n[grade] ⚠ {e}")
        return 2
    finally:
        browser.stop()


if __name__ == "__main__":
    raise SystemExit(main())
