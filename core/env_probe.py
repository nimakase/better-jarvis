"""环境探针 —— 造工具前先看一眼目标网页的【真实结构】。

## 为什么需要它

造工具框架一~四期解决的都是「代码和代码库对不对得上」。但抓取类工具最大的失败源
是【代码和目标网页对不对得上】——而模型从没见过那个页面。

实测教训（2026-07-18，oem_ems_screener 复盘）：为 HubSpot 写抓取工具时，翻页按钮
选择器是靠源码常量倒推猜的。人类协作者同样凭经验猜了 5 个候选，真实页面上只中 1 个，
排第一的那个根本不存在；真正稳的写法 `button[data-next-page='true']` 谁都没猜到。
差别不在谁猜得准——**在于能不能去看一眼**。

本模块把「看一眼」变成一次可复用的调用：导航到 URL，抽出一份【结构摘要】
（不是整页 HTML，那太费 token），注入生成提示词。

## 安全边界（重要）

- **只读**：只导航 + 读 DOM。不点击、不填表、不提交、不下载、不执行页面里的指令。
- **URL 只能来自用户的需求描述**，不接受从页面内容里发现的 URL（防注入）。
- 只允许 http/https；带凭据的 URL（user:pass@）一律拒绝。
- 复用 app 的浏览器 profile，因此**能看到已登录页面**——这也意味着摘要里可能含少量
  真实数据。摘要只取结构与列名，并对疑似长文本做截断。
- 全程有超时；失败一律降级为空串，绝不阻断造工具。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("jarvis.env_probe")

PROBE_TIMEOUT_MS = 25000
# 实测一张 HubSpot 列表页的摘要约 5000 字符（≈1700 token）。这是造工具时的一次性
# 成本，换来的是不用猜选择器，划算。留出余量并把最有判别力的内容排在前面，
# 万一超限被截断，先丢掉的是价值最低的 data-test-id 清单。
MAX_DIGEST_CHARS = 6000
_MAX_TEST_IDS = 40
_MAX_HEADERS = 30

_URL_RE = re.compile(r"https?://[^\s一-鿿'\"<>()（）「」【】]+")


def extract_urls(text: str, limit: int = 3) -> list:
    """从需求描述里提取候选 URL。只认 http/https，拒绝带凭据的。"""
    out, seen = [], set()
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:！。，；、)）]】")
        try:
            p = urlparse(url)
        except Exception:
            continue
        if p.scheme not in ("http", "https") or not p.netloc:
            continue
        if "@" in p.netloc:          # 形如 user:pass@host —— 不碰
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out


# 在页面里执行的结构抽取脚本。刻意只取【结构】：标签、属性名、data-test-id、表头文本、
# 一行的骨架（文本用长度占位替代），足够写对选择器，又不会把整张表拖进提示词。
_DIGEST_JS = r"""
() => {
  const cap = (a, n) => a.slice(0, n);
  // 属性降噪：实测原始输出里一半是每行都不同的实例 id 和框架内部状态，
  // 挤掉真正有判别力的定位属性。只保留能用来写选择器 / 判断状态的那些。
  const NOISE = /^(id|class|style|role|tabindex|aria-invalid|aria-owns|aria-labelledby|aria-hidden|aria-pressed|aria-haspopup|data-table-external-id|data-fdt-|data-observer-|data-editable-cell|data-pressed|data-invalid|data-dropdown|data-fnd-button|data-button-use|data-tooltip-loaded|data-avatar-loaded|data-source|data-size|data-onboarding|data-toggle-input-wrapper|data-test-width|scope|type|value|hidden|rel|target)/;
  // 实例 id（cell-0-2-name-6602561637）每行都不同，泛化成 * 便于识别模式
  const gen = v => v.replace(/\d{6,}/g, '*');
  const attrsOf = el => Array.from(el.attributes)
      .filter(a => !NOISE.test(a.name))
      .map(a => a.value.length > 40 ? a.name : `${a.name}="${gen(a.value)}"`);

  // 1. data-test-id 清单（SPA 的稳定锚点）。只取【主内容区】的：全局导航栏那一堆
  //    （hs-global-toolbar / *-toggle / nav-*）对写抓取代码毫无用处，纯属噪音。
  const NAV_ID = /^(hs-|@@nav|nav-|sidebar|VNC|.*-toggle$|home-link|icon-|global-|scrollable-pane)/;
  const ids = new Set();
  const scope = document.querySelector('main, [role=main]') || document;
  scope.querySelectorAll('[data-test-id]').forEach(e => {
    const v = gen(e.getAttribute('data-test-id'));
    if (!NAV_ID.test(v)) ids.add(v);
  });

  // 2. 表格：表头（含 data-column-index 之类的定位属性）
  const table = document.querySelector('table');
  let headers = [], rowSkeleton = null, rowCount = 0, tableAttrs = [];
  if (table) {
    tableAttrs = attrsOf(table);
    headers = cap(Array.from(table.querySelectorAll('thead th')), 30).map((th, i) => ({
      pos: i, attrs: attrsOf(th), label: (th.innerText || '').trim().slice(0, 40)
    }));
    const rows = table.querySelectorAll('tbody tr');
    rowCount = rows.length;
    if (rows[0]) {
      // 一行的骨架：单元格属性 + 内部结构（文本替换为 «len»，不带出真实内容）
      rowSkeleton = {
        rowAttrs: attrsOf(rows[0]),
        cells: cap(Array.from(rows[0].querySelectorAll('td')), 12).map((td, i) => ({
          pos: i, attrs: attrsOf(td),
          inner: cap(Array.from(td.querySelectorAll('[data-test-id],a,span,input')), 4)
                   .map(e => `${e.tagName.toLowerCase()}[${attrsOf(e).join(' ')}]`)
        }))
      };
    }
  }

  // 3. 疑似分页 / 翻页控件
  const pager = cap(Array.from(document.querySelectorAll('button,a')).filter(b => {
    const t = (b.innerText || '').trim().toLowerCase();
    const al = (b.getAttribute('aria-label') || '').toLowerCase();
    const dt = (b.getAttribute('data-test-id') || '').toLowerCase();
    return ['next','prev','previous'].includes(t) || al.includes('next') || al.includes('page')
        || dt.includes('next') || dt.includes('pag');
  }), 8).map(b => `${b.tagName.toLowerCase()}[${attrsOf(b).join(' ')}] text="${(b.innerText||'').trim().slice(0,16)}"`);

  // 4. 页面自报的总数（"532 records" 这类）——工具做覆盖率自检的外部真值
  let totals = [];
  document.querySelectorAll('*').forEach(e => {
    if (e.childElementCount) return;
    const t = (e.innerText || '').trim();
    if (/^[\d,]+\s+(records|results|items)$/i.test(t)) totals.push(t);
  });

  return {
    url: location.href.slice(0, 200), title: document.title.slice(0, 120),
    looksLikeLogin: /login|signin|sign-in/i.test(location.href)
                    || !!document.querySelector("input[type='password']"),
    testIds: cap(Array.from(ids), 40),
    tableAttrs, headers, rowCount, rowSkeleton,
    pager, totals: cap(totals, 3),
    viewport: [window.innerWidth, window.innerHeight]
  };
}
"""


def _render(d: dict) -> str:
    """把结构 JSON 渲染成给模型读的紧凑文本。

    【顺序即优先级】：最有判别力的排前面（翻页控件 > 表头 > 行骨架 > 其余），
    因为超出 MAX_DIGEST_CHARS 时是从尾部截断的——先丢掉的必须是价值最低的部分。
    """
    L = [f"URL: {d.get('url','')}", f"标题: {d.get('title','')}"]
    if d.get("looksLikeLogin"):
        L.append("⚠ 这看起来是【登录页】——说明探针没有有效会话。下面的结构可能不是目标页面，"
                 "写代码时务必处理未登录分支（不要用 input() 等回车，见运行环境契约）。")
    L.append(f"浏览器视口: {d.get('viewport')}")
    if d.get("totals"):
        L.append(f"页面自报总数: {', '.join(d['totals'])} ← 可用作覆盖率自检的外部真值："
                 f"跑完拿实际抓取条数与它比对，对不上要如实报警")

    # ① 翻页 / 分页控件 —— 最容易猜错、猜错代价最大（静默只抓第一页）
    if d.get("pager"):
        L.append("\n【翻页 / 分页控件】照抄这里的真实属性写选择器，绝不要凭经验猜"
                 "（形如 next-page-button 这种想当然的名字往往根本不存在）：")
        for p in d["pager"]:
            L.append(f"  {p}")
        L.append("  提示：优先选与界面语言无关的属性（如 data-next-page），"
                 "aria-label 只在英文界面成立；禁用态通常看 aria-disabled 而非 disabled。")

    # ② 表头 —— 列定位的依据
    if d.get("headers"):
        L.append(f"\n【表头】共 {len(d['headers'])} 列，pos 是 DOM 位置：")
        for h in d["headers"][:_MAX_HEADERS]:
            L.append(f"  pos={h['pos']} {' '.join(h['attrs'])} → {h['label']!r}")
        L.append("  ⚠ pos（DOM 位置）与 data-column-index 之类的属性值【不是一回事】——"
                 "上面第 0 列往往是没有该属性的复选框列。定位单元格一律用属性选择器，"
                 "不要用位置下标。列顺序还会因视图而异，所以要【按列名解析】，不要硬编码索引。")

    # ③ 一行的结构骨架 —— 取文本时该往哪层钻
    if d.get("rowCount") is not None:
        L.append(f"\n【行结构】tbody 中当前已渲染 {d['rowCount']} 行"
                 "（若小于每页条数，说明是虚拟滚动：行会随滚动被回收，"
                 "必须滚动累积 + 按行 id 去重才拿得全一页）")
    rs = d.get("rowSkeleton")
    if rs:
        L.append(f"  tr[{' '.join(rs['rowAttrs'])}]")
        for c in rs["cells"]:
            inner = ("  内部: " + " ".join(c["inner"])) if c["inner"] else ""
            L.append(f"  td pos={c['pos']} [{' '.join(c['attrs'])}]{inner}")

    if d.get("tableAttrs"):
        L.append(f"\n表格元素: table[{' '.join(d['tableAttrs'])}]")
    if d.get("testIds"):
        L.append(f"\n主内容区的 data-test-id（前 {_MAX_TEST_IDS} 个，稳定锚点；"
                 f"数字实例 id 已泛化为 *）:")
        L.append("  " + ", ".join(d["testIds"][:_MAX_TEST_IDS]))
    return "\n".join(L)


def probe_page(url: str, profile_dir: Path = None, timeout_ms: int = PROBE_TIMEOUT_MS) -> str:
    """导航到 url，返回一份结构摘要文本。只读，不点击不填表。失败返回空串。

    profile_dir 给定则复用该浏览器 profile（能看到已登录页面）；否则用临时上下文。
    """
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.netloc or "@" in p.netloc:
            return ""
    except Exception:
        return ""

    pw = ctx = None
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        if profile_dir:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir), channel="chrome", headless=True,
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1600, "height": 1600})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        else:
            browser = pw.chromium.launch(headless=True)
            ctx = browser
            page = ctx.new_page()
        page.set_default_timeout(timeout_ms)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(3500)          # 等 SPA 渲染
        data = page.evaluate(_DIGEST_JS)
        text = _render(data)
        return text[:MAX_DIGEST_CHARS]
    except Exception as e:
        logger.warning("环境探针失败（降级为不注入）：%s: %s", type(e).__name__, e)
        return ""
    finally:
        for obj, fn in ((ctx, "close"), (pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, fn)()
            except Exception:
                pass
