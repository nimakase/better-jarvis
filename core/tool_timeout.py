"""
core/tool_timeout.py — 工具耗时分类标签 + 强制超时机制（任务 #14）

背景：此前 controller._execute_tool 对工具调用【没有任何超时】——一个卡住的
工具（网络请求悬挂、外部进程不回应……2026-08-08 刚发生过一次真实案例：一个
配置错误/卡死的 MCP server 会让 mcp_discover_tools 无限期挂起整个对话回合）
会冻住整条主对话，跟贾维斯"秘书永远该在，慢活该丢给后台"的既定设计哲学
（[[spawn_subtask/spawn_fanout 的 detach+deliver 改造，任务 #13]]）直接冲突：
如果一个工具因为没设超时就能占住主线不放，detach 机制解决的问题就白解决了一半。

设计跟 core/effects.py 的效应分级同一个骨架（有意为之，降低认知负担）：
    1. ToolSpec.timeout_s  —— 工具注册时自带声明（优先级最高）
    2. 本文件 BUILTIN_TIMEOUTS 表 —— 给存量工具集中标注，不必逐个改连接器
    3. 默认 DEFAULT_TIMEOUT_S = 15 秒 —— 未声明的工具一律按"快工具"对待
       （2026-08-07 已与用户确认此默认值）

三档分类标签（FAST/SLOW/UNBOUNDED）只是给 BUILTIN_TIMEOUTS 表写注释用的
语义分组，实际生效的是数值秒数——不搞成另一套要查表的间接层。

UNBOUNDED（None）只应该给"本来就该长时间跑、且已经自己管理生命周期"的极少数
工具用（目前只有 run_self_review——它内部跑的是全量测试套件，可能几十秒到几分钟，
且有自己的红绿判据和回滚，不是"忘了加超时"，是"这活确实需要这么久"）。新工具
如果预期会超过 SLOW 档（60秒），第一反应应该是改造成 spawn_subtask 派发到后台，
而不是把 UNBOUNDED 当逃生舱——UNBOUNDED 需要能说清"为什么不能是子 agent"。

超时后的行为：_execute_tool 捕获 asyncio.TimeoutError，把它当一次工具失败
（telemetry 照记，err="timeout"），给模型一句可读的话："这活可能没那么快，建议
改用后台派发"——不是让模型静默卡死，也不是让异常冒泡打断整个对话。
"""
from __future__ import annotations

FAST = "fast"
SLOW = "slow"
UNBOUNDED = "unbounded"

_TAG_SECONDS = {FAST: 15.0, SLOW: 60.0, UNBOUNDED: None}

DEFAULT_TIMEOUT_S = _TAG_SECONDS[FAST]

# ── 存量工具的集中标注（来源 2）─────────────────────────────────────────────
# 注：新工具应在注册时自带 timeout_s（来源 1，@tool(..., duration="slow")）；
# 本表只为存量/未显式声明的工具兜底。两处都写时以注册声明为准。
BUILTIN_TIMEOUTS: dict[str, str] = {
    # —— 联网/外部 IO，legitimately 比 15s 默认久，但仍应该有界 ——
    "web_search": SLOW,
    "fetch_page": SLOW,
    "weather": SLOW,
    # mcp_discover_tools 已在 connectors/mcp_tools.py 注册时自带 duration="slow"
    # （来源1，优先于本表）——留在这里做注释性说明，不重复声明。
    # —— 本机但计算重（OCR/多步解析），偶发超过 15s ——
    "ingest_credential_image": SLOW,
    "ingest_document_file": SLOW,
    # —— 报告生成常内部再调一次模型补全，留够余量 ——
    "generate_report": SLOW,
    # —— 自我迭代闭环：内部跑全量测试套件，时长与用例数正相关，不该被腰斩 ——
    "run_self_review": UNBOUNDED,
}


def timeout_of(tool_name: str) -> "float | None":
    """查询一个工具的超时秒数（来源优先级：注册声明 > 本表 > 默认）。None=不设超时。"""
    try:
        from core import registry
        spec = registry._SPECS.get(tool_name)  # noqa: SLF001 — 同包内读，效仿 effects.effect_of
        # 0 是"未声明"哨兵值；None 是"显式声明不设超时"，两者含义相反，不能合并判断
        # （之前一版把 None 也当"未声明"处理，导致 unbounded 声明被悄悄忽略——已修）。
        if spec is not None and spec.timeout_s != 0:
            return spec.timeout_s
    except Exception:
        pass
    tag = BUILTIN_TIMEOUTS.get(tool_name)
    if tag is not None:
        return _TAG_SECONDS[tag]
    return DEFAULT_TIMEOUT_S
