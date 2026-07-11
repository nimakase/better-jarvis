"""
报告共享渲染层。

  - render_html_to_pdf(html, out_path)：惰性引入 playwright，把 HTML 渲染成 A4 PDF。
  - custom_report_html(title, body_md, subtitle)：自定义报告的统一模板（标题页眉 + 正文 + 页脚）。
  - md_to_html(md)：极简 markdown 子集转换（标题/加粗/列表/段落），够模型撰写报告用，无第三方依赖。

自定义报告 = 模型撰写 markdown 内容 → 套这里的统一模板 → 渲染归档；与"市场情报日报"等
固定模板报告共用同一条 HTML→PDF 管道，风格统一。
"""
from __future__ import annotations

import html as _html
import re
from datetime import date
from pathlib import Path
from typing import Optional

CSS = """<style>
@page { size: A4; margin: 20mm 16mm 22mm 16mm; }
@page :first { margin-top: 16mm; }
body { font-family:"Noto Sans CJK SC","Microsoft YaHei","PingFang SC","WenQuanYi Micro Hei",sans-serif;
  color:#333; font-size:12pt; line-height:1.6; -webkit-print-color-adjust:exact; print-color-adjust:exact; }
.header { display:flex; justify-content:space-between; align-items:baseline;
  border-bottom:2.5px solid #1a3a5c; padding-bottom:8px; margin-bottom:18px; }
.header-left { display:flex; align-items:baseline; gap:16px; }
.header-title { font-size:20pt; font-weight:700; color:#1a3a5c; margin:0; letter-spacing:1px; }
.header-date { font-size:11pt; color:#666; }
.header-logo { font-size:12pt; font-weight:700; color:#1a3a5c; letter-spacing:2px; }
h1.doc { font-size:17pt; color:#1a3a5c; margin:18px 0 10px; }
h2.doc { font-size:14pt; font-weight:700; color:#1a3a5c; border-left:4px solid #1a3a5c;
  padding-left:10px; margin:20px 0 10px; }
h3.doc { font-size:12.5pt; font-weight:700; color:#234; margin:14px 0 6px; }
p.doc { margin:8px 0; }
ul.doc, ol.doc { margin:8px 0 8px 22px; }
ul.doc li, ol.doc li { margin:4px 0; }
.footer { margin-top:26px; padding-top:10px; border-top:1px solid #d0d6de; font-size:9pt; color:#888; line-height:1.5; }
</style>"""


def _esc(s) -> str:
    return _html.escape(str(s if s is not None else ""))


def _inline(text: str) -> str:
    """行内 markdown：**加粗** → <b>，转义其余。"""
    out, last = [], 0
    for m in re.finditer(r"\*\*(.+?)\*\*", text):
        out.append(_esc(text[last:m.start()]))
        out.append("<b>" + _esc(m.group(1)) + "</b>")
        last = m.end()
    out.append(_esc(text[last:]))
    return "".join(out)


def md_to_html(md: str) -> str:
    """极简 markdown 子集 → HTML（标题 # ## ###、- 列表、1. 列表、空行分段、**加粗**）。"""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    html_parts: list[str] = []
    para: list[str] = []
    list_type: Optional[str] = None  # "ul" / "ol"

    def flush_para():
        if para:
            html_parts.append(f'<p class="doc">{_inline(" ".join(para))}</p>')
            para.clear()

    def flush_list():
        nonlocal list_type
        if list_type:
            html_parts.append(f"</{list_type}>")
            list_type = None

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_para(); flush_list(); continue
        m_h = re.match(r"^(#{1,3})\s+(.*)$", line)
        m_ul = re.match(r"^[-*]\s+(.*)$", line)
        m_ol = re.match(r"^\d+[.)]\s+(.*)$", line)
        if m_h:
            flush_para(); flush_list()
            lvl = len(m_h.group(1))
            html_parts.append(f'<h{lvl} class="doc">{_inline(m_h.group(2))}</h{lvl}>')
        elif m_ul or m_ol:
            flush_para()
            want = "ul" if m_ul else "ol"
            if list_type != want:
                flush_list(); html_parts.append(f'<{want} class="doc">'); list_type = want
            html_parts.append(f"<li>{_inline((m_ul or m_ol).group(1))}</li>")
        else:
            flush_list(); para.append(line.strip())
    flush_para(); flush_list()
    return "\n".join(html_parts)


def custom_report_html(title: str, body_md: str, subtitle: Optional[str] = None,
                       as_of: Optional[str] = None) -> str:
    today = as_of or date.today().isoformat()
    body = md_to_html(body_md)
    sub = f'<h1 class="doc">{_esc(subtitle)}</h1>' if subtitle else ""
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<title>{_esc(title)}</title>{CSS}</head><body>'
        f'<div class="header"><div class="header-left">'
        f'<h1 class="header-title">{_esc(title)}</h1>'
        f'<span class="header-date">{today}</span></div>'
        f'<div class="header-logo">CCL</div></div>'
        f'{sub}{body}'
        '<div class="footer"><p>本报告由贾维斯生成，内容供参考，不构成投资或采购建议。</p></div>'
        '</body></html>'
    )


async def render_html_to_pdf(html_str: str, out_path: str | Path) -> str:
    """HTML 字符串 → A4 PDF（惰性引入 playwright）。"""
    from playwright.async_api import async_playwright

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html_str, wait_until="networkidle")
        await page.wait_for_timeout(400)
        await page.emulate_media(media="print")
        await page.pdf(path=str(out), format="A4", print_background=True,
                       prefer_css_page_size=True,
                       margin={"top": "16mm", "bottom": "22mm", "left": "16mm", "right": "16mm"})
        await browser.close()
    return str(out)
