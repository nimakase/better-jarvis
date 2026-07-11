"""
报告类型注册 + 通用报告生成工具。

报告 = 经结构化通道产出、归档、可在线查看的工件；不是"更长的回答"。
报告类型自描述（描述/何时用/参数）并注入 system prompt，模型据此选型；
统一经一个通用工具 generate_report(report_type, params) 生成，避免零散专用工具。

触发纪律见 controller 的报告政策：默认对话，仅在用户明确索取报告/PDF/导出时才生成。
"""
import json
from datetime import date
from functools import partial

from core.registry import tool as _tool
from core import reports as _reports

tool = partial(_tool, group="reports")


# ── 类型 1：市场情报日报（固定模板，信号库真数据；主要靠定时自动出）──
async def _gen_market_intel(days: int = 14, **kwargs) -> dict:
    from intel import report
    from intel import signal_library as sl
    days = int(days)
    # 信号库空时不出空壳 PDF：明确提示先采集，避免"只有模板没数据"的困惑
    grouped = sl.query_report(days=days)
    if not any(grouped.values()):
        raise RuntimeError(
            f"信号库近 {days} 天没有信号，无法生成有内容的日报。"
            "请先在情报台点「采集今日信号」（或对贾维斯说「采集今日信号」）入库后再生成。"
        )
    path = await report.generate_report_pdf(days=days)
    return {"path": path, "title": f"电子元件市场情报日报 {date.today().isoformat()}"}


_reports.register_report_type(
    "market_intel", "电子元件市场情报日报", _gen_market_intel,
    description="基于信号库的电子元件市场日报：价格供需/行业事件/余料热点赛道",
    when_to_use="用户明确要『今天的日报 / 市场情报日报 / 出一份市场报告』时",
    params={"days": "回看天数，默认 14"},
)


# ── 类型 2：自定义报告（模型撰写内容 → 统一模板 → 归档；兜住一切临时报告需求）──
async def _gen_custom(title: str = "", content: str = "", subtitle: str = "", **kwargs) -> dict:
    from core import report_render as rr
    from pathlib import Path
    import re as _re
    try:
        import config
        out_dir = config.DATA_DIR / "reports"
    except Exception:
        out_dir = Path(__file__).resolve().parent.parent / "reports"
    title = title.strip() or f"自定义报告 {date.today().isoformat()}"
    html_str = rr.custom_report_html(title, content, subtitle or None)
    safe = _re.sub(r"[^\w\-]+", "_", title)[:40] or "custom"
    path = await rr.render_html_to_pdf(html_str, out_dir / f"custom_{safe}_{date.today().isoformat()}.pdf")
    return {"path": path, "title": title}


_reports.register_report_type(
    "custom", "自定义报告", _gen_custom,
    description="任意主题的报告：你先把要点/分析撰写成 markdown，套统一模板渲染归档",
    when_to_use="用户明确要『出一份关于X的报告』而 X 不属于上面的固定类型时（含某赛道/公司的深度报告）",
    params={"title": "报告标题", "content": "报告正文（markdown）", "subtitle": "副标题，可选"},
    quick=False,  # 需模型撰写 title/content 才有内容；不在报告中心出一键按钮（只走对话生成）
)


@tool(
    "generate_report",
    "生成一份【归档报告工件】（PDF）。仅在用户明确索取报告/日报/PDF/导出/存档时才调用——"
    "普通提问一律用对话回答，不要生成报告，也不要主动提议生成。可用类型见 system prompt 的"
    "『可生成的报告类型』。自定义报告(custom)需在 params 里传 title 与 content（你撰写的 markdown 正文）。"
    "生成后调用 send_file_to_chat 把 PDF 发给用户。",
    {
        "type": "object",
        "properties": {
            "report_type": {"type": "string", "description": "报告类型 id，如 market_intel / custom"},
            "params_json": {"type": "string", "description": "该类型参数的 JSON 字符串，可留空"},
        },
        "required": ["report_type"],
    },
)
async def generate_report(report_type: str, params_json: str = "") -> str:
    try:
        params = json.loads(params_json) if params_json.strip() else {}
    except Exception as e:
        return f"params_json 不是合法 JSON：{e}"
    if not isinstance(params, dict):
        return "params_json 必须是 JSON 对象。"
    res = await _reports.generate(report_type, **params)
    if not res.get("ok"):
        return f"生成报告失败：{res.get('error')}"
    rec = res["report"]
    return (f"报告已生成并归档（类型 {rec['type_name']}）：{rec['path']}\n"
            f"请调用 send_file_to_chat 把该 PDF 发给用户；可在报告中心在线查看。")
