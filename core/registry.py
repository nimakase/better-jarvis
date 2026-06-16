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

from dataclasses import dataclass
from typing import Callable, Optional

_EMPTY_SCHEMA = {"type": "object", "properties": {}}


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable
    group: str = "general"   # 领域分组，为将来 controller 渐进披露铺路（默认 general）


# 注册表：name -> ToolSpec；_order 保留注册顺序（影响呈现给模型的顺序）
_SPECS: dict[str, ToolSpec] = {}
_ORDER: list[str] = []


def register_spec(spec: ToolSpec) -> None:
    """注册一个 ToolSpec。重名自动跳过（首次注册优先）。"""
    if spec.name in _SPECS:
        return
    _SPECS[spec.name] = spec
    _ORDER.append(spec.name)


def register_tool(definition: dict, handler: Callable) -> None:
    """兼容旧写法：用 input_schema 格式的 dict + handler 注册。"""
    register_spec(ToolSpec(
        name=definition["name"],
        description=definition.get("description", ""),
        input_schema=definition.get("input_schema", dict(_EMPTY_SCHEMA)),
        handler=handler,
        group=definition.get("group", "general"),
    ))


def tool(name: str, description: str, input_schema: Optional[dict] = None,
         group: str = "general"):
    """装饰器：把一个（async）函数声明为工具并注册。

    用法：
        @tool("weather", "查询天气", {"type":"object","properties":{...},"required":[...]})
        async def weather(city: str) -> str: ...

    group：领域分组（如 "signal_intel"），默认 "general"。仅作元数据，
    不改变现有披露行为；为将来按组渐进披露铺路。
    """
    def deco(fn: Callable) -> Callable:
        register_spec(ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema or dict(_EMPTY_SCHEMA),
            handler=fn,
            group=group,
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
