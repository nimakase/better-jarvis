"""
工具注册中心（registry）

全项目工具的单一事实来源。连接器用 @tool 装饰业务函数即可自注册，
无需再写 TOOL_DEFS / handler 包装 / register_* 样板。

- ToolSpec：一个工具的完整描述（名称、说明、输入 schema、处理函数）。
- @tool(...)：装饰器，声明式注册一个工具（推荐的新写法）。
- register_tool(definition, handler)：兼容旧的「dict 定义 + handler」写法。
- definitions() / get_handler()：供 controller 读取工具列表与分发执行。
- discover_connectors()：导入 connectors/ 下所有模块，触发其 @tool 装饰
  与遗留的 register_*_tools()（迁移期两种写法并存）。
"""

import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("jarvis.registry")

_EMPTY_SCHEMA = {"type": "object", "properties": {}}


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable
    group: str = "general"   # 领域分组，为将来 controller 渐进披露铺路（默认 general）
    origin: str = "builtin"  # "builtin"=第一方(连接器/元工具) | "skill"=运行时自建技能
    effect: str = ""         # 动作效应等级（见 core/effects.LEVELS）；空=未声明，
                             # 由 effects.effect_of() 按内置表/默认值兜底
    timeout_s: "float | None" = 0  # 强制超时秒数（见 core/tool_timeout.py）；
                             # 0=未声明，由 timeout_of() 按内置表/15s 默认值兜底；
                             # None=显式声明"不设超时"（极少数，需慎用）


# 注册表：name -> ToolSpec；_order 保留注册顺序（影响呈现给模型的顺序）
_SPECS: dict[str, ToolSpec] = {}
_ORDER: list[str] = []


def register_spec(spec: ToolSpec, replace: bool = False) -> bool:
    """注册一个 ToolSpec，返回是否成功登记。

    默认 replace=False：重名跳过（首次注册优先）——第一方工具（连接器/元工具）
    启动时先注册，撞名者被忽略并告警。

    replace=True（自建技能重激活/更新用）：允许覆盖【同名的已有技能注册】，
    使"编辑工具→重新激活"能在不重启进程的情况下即时生效。但仍**绝不允许**用
    自建技能覆盖第一方工具（origin=="builtin"）——安全边界保留。
    """
    existing = _SPECS.get(spec.name)
    if existing is not None:
        if not replace:
            logger.warning(
                "工具重名，忽略后注册者（保留先注册的）：name=%r 已存在(group=%r)，"
                "跳过新注册(group=%r)。若是自建技能撞了第一方工具名，请给技能改名。",
                spec.name, existing.group, spec.group,
            )
            return False
        if existing.origin == "builtin" and spec.origin != "builtin":
            logger.warning("拒绝用自建技能覆盖第一方工具：name=%r", spec.name)
            return False
        _SPECS[spec.name] = spec          # 原位替换，_ORDER 位置不变
        return True
    _SPECS[spec.name] = spec
    _ORDER.append(spec.name)
    return True


def register_tool(definition: dict, handler: Callable) -> None:
    """兼容旧写法：用 input_schema 格式的 dict + handler 注册（第一方，首次优先）。"""
    register_spec(ToolSpec(
        name=definition["name"],
        description=definition.get("description", ""),
        input_schema=definition.get("input_schema", dict(_EMPTY_SCHEMA)),
        handler=handler,
        group=definition.get("group", "general"),
    ))


def register_skill_tool(definition: dict, handler: Callable) -> tuple[bool, str]:
    """注册/更新一个【自建技能】工具（origin="skill"，允许替换自己之前的注册）。
    返回 (ok, message)。撞第一方工具名时拒绝并给出可读原因。"""
    spec = ToolSpec(
        name=definition["name"],
        description=definition.get("description", ""),
        input_schema=definition.get("input_schema", dict(_EMPTY_SCHEMA)),
        handler=handler,
        group=definition.get("group", "general"),
        origin="skill",
    )
    if register_spec(spec, replace=True):
        return True, "ok"
    return False, (f"工具名 {spec.name!r} 与第一方工具冲突，自建技能不能覆盖它，请给技能改名。")


