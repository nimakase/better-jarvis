"""
connectors/spawn_tools.py — 子 agent 的对话入口（core/spawn）

主线程（对话中的贾维斯）用这几个工具把重活派给隔离的子 agent，自己保持轻快。

2026-08-07 改为 detach + 推送交付（此前的问题）：此前 spawn_subtask/spawn_fanout
在工具调用里直接 `await`，最长可占用主对话轮次达 DEFAULT_TIMEOUT_S（300 秒）——
跟 run_workflow 对 dispatch="detach" 工作流的处理方式不一致（那边派发即返回、
后台跑完推送）。同一类"耗时不确定，不该占用主线"的活，两个功能演化出了两种
答案，现在收敛成一份：调用即刻返回，任务在后台跑，跑完经 core.delivery 推送
结果（飞书/网页通知），期间可以继续聊别的。查状态用 spawn_status。

安全设计不变：这里【故意不暴露】extra_tools 参数——对话中的模型只能派出
默认只读权限的子 agent；给子 agent 升权（如允许写日历）只能由人在代码/
配置层显式做。模型不能给自己的分身加权。
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from functools import partial

from core import effects
from core.registry import tool as _tool
from core.spawn import spawn, spawn_many

tool = partial(_tool, group="self")
logger = logging.getLogger("jarvis.spawn_tools")

# 正在后台跑的子任务：task_id -> {label, kind, started_at}（供 spawn_status 查）
_RUNNING: dict[str, dict] = {}


def _new_task_id(label: str) -> str:
    safe = re.sub(r"[^\w一-鿿\-]+", "", label)[:16] or "task"
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    return f"{safe}_{ts}"


def _deliver_result(task_id: str, title: str, content: str, ok: bool) -> None:
    """子任务跑完的收尾：推送结果，绝不向上抛（没有人在等它）。"""
    try:
        from core import delivery
        res = delivery.deliver(
            f"spawn:{task_id}", title, content,
            severity="normal" if ok else "high",
        )
        if not res.get("delivered"):
            logger.warning("子任务『%s』结果未送达：%s", task_id,
                           res.get("sent") or res.get("reason"))
    except Exception as e:  # noqa: BLE001
        logger.warning("子任务结果投递异常：%s", e)


async def _run_subtask_detached(task_id: str, task: str, label: str) -> None:
    try:
        res = await spawn(task, label=label)
        _deliver_result(task_id, f"子任务『{label}』完成", res.brief(), ok=res.ok)
    except Exception as e:  # noqa: BLE001
        logger.exception("子任务异常：%s", task_id)
        _deliver_result(task_id, f"子任务『{label}』失败", f"运行异常：{e}", ok=False)
    finally:
        _RUNNING.pop(task_id, None)


async def _run_fanout_detached(task_id: str, tasks: list, label: str) -> None:
    try:
        results = await spawn_many([{"task": t, "label": f"扇出{i + 1}"}
                                    for i, t in enumerate(tasks)])
        content = "\n\n".join(r.brief() for r in results)
        ok = all(r.ok for r in results)
        _deliver_result(task_id, f"扇出任务『{label}』完成（{len(tasks)} 项）", content, ok=ok)
    except Exception as e:  # noqa: BLE001
        logger.exception("扇出任务异常：%s", task_id)
        _deliver_result(task_id, f"扇出任务『{label}』失败", f"运行异常：{e}", ok=False)
    finally:
        _RUNNING.pop(task_id, None)


async def _run_document_fanout_detached(task_id: str, path: str, task_template: str,
                                        chunk_chars: int, label: str) -> None:
    try:
        from connectors.document import _extract_document_text
        from core import chunking

        ok, text = await _extract_document_text(path)
        if not ok:
            _deliver_result(task_id, f"大文档任务『{label}』失败", f"读取文档失败：{text}", ok=False)
            return
        chunks = chunking.chunk_text(text, chunk_chars=chunk_chars)
        if not chunks:
            _deliver_result(task_id, f"大文档任务『{label}』失败", "文档提取出的内容为空", ok=False)
            return
        results = await chunking.fanout_over_chunks(
            task_template, text, chunk_chars=chunk_chars, label=label)
        content = (f"共切成 {len(chunks)} 块，逐块结果如下（原文顺序）：\n\n"
                   + "\n\n".join(f"【第{i + 1}块】\n{r.brief()}" for i, r in enumerate(results)))
        ok_all = all(r.ok for r in results)
        _deliver_result(task_id, f"大文档任务『{label}』完成（{len(chunks)} 块）", content, ok=ok_all)
    except Exception as e:  # noqa: BLE001
        logger.exception("大文档分块任务异常：%s", task_id)
        _deliver_result(task_id, f"大文档任务『{label}』失败", f"运行异常：{e}", ok=False)
    finally:
        _RUNNING.pop(task_id, None)


@tool(
    "spawn_subtask",
    "把一个需要多轮查询/阅读的重活派给隔离的子 agent 在【后台】完成，不占用当前对话——"
    "调用后立刻返回『已派发』，你可以继续聊别的，子 agent 跑完会自动推送结果（飞书/网页"
    "通知）。适合：调研某公司/主题、通读长文档后总结、跨多个信息源核对事实。子 agent 只有"
    "只读权限（查记忆/文档/联网读），不能写入或对外操作。派发前告知用户一声『我会让人去查，"
    "查完告诉你』。想知道进度用 spawn_status。",
    {
        "type": "object",
        "properties": {
            "task":  {"type": "string", "description": "任务描述，自包含（子 agent 看不到本对话）"},
            "label": {"type": "string", "description": "任务短标签，如 B司调研"},
        },
        "required": ["task"],
    },
    effect=effects.READ_EXTERNAL,
)
async def spawn_subtask(task: str, label: str = "") -> str:
    label = label or (task[:20] + "…" if len(task) > 20 else task)
    task_id = _new_task_id(label)
    _RUNNING[task_id] = {"label": label, "kind": "subtask",
                         "started_at": datetime.now(timezone.utc).isoformat()}
    t = asyncio.create_task(_run_subtask_detached(task_id, task, label))
    t.add_done_callback(lambda fut: fut.exception())   # 吞掉未观察异常告警
    return (f"已派发子任务『{label}』（id: {task_id}），预计几十秒到几分钟，在后台跑，"
            f"跑完我会推送结果给你，期间可以继续聊别的。")


@tool(
    "spawn_fanout",
    "并发派出多个只读子 agent 在【后台】完成（扇出调研）：同类任务批量做，如「分别调研这"
    "5 家公司」。调用后立刻返回『已派发』，全部跑完汇总结果一并推送。tasks 传任务描述数组，"
    "数量建议 ≤5。想知道进度用 spawn_status。",
    {
        "type": "object",
        "properties": {
            "tasks": {"type": "array", "items": {"type": "string"},
                      "description": "任务描述列表，每条自包含"},
            "label": {"type": "string", "description": "这批扇出任务的短标签，如 5家目标客户调研"},
        },
        "required": ["tasks"],
    },
    effect=effects.READ_EXTERNAL,
)
async def spawn_fanout(tasks: list, label: str = "") -> str:
    if not tasks:
        return "任务列表为空。"
    if len(tasks) > 5:
        return f"一次最多 5 个子任务（收到 {len(tasks)} 个），请分批。"
    label = label or f"{len(tasks)}项扇出任务"
    task_id = _new_task_id(label)
    _RUNNING[task_id] = {"label": label, "kind": "fanout",
                         "started_at": datetime.now(timezone.utc).isoformat()}
    t = asyncio.create_task(_run_fanout_detached(task_id, list(tasks), label))
    t.add_done_callback(lambda fut: fut.exception())
    return (f"已派发『{label}』（id: {task_id}，共 {len(tasks)} 项），在后台并发跑，"
            f"全部跑完我会把汇总结果推送给你，期间可以继续聊别的。")


@tool(
    "process_large_document",
    "任务体量太大的标准安全解法（任务 #15）——当 read_document 提示文档被截断、而你的"
    "任务需要处理【全文】（如翻译整份文档、逐段摘要、全文核对）时用这个，不要凭截断内容"
    "硬做，更不要自己发明变通方法（比如尝试调用/安装其它程序）。会在服务端把全文自动"
    "切块、并发派多个只读子agent并行处理，跑完把每块结果汇总推送给你——调用后立刻返回"
    "『已派发』，可以继续聊别的。task_template 是你希望对【每一块】执行的指令，必须包含"
    "占位符 {chunk}（会被替换成该块的实际文本），例如："
    "\"阅读以下文本片段并翻译成中文，只输出译文：\\n\\n{chunk}\"。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文档完整路径"},
            "task_template": {"type": "string",
                              "description": "对每一块执行的指令模板，必须包含 {chunk} 占位符"},
            "chunk_chars": {"type": "integer", "description": "每块的目标字符数，默认 12000"},
            "label": {"type": "string", "description": "这个大文档任务的短标签，如「XX报告全文翻译」"},
        },
        "required": ["path", "task_template"],
    },
    effect=effects.READ_EXTERNAL,
)
async def process_large_document(path: str, task_template: str, chunk_chars: int = 12000,
                                 label: str = "") -> str:
    if "{chunk}" not in task_template:
        return "task_template 必须包含 {chunk} 占位符（会被替换成每一块的实际文本），请修正后重调。"
    label = label or f"大文档处理:{path.split('/')[-1]}"
    task_id = _new_task_id(label)
    _RUNNING[task_id] = {"label": label, "kind": "document_fanout",
                         "started_at": datetime.now(timezone.utc).isoformat()}
    t = asyncio.create_task(_run_document_fanout_detached(
        task_id, path, task_template, max(1000, int(chunk_chars or 12000)), label))
    t.add_done_callback(lambda fut: fut.exception())
    return (f"已派发『{label}』（id: {task_id}），会先读取全文再自动分块并发处理，"
            f"预计几十秒到数分钟（取决于文档大小），跑完我会把逐块结果汇总推送给你，"
            f"期间可以继续聊别的。")


@tool(
    "spawn_status",
    "查看后台子任务运行状态：哪些正在跑（spawn_subtask/spawn_fanout 派发的）。"
    "用户问「那个调研做完了吗/子任务什么情况」时用。只读。",
    {"type": "object", "properties": {}},
)
async def spawn_status() -> str:
    if not _RUNNING:
        return "当前没有在跑的后台子任务。"
    lines = ["⏳ 正在后台运行的子任务："]
    for tid, info in sorted(_RUNNING.items(), key=lambda kv: kv[1]["started_at"]):
        lines.append(f"  [{info['kind']}] {info['label']}（id: {tid}，"
                     f"始于 {info['started_at'][:19]}）")
    lines.append("跑完会自动推送结果，无需反复查询。")
    return "\n".join(lines)
