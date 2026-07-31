"""prospecting/view_pipeline_smoketest.py — view 管线实盘校准探针(本机跑)。

专门校准两处浏览器驱动的选择器:
  1) Breeze 抽取(breeze_outreach.ask_breeze):对一个账户问 outreach,打印解析结果;
  2) 名单法写 view(view_writer.set_view_membership):对一个 view **dry-run**(填好值不点 Set values)。

哪步选择器不对,把报错/输出贴回来即可精确修。默认全程不写库、不改 view(dry-run)。

用法(本机 .venv,需已登录):
    # 只校准 Breeze 抽取:
    python -m prospecting.view_pipeline_smoketest breeze "Insta Elektro"

    # 只校准名单法(dry-run 到某个已建好 Account-name-contains-exactly 过滤器的 view):
    python -m prospecting.view_pipeline_smoketest view "<视图URL>" "Acct A" "Acct B"
"""
from __future__ import annotations

import sys
from pathlib import Path

from prospecting import hubspot_worker as w
from prospecting import breeze_outreach, view_writer
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


def _start(browser):
    try:
        browser.start(run_mode="interactive")
    except w.SessionExpiredError:
        print("[probe] ⚠ 登录态失效,转手动登录…")
        browser.stop()
        if do_login() != 0:
            raise SystemExit(4)
        b2, _, _ = _make_browser()
        b2.start(run_mode="interactive")
        return b2
    return browser


_DUMP_JS = r"""(kw) => {
  const rx = new RegExp(kw, 'i');
  const g = (el,a)=>el.getAttribute(a)||'';
  const vis = el => !!(el.offsetParent || el.getClientRects().length);
  const desc = el => ({tag:el.tagName, st:g(el,'data-selenium-test'), tid:g(el,'data-test-id'),
    aria:g(el,'aria-label'), title:g(el,'title'), txt:(el.innerText||'').trim().slice(0,25), vis:vis(el)});
  const inputs = [...document.querySelectorAll('[contenteditable="true"],textarea,[role="textbox"]')]
    .map(el=>({tag:el.tagName, st:g(el,'data-selenium-test'), tid:g(el,'data-test-id'),
      ph:g(el,'placeholder')||g(el,'data-placeholder')||g(el,'aria-label'),
      ce:g(el,'contenteditable'), vis:vis(el)}));
  const tagged = [...document.querySelectorAll('[data-selenium-test],[data-test-id],[aria-label],[title]')]
    .filter(el=>rx.test(g(el,'data-selenium-test')+' '+g(el,'data-test-id')+' '+g(el,'aria-label')+' '+g(el,'title')))
    .filter(vis).slice(0,50).map(desc);
  // 无文字的图标按钮(如铅笔),带上邻近文本帮定位
  const iconBtns = [...document.querySelectorAll('button')]
    .filter(b=>vis(b) && b.querySelector('svg') && !(b.innerText||'').trim())
    .map(b=>({st:g(b,'data-selenium-test'), tid:g(b,'data-test-id'), aria:g(b,'aria-label'),
      title:g(b,'title'), near:(b.closest('div')?.parentElement?.innerText||'').trim().replace(/\s+/g,' ').slice(0,50)}))
    .filter(b=>/contains|paragon|skyworth|account name|value/i.test(b.near) ||
               /edit|value|pencil/i.test(b.st+' '+b.tid+' '+b.aria+' '+b.title))
    .slice(0,25);
  return {inputs, tagged, iconBtns};
}"""


