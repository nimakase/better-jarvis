"""prospecting/cross_account_probe.py — 受控【跨账户连写】探针。

专门验证那个"账户之间写入不稳"的 bug 是否已修:给定 2-3+ 个账户名,用修好的
account_writer.set_property(view_url=..., apply=True) 依次写 priority,逐个打印
ok + 抓到的成功 toast,末尾给成/败汇总。

跟 workflow_smoketest 的区别:这里只写你点名的少数几个账户(可控、可还原),
且强制走写入循环——workflow 在 dry-run 下根本不调 set_property,测不到这个 bug。

⚠ 这是真写(effect=write_external)。默认把每个点名账户的 priority 设成 warm(3),
   跑完可用同一脚本 --revert 把它们清空,或让我在浏览器里帮你还原。

用法(本机 .venv,需已登录;掉登录先 `python -m prospecting.breeze_smoketest login`):
    # 连写 3 个账户,验证不再卡在第一个之后:
    python -m prospecting.cross_account_probe \
        "https://app.hubspot.com/contacts/9311334/objects/0-2/views/68742792/list" \
        "duagon AG" "SALTO Systems" "Insta Elektro"

    # 换个目标值(hot/warm/cold):加 --value
    python -m prospecting.cross_account_probe --value cold "<视图URL>" "账户A" "账户B"

    # 还原:把点名账户的 priority 清空(设回无) —— 需要 writer 支持 clear,见下方说明
    python -m prospecting.cross_account_probe --revert "<视图URL>" "账户A" "账户B"

说明:选择器/落点见 account_writer.py 顶部注释。任一步不对,把输出贴回来即可修。
"""
from __future__ import annotations

import sys
import time
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


def _parse_args(argv):
    value, revert, accounts, view_url = "warm", False, [], None
    it = iter(argv)
    for a in it:
        if a == "--value":
            value = next(it, "warm").lower()
        elif a == "--revert":
            revert = True
        elif a.lower().startswith("http"):
            view_url = a
        else:
            accounts.append(a)
    return view_url, value, revert, accounts


def main() -> int:
    view_url, value, revert, accounts = _parse_args(sys.argv[1:])
    if not view_url or len(accounts) < 1:
        print('用法: python -m prospecting.cross_account_probe [--value hot|warm|cold] '
              '[--revert] "<视图URL>" "账户A" "账户B" ...')
        return 5
    browser, paths, logger = _make_browser()
    print(f"[probe] 启动浏览器…目标 {len(accounts)} 个账户,"
          f"{'还原清空' if revert else f'priority→{value}'}(APPLY 真写)")
    try:
        browser.start(run_mode="interactive")
    except w.SessionExpiredError:
        print("[probe] ⚠ 登录态失效,转手动登录…")
        browser.stop()
        if do_login() != 0:
            return 4
        browser, paths, logger = _make_browser()
        browser.start(run_mode="interactive")

    ok, failed = [], []
    try:
        for i, acct in enumerate(accounts, 1):
            t0 = time.time()
            if revert:
                res = writer.clear_property(browser, acct, "priority",
                                            view_url=view_url, apply=True, logger=logger)
            else:
                res = writer.set_property(browser, acct, "priority", value,
                                          view_url=view_url, apply=True, logger=logger)
            dt = time.time() - t0
            tag = "✅" if res.get("ok") else "❌"
            print(f"[probe] {i}/{len(accounts)} {tag} {acct} | {dt:.1f}s | "
                  f"toast={res.get('toast')!r} | reason={res.get('reason')}")
            (ok if res.get("ok") else failed).append(acct)
    finally:
        browser.stop()

    print(f"\n[probe] 汇总:成 {len(ok)} / 败 {len(failed)}"
          + (f" | 失败:{failed}" if failed else ""))
    print("[probe] " + ("✅ 跨账户连写全通,脏页 bug 已解。"
                        if not failed else "❌ 仍有失败,把上面每行贴回来我按 reason 修。"))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
