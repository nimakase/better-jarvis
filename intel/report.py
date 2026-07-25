"""
第一方市场情报日报 —— 用信号库真数据渲染 PDF。

取代 skills/market_intel_report（沙箱技能读不到 core/intel，且数据是写死的假数据）。
版式沿用原技能的 PDF 模板；数据来自 signal_library.query_report() + detect_hot_sectors()。

build_report_html() 纯函数、可测；generate_report_pdf() 惰性引入 playwright 渲染。
"""
from __future__ import annotations

import html as _html
import json
from datetime import date
from pathlib import Path
from typing import Optional

from intel import signal_library as sl

try:
    import config
    _OUT = config.DATA_DIR / "reports"
except Exception:
    _OUT = Path(__file__).resolve().parent / "reports"

TLABEL = {
    "pricing": "涨价", "lead_time": "交期", "shortage": "缺货", "oversupply": "过剩",
    "demand_shift": "需求下滑", "capacity": "产能", "layoff": "裁员", "eol_pcn": "EOL",
    "closure": "关厂", "m_and_a": "并购", "write_down": "减值", "policy": "政策",
}
PRICE_TYPES = {"pricing", "lead_time", "shortage", "oversupply"}
EVENT_TYPES = {"closure", "m_and_a", "layoff", "capacity", "demand_shift", "eol_pcn", "write_down", "policy"}

CSS = """<style>
@page { size: A4; margin: 20mm 16mm 22mm 16mm; }
@page :first { margin-top: 16mm; }
body { font-family: "Noto Sans CJK SC","Microsoft YaHei","PingFang SC","WenQuanYi Micro Hei",sans-serif;
  color:#333; font-size:12pt; line-height:1.5; -webkit-print-color-adjust:exact; print-color-adjust:exact; }
.header { display:flex; justify-content:space-between; align-items:baseline; border-bottom:2.5px solid #1a3a5c; padding-bottom:8px; margin-bottom:16px; }
.header-left { display:flex; align-items:baseline; gap:16px; }
.header-title { font-size:20pt; font-weight:700; color:#1a3a5c; margin:0; letter-spacing:1px; }
.header-date { font-size:11pt; color:#666; }
.header-logo { font-size:12pt; font-weight:700; color:#1a3a5c; letter-spacing:2px; }
.section-title { font-size:14pt; font-weight:700; color:#1a3a5c; border-left:4px solid #1a3a5c; padding-left:10px; margin-top:22px; margin-bottom:12px; }
.card-grid { display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin-bottom:8px; }
.card { background:#f8f9fb; border:1px solid #e8ecf0; border-radius:6px; padding:10px 12px; }
.card-label { font-size:10pt; color:#666; margin-bottom:6px; }
.card-value { font-size:16pt; font-weight:700; color:#1a3a5c; }
table { width:100%; border-collapse:collapse; margin-bottom:8px; font-size:10.5pt; }
th { background:#eef2f7; color:#1a3a5c; font-weight:600; text-align:left; padding:8px; border-bottom:1.5px solid #d0d6de; }
td { padding:7px 8px; border-bottom:1px solid #eef0f2; }
tr:last-child td { border-bottom:none; }
.badge { display:inline-block; padding:2px 8px; border-radius:3px; font-size:10pt; font-weight:600; }
.badge-red { background:#fde8e8; color:#c53030; }
.badge-yellow { background:#fef7e0; color:#9a7a1a; }
.heat-dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; }
.heat-high { background:#e53e3e; } .heat-medium { background:#dd6b20; } .heat-low { background:#38a169; }
.event-list { padding-left:20px; margin:0; }
.event-list li { margin-bottom:5px; font-size:10.5pt; line-height:1.6; }
.footer { margin-top:24px; padding-top:10px; border-top:1px solid #d0d6de; font-size:9pt; color:#888; line-height:1.4; }
</style>"""


def _esc(s) -> str:
    return _html.escape(str(s if s is not None else ""))


def _components(it: dict) -> str:
    raw = it.get("raw_json")
    if not raw:
        return ""
    try:
        return "、".join(json.loads(raw).get("components") or [])
    except Exception:
        return ""


def _dir_badge(d) -> str:
    if d == "up":
        return '<span class="badge badge-red">涨/紧</span>'
    if d == "down":
        return '<span class="badge badge-yellow">跌/松</span>'
    return ""