def _dump(page, kw: str):
    for fr in page.frames:
        try:
            r = fr.evaluate(_DUMP_JS, kw)
        except Exception as e:
            print(f"  [frame {fr.url[:60]}] evaluate 失败: {e}")
            continue
        if r["inputs"] or r["tagged"] or r.get("iconBtns"):
            print(f"  ── frame: {fr.url[:80] or '(main)'} ──")
            for i in r["inputs"]:
                print("    input:", i)
            for t in r["tagged"]:
                print("    tagged:", t)
            for b in r.get("iconBtns", []):
                print("    iconBtn:", b)


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("breeze", "view", "dump", "cycle"):
        print('用法: python -m prospecting.view_pipeline_smoketest breeze "<账户名>"'
              ' | view "<视图URL>" "<账户名>"... | dump breeze|view "<URL>"'
              ' | cycle [apply] [limit=N]')
        return 5

    browser, paths, logger = _make_browser()
    browser = _start(browser)
    try:
        if args[0] == "cycle":
            from prospecting import view_manager
            from prospecting.view_config import VIEW_URL_MAP
            apply = "apply" in [a.lower() for a in args]
            limit = None
            for a in args[1:]:
                if a.startswith("limit="):
                    limit = int(a.split("=", 1)[1])
            missing = [k for k, v in VIEW_URL_MAP.items() if not v]
            if missing:
                print("[cycle] ⚠ 未配 view URL 的段(会跳过):", missing, "→ 去 view_config.py 回填")
            print(f"[cycle] {'APPLY 真写' if apply else 'dry-run 不改 view'}"
                  f"{f' | limit={limit}' if limit else ''} …(逐账户 Breeze,较慢)")
            res = view_manager.run_view_cycle(browser, VIEW_URL_MAP, apply=apply,
                                              limit=limit, logger=logger)
            print("结果:", res)
            return 0

        if args[0] == "dump":
            sub = args[1] if len(args) > 1 else "breeze"
            page = browser.page
            if sub == "view" and len(args) > 2:
                page.goto(args[2], wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                try:
                    page.locator('[data-selenium-test="FiltersBar-advancedFilters"]').first.click(timeout=5000)
                    page.wait_for_timeout(3000)   # 等高级筛选浮层真打开
                except Exception as e:
                    print("  (advanced filters 点击:", repr(e)[:120], ")")
                print("[dump] filter / edit / value / 铅笔 相关:")
                _dump(page, "filter|edit|value|pencil|contains|listing|operator|property")
            else:
                for sel in ['button[aria-label="Open Assistant"]',
                            '[data-selenium-test*="opilot"]', 'button:has-text("Assistant")']:
                    try:
                        page.locator(sel).first.click(timeout=4000)
                        page.wait_for_timeout(2500)
                        break
                    except Exception:
                        continue
                print("[dump] frames:", [f.url[:70] for f in page.frames])
                print("[dump] copilot 输入 / 相关:")
                _dump(page, "copilot|assistant|chat|send|message|prompt")
            return 0

        if args[0] == "breeze":
            acct = args[1] if len(args) > 1 else "Insta Elektro"
            print(f"[probe] Breeze 抽取:{acct} …")
            res = breeze_outreach.ask_breeze(browser, acct, logger=logger)
            p = res["parsed"]
            print("found:", p.get("found"), "| 邮件数:", len(p.get("outreach_emails", [])),
                  "| 回信联系人:", p.get("replied_contacts"))
            print("按联系人聚合:")
            for c in res["contacts"]:
                print(f"  - {c['name']} | 岗位={c.get('job_title')} | {len(c['sent_dates'])}封"
                      f" | replied={c['replied']}")
            if not res["contacts"]:
                print("  (空 —— 若该账户确有 outreach,多半是【发送未触发/超时】。把 raw 前 600 字贴回来:)")
                print("  raw:", (res.get("raw") or "")[:600])
            return 0

        # view dry-run
        if len(args) < 3 or not args[1].lower().startswith("http"):
            print('用法: ...view "<视图URL>" "<账户名>"...')
            return 5
        view_url, names = args[1], args[2:]
        print(f"[probe] 名单法 dry-run:{view_url} ← {len(names)} 个账户(不点 Set values)")
        r = view_writer.set_view_membership(browser, view_url, names, apply=False, logger=logger)
        print("结果:", r)
        if not r.get("ok"):
            print("  某步选择器没命中,reason 已指出;贴回来我修(多半是那支铅笔)。")
        return 0
    finally:
        browser.stop()


if __name__ == "__main__":
    raise SystemExit(main())
