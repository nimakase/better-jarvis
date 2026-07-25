"""
工作流触发工具 run_workflow —— 运行已注册的多步骤工作流（潜客名单等）。

触发纪律（见 controller 的工作流政策）：仅在用户【明确要求运行】或定时任务触发时调；
对有副作用的工作流（confirm=true，会打开浏览器/连 HubSpot），必须先一句话告知将做什么、
得到确认后再调，绝不擅自或意外触发。读取无需工具——工作流目录已注入 system prompt。

【产出文件自动发到对话】工作流出的文件（如潜客 xlsx）由本工具直接挂 file_download
带外动作——网页显示下载卡片、飞书发文件消息。不再依赖模型"记得"去调
send_file_to_chat：旧路径下模型连文件路径都拿不到（返回里只有步骤 summary），
文件从结构上就发不出去，这曾是"跑通了但文件没给到人"的直接原因。
"""
import asyncio
import logging
from functools import partial
from pathlib import Path

from core.registry import tool as _tool
from core.results import ToolResult, file_download
from core import workflow_registry as wr

tool = partial(_tool, group="workflows")
logger = logging.getLogger("jarvis.workflow_tools")

# 正在后台跑的 detach 工作流（防同一条重复并发派发）
_RUNNING: set[str] = set()


def _notify_done(wf: dict, run: dict, context: dict) -> None:
    """detach 工作流跑完后的收尾。

    投递纪律（2026-07-25 收敛，修「一次运行两条飞书」的重复投递）：
    **成功不再由这里发卡**。每条 detach 工作流的业务结果——信号采集的摘要+日报 PDF、
    潜客的名单 xlsx、未登录通知等——都由工作流自己的步骤经 delivery 发出（最贴近数据、
    内容最有用）。此前这里跑完又发一张 run 总结卡（"✓ collect ✓ ingest …"），于是
    用户每次收到两条：一条业务卡 + 一条总结卡。故本函数只保留两件事：
      ① 跑【失败】时兜底告警——此时业务步骤可能没来得及发，靠它兜底（severity=high）；
      ② 把产出文件登记进产物图书馆（无论成败）。
    契约：detach 工作流必须自行投递其成功结果；框架不再代发成功卡。全程 best-effort。
    """
    fp = extract_output_file(context)
    # ① 失败兜底告警（成功由工作流步骤自己发，这里不再重复投递）
    if run.get("status") == "failed":
        title = f"工作流『{wf.get('name', wf['id'])}』运行失败"
        content = run.get("summary", "") or "（无摘要）"
        try:
            from core import delivery
            res = delivery.deliver(wf["id"], title, content, severity="high",
                                   attachments=[fp] if fp else None)
            if not res.get("delivered"):
                logger.warning("detach 工作流『%s』失败告警未送达：%s",
                               wf["id"], res.get("sent") or res.get("reason"))
        except Exception as e:  # noqa: BLE001
            logger.warning("detach 工作流失败告警投递异常：%s", e)
    # ② 产出文件登记进产物图书馆（无论成败）。kind 按扩展名区分：PDF=报告、其余=清单。
    if fp:
        try:
            from core import artifacts
            kind = "报告" if Path(fp).suffix.lower() == ".pdf" else "清单"
            artifacts.register(fp, producer=f"workflow:{wf['id']}", kind=kind,
                               label=Path(fp).stem, state="kept")
        except Exception:
            pass


async def _run_detached(wf: dict) -> None:
    """后台跑一条 detach 工作流并交付。绝不向上抛（没有人在等它）。"""
    try:
        res = await wr.run(wf["id"])
        run = res.get("run") or {"status": "failed",
                                 "summary": res.get("error", "未知错误")}
        _notify_done(wf, run, res.get("context") or {})
    except Exception as e:  # noqa: BLE001
        logger.exception("detach 工作流异常：%s", wf["id"])
        _notify_done(wf, {"status": "failed", "summary": f"运行异常：{e}"}, {})
    finally:
        _RUNNING.discard(wf["id"])


def extract_output_file(context: dict) -> str | None:
    """从工作流 context 里找产出文件路径。

    约定：output 步返回 {"path": <文件路径>, ...}（见 prospecting.make_output_fn）。
    路径存在才算数——未出表时 path 为 None 或文件不在，返回 None。
    """
    out = (context or {}).get("output")
    if not isinstance(out, dict):
        return None
    p = out.get("path")
    if p and Path(p).exists():
        return str(p)
    return None


@tool(
    "run_workflow",
    "运行一个已注册的多步骤工作流（如 prospect_daily 今日潜客名单）。仅在用户明确要求运行时调用。"
    "对 confirm=true 的工作流（会打开有头 Chrome、连接 HubSpot），必须【先】用一句话告诉用户"
    "『我将运行X，会打开浏览器连 HubSpot』并取得确认，再调用本工具。可用工作流见 system prompt 的"
    "『可运行的工作流』。运行结束会返回各步骤状态；产出的文件会自动发到对话界面，"
    "无需再调 send_file_to_chat。",
    {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string", "description": "工作流 id，如 prospect_daily"},
        },
        "required": ["workflow_id"],
    },
)
async def run_workflow(workflow_id: str):
    # 长耗时工作流（dispatch="detach"）：派发即返回，主对话不被扣押。
    # 后台跑完经 delivery 推送交付（飞书可带附件文件）。
    wf = wr.get(workflow_id)
    if wf and wf.get("dispatch") == "detach":
        if workflow_id in _RUNNING:
            return (f"工作流『{wf.get('name', workflow_id)}』已经在后台跑着了，"
                    f"不重复派发。跑完会推送结果。")
        _RUNNING.add(workflow_id)
        task = asyncio.create_task(_run_detached(wf))
        task.add_done_callback(lambda t: t.exception())   # 吞掉未观察异常告警
        return (f"已派发：工作流『{wf.get('name', workflow_id)}』在后台运行"
                f"（{wf.get('needs') or '预计几分钟'}）。跑完我会把结果推送给你"
                f"（飞书/网页通知，产出文件一并发送），期间可以继续聊别的。")

    res = await wr.run(workflow_id)
    run = res.get("run")
    if not run:
        return f"运行失败：{res.get('error', '未知错误')}"

    text = (f"工作流『{run.get('name', workflow_id)}』运行结束（{run.get('status')}）。\n"
            f"{run.get('summary', '')}")

    fp = extract_output_file(res.get("context") or {})
    if fp:
        p = Path(fp)
        text += f"\n产出文件已直接发送到对话界面：{p.name}"
        return ToolResult(
            text=text,
            actions=[file_download(str(p), p.name, p.stat().st_size)],
        )
    return text


@tool(
    "workflow_status",
    "查看工作流运行状态：哪些正在后台跑（detach 派发的）、最近几次运行的结果。"
    "用户问「采集跑完了吗 / 潜客名单好了吗 / 工作流什么情况」时用。只读。",
    {"type": "object", "properties": {}},
)
async def workflow_status() -> str:
    lines = []
    if _RUNNING:
        lines.append("⏳ 正在后台运行：" + "、".join(sorted(_RUNNING)) +
                     "（跑完会自动推送结果）")
    else:
        lines.append("当前没有在跑的后台工作流。")
    recent = wr.recent_runs(limit=5)
    if recent:
        lines.append("最近运行：")
        for r in recent:
            lines.append(f"  [{r.get('status')}] {r.get('name', r.get('id'))} · "
                         f"{str(r.get('finished_at') or r.get('started_at') or '')[:16]} · "
                         f"{(r.get('summary') or '')[:60]}")
    return "\n".join(lines)
