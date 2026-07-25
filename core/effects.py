"""
core/effects.py — 动作效应模型（运行时护栏 · 第一层）

给每个工具一个「效应等级」，回答：这次调用的后果有多大、可不可逆。
这是与 core/self_model（代码维度：哪个文件能改）正交的另一套护栏——
运行时维度：哪个动作能直接做、哪个必须过闸。

五级（由轻到重）：
    read_local     只读本机（查记忆、读文档、列清单）
    read_external  只读外部（联网查询、抓网页）
    write_local    写本机（存记忆、生成文件、建日历）
    write_external 对外产生影响（发消息、发邮件、推送）
    irreversible   不可逆（删除数据/文件/工具/日程）

第一刀只机械拦 irreversible：模型调用不可逆工具时，第一次会被物理拦下，
必须经过【至少一个用户回合】（用户看到并回话）后重调同名同参才放行。
此前这条规则只写在 system prompt 的自然语言里（模型可忽略）；现在落进代码。

分级来源（优先级从高到低）：
    1. ToolSpec.effect —— 工具注册时自带声明；
    2. 本文件的 BUILTIN_EFFECTS 表 —— 给存量工具集中标注，不必逐个改连接器；
    3. 默认 DEFAULT_EFFECT（write_local）—— 未知工具不会误触 irreversible 闸，
       但也被记为「有写入可能」，后续收紧时（如子 agent 白名单）按此保守对待。

设计原则：逻辑全在本模块（纯函数 + 小状态类，可确定性单测），
controller 只加十几行胶水调用。
"""
from __future__ import annotations

# ── 五级效应 ──────────────────────────────────────────────────────────────────

READ_LOCAL = "read_local"
READ_EXTERNAL = "read_external"
WRITE_LOCAL = "write_local"
WRITE_EXTERNAL = "write_external"
IRREVERSIBLE = "irreversible"

LEVELS = [READ_LOCAL, READ_EXTERNAL, WRITE_LOCAL, WRITE_EXTERNAL, IRREVERSIBLE]
_RANK = {name: i for i, name in enumerate(LEVELS)}

# 未在任何处声明的工具 → 保守按「会写本机」对待（不触发确认闸，但不算只读）。
DEFAULT_EFFECT = WRITE_LOCAL

# ── 存量工具的集中标注（来源 2）─────────────────────────────────────────────
# 注：新工具应在注册时自带 effect（来源 1）；本表只为存量工具兜底，
# 两处都写时以注册声明为准。

BUILTIN_EFFECTS: dict[str, str] = {
    # —— 只读本机 ——
    "list_credentials": READ_LOCAL,
    "list_documents": READ_LOCAL,
    "read_document": READ_LOCAL,
    "list_entities": READ_LOCAL,
    "lookup_entity": READ_LOCAL,
    "search_entities": READ_LOCAL,
    "recall": READ_LOCAL,
    "list_reports": READ_LOCAL,
    "find_report": READ_LOCAL,
    "calendar_agenda": READ_LOCAL,
    "delivery_status": READ_LOCAL,
    "list_self_modules": READ_LOCAL,
    "read_self_source": READ_LOCAL,
    "read_symbol": READ_LOCAL,
    "list_tools_meta": READ_LOCAL,
    "read_tool_code": READ_LOCAL,
    "review_tool": READ_LOCAL,
    "list_schedules": READ_LOCAL,
    # —— 只读外部 ——
    "weather": READ_EXTERNAL,
    # —— 写本机 ——
    "remember_fact": WRITE_LOCAL,
    "save_entity": WRITE_LOCAL,
    "remember_episode": WRITE_LOCAL,
    "update_credential": WRITE_LOCAL,
    "ingest_credential_image": WRITE_LOCAL,
    "ingest_document_file": WRITE_LOCAL,
    "update_document": WRITE_LOCAL,
    "calendar_create_event": WRITE_LOCAL,
    "calendar_update_event": WRITE_LOCAL,
    "generate_report": WRITE_LOCAL,
    "create_tool": WRITE_LOCAL,
    "edit_tool": WRITE_LOCAL,
    "update_tool_code": WRITE_LOCAL,
    "activate_tool": WRITE_LOCAL,
    "create_schedule": WRITE_LOCAL,
    "pause_schedule": WRITE_LOCAL,
    "resume_schedule": WRITE_LOCAL,
    "delivery_pause": WRITE_LOCAL,
    "delivery_resume": WRITE_LOCAL,
    "delivery_rest": WRITE_LOCAL,
    "delivery_vacation_default": WRITE_LOCAL,
    # reveal_credential 只显示在用户自己的界面（带外通道），不对外发送 → write_local
    "reveal_credential": WRITE_LOCAL,
    "send_file_to_chat": WRITE_LOCAL,
    # —— 对外影响 ——
    #（有副作用的多步任务：会开浏览器/连外部系统。目前靠工作流政策+确认；
    #  标 write_external 为后续 trust 闸和子 agent 白名单铺路。）
    "run_workflow": WRITE_EXTERNAL,
    # —— 不可逆（第一刀的确认闸对象）——
    "delete_credential": IRREVERSIBLE,
    "delete_document": IRREVERSIBLE,
    "delete_tool": IRREVERSIBLE,
    "delete_schedule": IRREVERSIBLE,
    "calendar_delete_event": IRREVERSIBLE,
    "cleanup_drafts": IRREVERSIBLE,      # confirm_delete 模式会真删文件
    "run_self_review": WRITE_LOCAL,      # 自我迭代已有自己的护栏（先红后绿+回滚）
}


