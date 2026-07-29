"""prospecting/cold_start_smoketest.py — 冷启动本机验证。

用法(本机 .venv):
    # dry-run:读全书、出全量对账计划、存文件、不写任何东西(推荐先看这个):
    python -m prospecting.cold_start_smoketest "<视图URL>"

    # 只读前 N 页(快速看看):
    python -m prospecting.cold_start_smoketest "<视图URL>" pages=2

    # 小批真写(写自动集前 N 个,验证批量写回;可逆):
    python -m prospecting.cold_start_smoketest "<视图URL>" apply limit=5

    # 全量真写(= 第一次夜跑要做的事;确认计划无误后再跑):
    python -m prospecting.cold_start_smoketest "<视图URL>" apply

dry-run 会把完整计划存到 DATA_DIR/customer_loop/coldstart_plan_*.json 供你细看。
manual_demote(标 Core 却无 deal)只列不写——那批要你另行处理(降级+Opt-Out)。
"""
from __future__ import annotations

import sys
from pathlib import Path

from prospecting import hubspot_worker as w
from prospecting import cold_start
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
    if not args or not args[0].lower().startswith("http"):
        print('用法: python -m prospecting.cold_start_smoketest "<视图URL>" [apply] [limit=N] [pages=N]')
        return 5
    view_url = args[0]
    apply = "apply" in [a.lower() for a in args]
    limit = pages = None
    for a in args[1:]:
        if a.startswith("limit="):
            limit = int(a.split("=", 1)[1])
        elif a.startswith("pages="):
            pages = int(a.split("=", 1)[1])

    browser, paths, logger = _make_browser()
    print(f"[cold_start] 启动…(mode={'APPLY 真写' if apply else 'dry-run 不写'}"
          f"{f', limit={limit}' if limit else ''}{f', pages={pages}' if pages else ''})")
    try:
        browser.start(run_mode="interactive")
    except w.SessionExpiredError:
        print("[cold_start] ⚠ 登录态失效,转手动登录…")
        browser.stop()
        if do_login() != 0:
            return 4
        browser, paths, logger = _make_browser()
        browser.start(run_mode="interactive")

    try:
        s = cold_start.run(browser, view_url, apply=apply, limit=limit,
                           max_pages=pages, logger=logger)
        exp = s.get("expected_total")
        flag = "✅完整" if s.get("complete", True) else f"⚠️漏读(应有 {exp})"
        print(f"\n共读到 {s['total']} 账户 / 页面显示 {exp}  {flag}")
        print("五桶:", s["bucket_counts"])
        print("计划:", s["plan_counts"], "| 计划文件:", s["plan_file"])
        if s["manual_demote"]:
            print(f"人工降级清单({len(s['manual_demote'])}):", s["manual_demote"][:15],
                  "…" if len(s["manual_demote"]) > 15 else "")
        if s["applied"] is not None:
            ap = s["applied"]
            print(f"\n已写:{len(ap['ok'])} 成功 / {len(ap['failed'])} 失败")
            for f in ap["failed"][:10]:
                print("  失败:", f["account"], f["reason"])
        print("\n[cold_start] ✅", "已写自动集,去 HubSpot 抽查。" if apply else "dry-run 完成,细看计划文件。")
        return 0
    except RuntimeError as e:
        print(f"\n[cold_start] ⚠ {e}")
        return 2
    finally:
        browser.stop()


if __name__ == "__main__":
    raise SystemExit(main())