def unregister(name: str) -> bool:
    """从活着的注册表里注销一个【自建技能】工具（供 deactivate/delete 用），
    让停用/删除在不重启进程的情况下即时生效。第一方工具拒绝注销。"""
    spec = _SPECS.get(name)
    if spec is None:
        return False
    if spec.origin == "builtin":
        logger.warning("拒绝注销第一方工具：%r", name)
        return False
    del _SPECS[name]
    try:
        _ORDER.remove(name)
    except ValueError:
        pass
    return True


def tool(name: str, description: str, input_schema: Optional[dict] = None,
         group: str = "general", effect: str = "", duration: str = ""):
    """装饰器：把一个（async）函数声明为工具并注册。

    用法：
        @tool("weather", "查询天气", {"type":"object","properties":{...},"required":[...]})
        async def weather(city: str) -> str: ...

    group：领域分组（如 "signal_intel"），默认 "general"。仅作元数据，
    不改变现有披露行为；为将来按组渐进披露铺路。
    effect：动作效应等级（core/effects.LEVELS 之一）。空=未声明，
    由 effects 模块的内置表/默认值兜底。新工具建议显式声明。
    duration：耗时分类标签（core/tool_timeout.FAST/SLOW/UNBOUNDED 之一）。
    空=未声明，由 tool_timeout 模块的内置表/15s 默认值兜底。预期比 15s 默认值
    慢的新工具（联网调用、OCR、多步生成……）建议显式声明 duration="slow"；
    预期会长时间跑（分钟级）的活，第一反应应该是改造成 spawn_subtask 派发到
    后台，而不是声明 duration="unbounded"。
    """
    def deco(fn: Callable) -> Callable:
        timeout_s: "float | None" = 0
        if duration:
            from core.tool_timeout import _TAG_SECONDS
            timeout_s = _TAG_SECONDS.get(duration, 0)
        register_spec(ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema or dict(_EMPTY_SCHEMA),
            handler=fn,
            group=group,
            effect=effect,
            timeout_s=timeout_s,
        ))
        return fn
    return deco


def definitions(group: Optional[str] = None) -> list[dict]:
    """按注册顺序返回工具的 input_schema 定义（供 controller 转 OpenAI 格式）。

    group=None（默认）→ 全部工具，行为与历史完全一致。
    group="x"        → 只返回该组工具（为渐进披露预留，现阶段无人传）。
    """
    specs = (_SPECS[n] for n in _ORDER)
    return [
        {"name": s.name, "description": s.description, "input_schema": s.input_schema}
        for s in specs if group is None or s.group == group
    ]


def groups() -> dict[str, list[str]]:
    """返回 {组名: [工具名, ...]}，按注册顺序。供分组披露/自省使用。"""
    out: dict[str, list[str]] = {}
    for n in _ORDER:
        out.setdefault(_SPECS[n].group, []).append(n)
    return out


def get_handler(name: str) -> Optional[Callable]:
    spec = _SPECS.get(name)
    return spec.handler if spec else None


def iter_specs() -> list[ToolSpec]:
    return [_SPECS[n] for n in _ORDER]


def discover_connectors(skip: tuple = ()) -> list[str]:
    """导入 connectors/ 包下所有模块，触发自注册。

    - 新写法（@tool）：模块被导入时装饰器即完成注册。
    - 旧写法：导入后调用模块内任意 register_*_tools() 函数。
    返回已加载的模块名列表。
    """
    import importlib
    import pkgutil
    import connectors

    loaded = []
    for modinfo in pkgutil.iter_modules(connectors.__path__):
        name = modinfo.name
        if name.startswith("_") or name in skip:
            continue
        mod = importlib.import_module(f"connectors.{name}")
        # 兼容旧写法：调用遗留的 register_*_tools()
        for attr in dir(mod):
            if attr.startswith("register_") and attr.endswith("_tools"):
                fn = getattr(mod, attr)
                if callable(fn):
                    fn()
        loaded.append(name)
    return loaded