def build_report_html(db_path: str | Path = sl.DEFAULT_DB, days: int = 14,
                      as_of: Optional[str] = None) -> str:
    grouped = sl.query_report(db_path=db_path, days=days, as_of=as_of)
    flat = [(t, it) for t, items in grouped.items() for it in items]
    price = [(t, it) for t, it in flat if t in PRICE_TYPES]
    event = sorted([(t, it) for t, it in flat if t in EVENT_TYPES],
                   key=lambda x: -(x[1].get("severity") or 0))
    hot = sl.detect_hot_sectors(db_path=db_path, threshold=3.0, as_of=as_of)[:8]
    # 点名公司走同一个 days 窗口（这个查询没有强度衰减，窗口必须由调用方给）
    pointed = sl.company_pointed_signals(db_path=db_path, as_of=as_of, days=days)
    today = as_of or date.today().isoformat()

    cards = [("活跃信号", len(flat)), ("点名公司", len(pointed)),
             ("价格供需动态", len(price)), ("行业事件", len(event)), ("热点赛道", len(hot))]
    cards_html = "".join(
        f'<div class="card"><div class="card-label">{l}</div><div class="card-value">{v}</div></div>'
        for l, v in cards)

    price_section = ""
    if price:
        rows = "".join(
            f'<tr><td>{TLABEL.get(t, t)}</td><td>{_dir_badge(it.get("direction"))}</td>'
            f'<td>{_esc(_components(it))}</td><td>{_esc(it["summary"])}</td></tr>'
            for t, it in price)
        price_section = (
            '<div class="section-title">价格与供需动态</div>'
            '<table><thead><tr><th style="width:12%">类型</th><th style="width:14%">方向</th>'
            '<th style="width:24%">元件</th><th>摘要</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')

    events_html = "".join(f'<li><b>{TLABEL.get(t, t)}</b> · {_esc(it["summary"])}</li>' for t, it in event)
    event_section = ('<div class="section-title">行业事件（余料驱动）</div>'
                     f'<ul class="event-list">{events_html or "<li>—</li>"}</ul>')

    hot_section = ""
    if hot:
        rows = "".join(
            f'<tr><td>{_esc(h["sector_id"])}</td><td>{round(h["total_strength"], 1)}</td>'
            f'<td><span class="heat-dot {("heat-high" if i == 0 else "heat-medium" if i < 3 else "heat-low")}"></span>'
            f'{("高" if i == 0 else "中" if i < 3 else "低")}</td></tr>'
            for i, h in enumerate(hot))
        hot_section = (
            '<div class="section-title">余料机会赛道</div>'
            '<table><thead><tr><th style="width:42%">赛道</th><th style="width:22%">聚合强度</th>'
            '<th>热度</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')

    # ── 点名公司（金线索）──────────────────────────────────────────────
    # 这块过去挂在潜客名单后面当「机会轨」，但它是【日报的副产物】不是名单的一部分：
    # 信号本来就采到了具体公司，日报却一直没展示，反倒是潜客表把它们捡去拼在后面。
    # 摆回这里还顺带治好了重复刷屏——日报是快照、天然带 days 窗口，窗口内重复出现
    # 是正确行为；而名单是队列，同一家天天重出就是缺陷。详见 generation.assemble 注释。
    # 注意：这里【不做 HubSpot 匹配】，日报只回答"发生了什么"，不回答"是否已被认领"。
    pointed_section = ""
    if pointed:
        rows = "".join(
            f'<tr><td><b>{_esc(p["company_name"])}</b></td>'
            f'<td>{_esc(p.get("country") or "—")}</td>'
            f'<td><span class="badge badge-red">{TLABEL.get(p["signal_type"], p["signal_type"])}</span></td>'
            f'<td>{_esc(p.get("note") or p.get("summary") or "")}</td></tr>'
            for p in pointed)
        pointed_section = (
            '<div class="section-title">点名公司（金线索）</div>'
            '<table><thead><tr><th style="width:24%">公司</th><th style="width:12%">国家</th>'
            '<th style="width:12%">信号</th><th>为什么可能有余料</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')

    body = (
        f'<div class="header"><div class="header-left"><h1 class="header-title">电子元件市场情报日报</h1>'
        f'<span class="header-date">{today}</span></div><div class="header-logo">CCL</div></div>'
        f'<div class="section-title">概览</div><div class="card-grid">{cards_html}</div>'
        f'{pointed_section}{price_section}{event_section}{hot_section}'
        '<div class="footer"><p>本报告由 CCL 情报台基于信号库自动生成，数据来源于公开市场信息，仅供参考，不构成投资或采购建议。</p></div>')

    return ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<title>CCL 市场情报日报</title>' + CSS + '</head><body>' + body + '</body></html>')


async def generate_report_pdf(db_path: str | Path | None = None, days: int = 14,
                              out_dir: str | Path | None = None,
                              as_of: Optional[str] = None) -> str:
    from playwright.async_api import async_playwright
    html_str = build_report_html(db_path or sl.DEFAULT_DB, days, as_of)
    out = Path(out_dir or _OUT)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"market_intel_{as_of or date.today().isoformat()}.pdf"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html_str, wait_until="networkidle")
        await page.wait_for_timeout(600)
        await page.emulate_media(media="print")
        await page.pdf(path=str(path), format="A4", print_background=True,
                       prefer_css_page_size=True,
                       margin={"top": "16mm", "bottom": "22mm", "left": "16mm", "right": "16mm"})
        await browser.close()
    return str(path)


async def run_and_deliver(db_path=None, days: int = 14, track: str = "report",
                          as_of: Optional[str] = None) -> dict:
    """报告轨入口：渲染 PDF → 过投递闸门推送。"""
    from core import delivery
    path = await generate_report_pdf(db_path, days, as_of=as_of)
    res = delivery.deliver(track, "市场情报日报已就绪", f"已生成日报 PDF：{path}", severity="normal", as_of=as_of)
    return {"path": path, "delivered": res.get("delivered"), "reason": res.get("reason")}