def effect_of(tool_name: str) -> str:
    """查询一个工具的效应等级（来源优先级：注册声明 > 本表 > 默认）。"""
    try:
        from core import registry
        spec = registry._SPECS.get(tool_name)  # noqa: SLF001 — 同包内读
        if spec is not None and getattr(spec, "effect", ""):
            return spec.effect
    except Exception:
        pass
    return BUILTIN_EFFECTS.get(tool_name, DEFAULT_EFFECT)


def rank(effect: str) -> int:
    """效应等级 → 序数（比较用）。未知等级按最高危对待（fail-safe）。"""
    return _RANK.get(effect, _RANK[IRREVERSIBLE])


def at_least(effect: str, floor: str) -> bool:
    """effect 是否达到（≥）floor 等级。"""
    return rank(effect) >= rank(floor)


# ── 确认闸（irreversible 的机械拦截）──────────────────────────────────────────

CONFIRM_MESSAGE = (
    "⛔ 该操作不可逆（{effect}），已被安全闸拦下，本次【未执行】。\n"
    "请向用户用一句话复述你将要执行的具体动作和对象，等用户明确确认。\n"
    "用户确认后，再次以【完全相同的参数】调用本工具即可执行。\n"
    "若用户拒绝或改主意，不要重调。"
)


class ConfirmGate:
    """不可逆动作的「隔一个用户回合」确认闸。

    机械保证：一个 irreversible 调用要执行，必须满足——
      同名同参的调用在【上一个用户回合之前】被拦过一次，
      即用户至少有一次看到复述并回话的机会。

    状态迁移（每个 (tool, args) 键）：
      首次调用 → 拦下，进 pending；
      新用户回合到来 → pending 全部转 granted（一次性）；
      再次同名同参调用 → granted 命中，放行并销票；
      granted 未被使用而又过了一个用户回合 → 过期作废（防陈年授权复活）。

    纯内存、每会话一个实例；不依赖模型自觉，模型「忘了确认」也执行不了。
    """

    def __init__(self):
        self._pending: set[tuple[str, str]] = set()
        self._granted: set[tuple[str, str]] = set()

    def new_user_turn(self) -> None:
        """每个新的用户回合开始时调用：pending 晋级为 granted，旧 granted 过期。"""
        self._pending, self._granted = set(), self._pending

    def check(self, tool_name: str, args_json: str) -> tuple[bool, str]:
        """返回 (是否放行, 拦截时给模型看的话)。非 irreversible 一律放行。"""
        eff = effect_of(tool_name)
        if eff != IRREVERSIBLE:
            return True, ""
        key = (tool_name, args_json or "")
        if key in self._granted:
            self._granted.discard(key)  # 一次性票据
            return True, ""
        self._pending.add(key)
        return False, CONFIRM_MESSAGE.format(effect=eff)
