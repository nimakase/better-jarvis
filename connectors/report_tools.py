"""
日报工具 + 报告类型注册。

  - 把「市场情报日报」注册为报告中心(core.reports)的一个类型，生成器=intel.report
  - 给 agent 暴露 generate_market_report：生成并登记进报告中心

用户说「今天的报告 / 市场日报 / 生成日报」时，agent 调这一个工具即可。
"""
from datetime import date
from functools import partial

from core.registry import tool as _tool
from core import reports as _reports

tool = partial(_tool, group="report")


# ── 注册「市场情报日报」报告类型（导入即注册，随启动生效）──
async def _gen_market_intel(days: int = 14, **kwargs) -> dict:
    from intel import report
    path = await report.generate_report_pdf(days=days)
    return {"path": path, "title": f"市场情报日报 {date.today().isoformat()}"}


_reports.register_report_type("market_intel", "市场情报日报", _gen_market_intel)


@tool(
    "generate_market_report",
    "生成今天的电子元件市场情报日报（基于信号库真数据，含价格供需/行业事件/余料机会赛道），"
    "并登记进报告中心。用户要『今天的报告 / 市场情报日报 / 生成日报』时调这一个工具。"
    "生成后请调用 send_file_to_chat 把 PDF 发给用户。",
    {
        "type": "object",
        "properties": {"days": {"type": "integer", "description": "回看天数，默认 14"}},
        "required": [],
    },
)
async def generate_market_report(days: int = 14) -> str:
    res = await _reports.generate("market_intel", days=days)
    if not res.get("ok"):
        return f"生成日报失败：{res.get('error')}（可能是 playwright 未装或信号库为空）"
    rec = res["report"]
    return (f"市场情报日报已生成并归档到报告中心：{rec['path']}\n"
            f"请调用 send_file_to_chat 把该 PDF 发给用户。")
