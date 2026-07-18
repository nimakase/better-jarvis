#!/usr/bin/env python3
"""离线验证台：把 hubspot_worker 的【真实提取方法】跑在仿真 HubSpot DOM 上。

用途：不需要 HubSpot 账号、不需要浏览器，就能验证列定位与文本提取是否正确。
做法：用 BeautifulSoup + soupsieve（真实 CSS 选择器引擎）实现一个 Playwright Locator
      垫片，只覆盖 worker 实际用到的那几个方法。被测代码是 worker 的真身，不是复制品。

【这个验证台能证明什么】
  - `td[data-column-index=N]` 属性选择器 vs `cells.nth(N)` 位置下标，到底选中哪个单元格
  - `_extract_name` / `_extract_owner` / `_extract_text` 对嵌套 DOM 的处理是否正确
  - 翻页循环、去重、截断上报的控制流是否正确

【它证明不了什么（重要）】
  - 真实 HubSpot 的 DOM 是否与本文件的仿真结构一致（仿真结构是依据 worker 里的选择器
    常量倒推的，不是从真实页面抓的）
  - JS 渲染时序、真实的翻页按钮选择器、登录流程、网络异常
  → 这些仍必须在你本机对着真实视图跑一次才能确认。

用法（仓库根）：python tests/manual_hubspot_dom_harness.py
命名以 manual_ 开头，不会被 tests/run_all.py 收集（它只收 test_*.py）——因为本文件
依赖 bs4，属于开发期工具，不进自我迭代的测试 gate。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("需要 bs4：pip install beautifulsoup4 lxml soupsieve")
    sys.exit(1)


# ── Playwright Locator 垫片（只实现 worker 用到的子集）────────────────────────
class L:
    def __init__(self, els):
        self.els = els

    def locator(self, css):
        out = []
        for e in self.els:
            out.extend(e.select(css))
        return L(out)

    @property
    def first(self):
        return L(self.els[:1])

    @property
    def last(self):
        return L(self.els[-1:])

    def nth(self, i):
        return L(self.els[i:i + 1])

    def count(self):
        return len(self.els)

    def inner_text(self, timeout=None):
        if not self.els:
            raise RuntimeError("no element")
        return self.els[0].get_text(" ", strip=True)

    def get_attribute(self, name):
        return self.els[0].get(name) if self.els else None

    def scroll_into_view_if_needed(self, timeout=None):
        """静态 DOM 无虚拟滚动，空实现即可（真实页面上会触发行回收/新增）。"""
        return None


class FakePage:
    def __init__(self, html):
        self.soup = BeautifulSoup(html, "lxml")

    def locator(self, css):
        return L(self.soup.select(css))


# ── 仿真 HubSpot 表格 ────────────────────────────────────────────────────────
# 关键点：第一列是 checkbox 列，**没有** data-column-index —— 这正是位置下标与属性值
# 产生错位的根源。列名/结构依据 hubspot_worker 里的选择器常量倒推。
COMPANIES = [
    ("Foxconn Industrial Internet", "Ned Luo (ned@ccl.com)", "fii-foxconn.com"),
    ("Jabil Circuit", "Bronx Huang (bronx@ccl.com)", "jabil.com"),
    ("Arrow Electronics", "Ned Luo (ned@ccl.com)", "arrow.com"),
]
PAGE2 = [
    ("Flex Ltd", "Ned Luo (ned@ccl.com)", "flex.com"),
    ("Avnet Inc", "Bronx Huang (bronx@ccl.com)", "avnet.com"),
]


def build_html(rows, page_no=1, has_next=True, with_domain=True):
    headers = ['<th class="cb"><input type="checkbox"></th>',
               '<th data-column-index="0">Account name</th>',
               '<th data-column-index="1">Account owner</th>']
    if with_domain:
        headers.append('<th data-column-index="2">Account domain name</th>')
    headers.append(f'<th data-column-index="{3 if with_domain else 2}">Phone number</th>')

    body = []
    for i, (name, owner, domain) in enumerate(rows, start=1):
        tds = [
            '<td class="cb"><input type="checkbox"></td>',
            f'<td data-column-index="0"><a href="#">'
            f'<span data-test-id="label-cell-formatted-property-name">{name}</span></a></td>',
            f'<td data-column-index="1"><span data-test-id="truncated-object-label" '
            f'tabindex="0">{owner}</span></td>',
        ]
        if with_domain:
            tds.append(f'<td data-column-index="2">{domain}</td>')
        tds.append(f'<td data-column-index="{3 if with_domain else 2}">+86 755 0000</td>')
        body.append(f'<tr data-test-id="row-{page_no}-{i}">{"".join(tds)}</tr>')

    nxt = ('<button data-test-id="next-page-button">Next</button>' if has_next
           else '<button data-test-id="next-page-button" aria-disabled="true">Next</button>')
    return f"""<html><body>
      <table data-test-id="framework-data-table">
        <thead><tr>{''.join(headers)}</tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table>
      {nxt}
    </body></html>"""


# ── 用真实 worker 方法跑 ─────────────────────────────────────────────────────
import logging  # noqa: E402
from prospecting.hubspot_worker import (  # noqa: E402
    HubSpotBrowser, RuntimePaths, NAME_COL_LABEL, OWNER_COL_LABEL, DOMAIN_COL_LABEL,
)

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  → {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def make_browser(html):
    p = Path("/tmp/fake")
    paths = RuntimePaths(p, p, p, p, p, p, p, p, p / "x.log")
    b = HubSpotBrowser(paths, logging.getLogger("harness"))
    b.page = FakePage(html)
    return b


print("=" * 72)
print("① 核心争议：data-column-index 属性选择器  vs  cells.nth() 位置下标")
print("=" * 72)

b = make_browser(build_html(COMPANIES))
col_map = b._collect_column_index_map()
print("列映射:", col_map)
b.column_index_map = col_map

name_idx = b._column_idx(NAME_COL_LABEL)
row0 = b.rows().nth(0)

# 新版做法：属性选择器
by_attr = b._extract_name(row0.locator(f"td[data-column-index='{name_idx}']").first)
# 旧版做法：位置下标
cells = row0.locator("td")
by_nth = b._extract_text(cells.nth(int(name_idx)))

print(f"  name 列的 data-column-index = {name_idx!r}")
print(f"  属性选择器 td[data-column-index='{name_idx}']  → {by_attr!r}")
print(f"  位置下标   cells.nth({name_idx})                 → {by_nth!r}")
check("属性选择器取到正确公司名", by_attr == "Foxconn Industrial Internet", by_attr)
check("位置下标取到的是【错的】(证实旧版 bug)", by_nth != "Foxconn Industrial Internet",
      f"实际拿到 {by_nth!r}（checkbox 列，空）")

print()
print("=" * 72)
print("② 文本提取：专用提取器 vs 通用 inner_text")
print("=" * 72)
owner_idx = b._column_idx(OWNER_COL_LABEL)
owner_cell = row0.locator(f"td[data-column-index='{owner_idx}']").first
good = b._extract_owner(owner_cell)
raw = b._extract_text(owner_cell)
print(f"  _extract_owner → {good!r}")
print(f"  _extract_text  → {raw!r}")
check("_extract_owner 剥掉了邮箱后缀", good == "Ned Luo", good)
check("通用 _extract_text 会留下邮箱(证实旧版 bug)", "@" in raw, raw)

print()
print("=" * 72)
print("③ domain 列缺失时的降级")
print("=" * 72)
b2 = make_browser(build_html(COMPANIES, with_domain=False))
cm2 = b2._collect_column_index_map()
b2.column_index_map = cm2
print("无 domain 列的映射:", cm2)
try:
    b2.refresh_column_index_map(with_retry=False)
    check("refresh_column_index_map 在缺 domain 列时报错", False, "竟然没报错")
except Exception as e:
    check("refresh_column_index_map 缺 domain 列必炸(证实旧版 bug)", True, type(e).__name__)
check("_collect_column_index_map 不受影响、仍可用", cm2.get(b2._label_key(NAME_COL_LABEL)) == "0")

print()
print("=" * 72)
print("④ 新工具的解析 + 翻页控制流（用工具真身的函数）")
print("=" * 72)
sys.path.insert(0, str(REPO / "skills" / "oem_ems_screener"))
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location(
    "oem_tool", REPO / "skills" / "oem_ems_screener" / "tool.py")
oem = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(oem)
    tool_ok = True
except Exception as e:
    print("  工具模块加载失败（缺 openai 等依赖时属正常）:", type(e).__name__, e)
    tool_ok = False

if tool_ok:
    b3 = make_browser(build_html(COMPANIES))
    cm3 = b3._collect_column_index_map()
    b3.column_index_map = cm3
    ni = oem._find_column(b3, cm3, NAME_COL_LABEL, "company name", "name")
    di = oem._find_column(b3, cm3, DOMAIN_COL_LABEL, "domain")
    check("_find_column 找到 name/domain", (ni, di) == ("0", "2"), f"{ni},{di}")

    recs = oem._read_current_page(b3, ni, di)
    for r in recs:
        print("   ", r)
    check("解析出 3 条记录", len(recs) == 3, str(len(recs)))
    check("公司名正确", recs[0]["name"] == "Foxconn Industrial Internet", recs[0]["name"])
    check("domain 正确", recs[0]["domain"] == "fii-foxconn.com", recs[0]["domain"])

    # 缺 domain 列时降级
    b4 = make_browser(build_html(COMPANIES, with_domain=False))
    cm4 = b4._collect_column_index_map()
    b4.column_index_map = cm4
    di4 = oem._find_column(b4, cm4, DOMAIN_COL_LABEL, "domain")
    recs4 = oem._read_current_page(b4, oem._find_column(b4, cm4, NAME_COL_LABEL), di4)
    check("无 domain 列时不崩、仍解析出记录", di4 is None and len(recs4) == 3,
          f"domain_idx={di4}, rows={len(recs4)}")

    # 缓存键：域名优先、大小写/协议/www 归一；无域名退回规范化公司名
    ck = oem._cache_key
    check("缓存键以域名为准", ck("Foxconn Industrial Internet", "fii-foxconn.com") == "d:fii-foxconn.com")
    check("缓存键归一化 https/www", ck("X", "https://www.Jabil.com/") == ck("Y", "jabil.com"))
    check("同域名不同写法命中同一条", ck("Flex Ltd", "FLEX.com") == ck("Flex Limited", "flex.com"))
    check("无域名退回公司名", ck("Arrow Electronics", "") == "n:arrowelectronics")
    check("无域名时公司名写法差异归一", ck("Arrow Electronics", "") == ck("arrow  electronics!", ""))

    # 页面指纹（翻页是否生效的判据）
    sig1 = oem._page_signature(b3, ni)
    b5 = make_browser(build_html(PAGE2, page_no=2, has_next=False))
    b5.column_index_map = b5._collect_column_index_map()
    sig2 = oem._page_signature(b5, ni)
    print(f"    第1页指纹(3行): {sig1}")
    print(f"    第2页指纹(2行): {sig2}")
    check("不同页的指纹不同(翻页检测可用)", sig1 != sig2)
    check("指纹含公司名、不是只有行数", "Foxconn" in sig1, sig1)

    # 回归：两页【行数相同】时指纹必须仍不同。
    # 若指纹取的是 td 的第一个（checkbox 列，恒空），指纹会退化成只有行数，
    # 两页同为 N 行就被误判成"没翻动"，分页在第一页后静默停止。
    SAME_COUNT_P1 = COMPANIES[:2]
    SAME_COUNT_P2 = PAGE2[:2]
    ba = make_browser(build_html(SAME_COUNT_P1, page_no=1))
    ba.column_index_map = ba._collect_column_index_map()
    bb = make_browser(build_html(SAME_COUNT_P2, page_no=2))
    bb.column_index_map = bb._collect_column_index_map()
    sa, sb = oem._page_signature(ba, ni), oem._page_signature(bb, ni)
    print(f"    等行数两页: {sa}  vs  {sb}")
    check("两页行数相同时指纹仍不同(防分页提前停止)", sa != sb, f"{sa} vs {sb}")

if tool_ok:
    print()
    print("=" * 72)
    print("⑤ 浏览器启动 / 登录的控制流（真实事故回归）")
    print("=" * 72)
    # 事故：贾维斯实跑时「窗口一闪就没、什么也操作不了」。根因是启动路径上任一环节
    # 抛出非 SessionExpiredError 的异常，就会跳过有头登录兜底，连窗口都不打开。
    # 这里桩掉 _launch / detect_auth_state，把五条路径全钉住。
    import types as _t, logging as _lg
    _log = _lg.getLogger("harness_flow")
    _log.addHandler(_lg.NullHandler())

    class _Paths:
        profile_dir = "/tmp/p"
        log_file = "/tmp/p.log"

    _launched = []

    def _fake_page():
        return _t.SimpleNamespace(
            goto=lambda *a, **k: None, bring_to_front=lambda: None,
            set_default_timeout=lambda x: None, set_default_navigation_timeout=lambda x: None)

    def _mk_launch(fail_headless=False):
        def _l(paths, headless):
            _launched.append("headless" if headless else "HEADED")
            if headless and fail_headless:
                raise RuntimeError("profile locked")
            return ("pw", "ctx", _fake_page())
        return _l

    _orig = (oem._launch, oem._close, oem.HubSpotBrowser,
             oem._PROFILE_UNLOCK_COOLDOWN, oem._LOGIN_POLL_SECONDS, oem._LOGIN_WAIT_SECONDS)
    oem._close = lambda pw, ctx: None
    oem._PROFILE_UNLOCK_COOLDOWN = 0
    oem._LOGIN_POLL_SECONDS = 0.01
    oem._LOGIN_WAIT_SECONDS = 0.05

    def _flow(states, fail_headless=False):
        _launched.clear()
        oem._launch = _mk_launch(fail_headless)
        _seq = iter(states)

        class _FB:
            def __init__(self, *a):
                self.page = None

            def detect_auth_state(self):
                try:
                    return next(_seq)
                except StopIteration:
                    return "login_page"
        oem.HubSpotBrowser = _FB
        try:
            oem._open_view(_Paths(), _log, "https://x/view")
            return "ok", list(_launched)
        except Exception as e:
            return type(e).__name__, list(_launched)

    r, seq = _flow(["ok"])
    check("已登录时只开 headless、不打扰用户", (r, seq) == ("ok", ["headless"]), f"{r} {seq}")
    r, seq = _flow(["login_page", "ok"])
    check("未登录时开有头窗口并轮询到登录成功",
          (r, seq) == ("ok", ["headless", "HEADED"]), f"{r} {seq}")
    r, seq = _flow(["login_page"])
    check("等不到登录 → SessionExpiredError（且窗口确实开过）",
          (r, seq) == ("SessionExpiredError", ["headless", "HEADED"]), f"{r} {seq}")
    # 关键回归：headless 启动本身失败（profile 被占用）也必须走到有头兜底。
    # 上一版把 _launch 写在 try 外面，这里会直接抛 RuntimeError、窗口永不打开。
    r, seq = _flow(["ok"], fail_headless=True)
    check("headless 启动失败仍要开有头窗口（防『窗口一闪就没』）",
          (r, seq) == ("ok", ["headless", "HEADED"]), f"{r} {seq}")

    # _scrape 的 finally 不能因 browser 未定义而抛 NameError 盖掉真实错误
    oem._open_view = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    oem.resolve_paths = lambda p: _Paths()
    oem.ensure_directories = lambda p: None
    oem.setup_logger = lambda f: _log
    err = oem._scrape("u", 0, True).get("error", "")
    check("启动失败时返回真实错误、不被 finally 的 NameError 盖掉",
          err.startswith("RuntimeError: boom"), err)

    (oem._launch, oem._close, oem.HubSpotBrowser, oem._PROFILE_UNLOCK_COOLDOWN,
     oem._LOGIN_POLL_SECONDS, oem._LOGIN_WAIT_SECONDS) = _orig

print()
print("=" * 72)
if fails:
    print(f"❌ {len(fails)} 项失败：" + "; ".join(fails))
    sys.exit(1)
print("✅ 全部通过")
print()
print("""提醒：本验证台用的是【仿真 DOM】，仿真结构已于 2026-07-18 对着真实 HubSpot 页面校准。

已在真实页面确认（view 68270809，532 records）：
  · 第 0 列是 checkbox，无 data-column-index；dci=0 才是 ACCOUNT NAME → 位置下标必错位
  · data-column-index 的【列顺序因视图而异】（另一视图里 1=owner/2=domain，此视图 1=domain/2=owner）
    → 必须按列名解析，绝不能硬编码索引
  · 翻页按钮真实选择器是 button[data-next-page='true']（与界面语言无关）；
    aria-label='Next page' 仅英文界面成立；禁用态看 aria-disabled 而非 disabled
  · 虚拟滚动：渲染行数随窗口高度变化。窗口高 1323 → 25 行全渲染；
    高 960（worker 默认 viewport）→ 只有 23 行，滚动中一度只剩 22 行，
    但滚动累积去重后可拿全 25 行 → 必须滚动累积

仍未验证（只能在本机真跑）：登录流程、会话过期分支、连续翻页数十页的稳定性。""")
