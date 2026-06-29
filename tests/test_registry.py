"""registry 核心冒烟 —— 注册/分发/重名语义。

registry 是受保护核心，但其行为是全系统工具的地基，必须有冒烟覆盖。
registry 只依赖 logging/dataclass（轻），沙箱可跑。
"""
import sys
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from core import registry  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


@registry.tool("t_smoke_a", "desc A", {"type": "object", "properties": {}}, group="g1")
async def a():
    return "a"


@registry.tool("t_smoke_b", "desc B", group="g2")
async def b():
    return "b"


names = [d["name"] for d in registry.definitions()]
check("@tool 注册", "t_smoke_a" in names and "t_smoke_b" in names)
check("get_handler 可取", registry.get_handler("t_smoke_a") is not None)
check(
    "definitions 三字段齐全",
    all({"name", "description", "input_schema"} <= set(d) for d in registry.definitions()),
)
check(
    "省略 schema 时填默认 object",
    any(
        d["name"] == "t_smoke_b" and d["input_schema"].get("type") == "object"
        for d in registry.definitions()
    ),
)

g = registry.groups()
check("groups 含 g1/g2", "g1" in g and "g2" in g)
check("未知工具 handler 为 None", registry.get_handler("不存在的工具名") is None)


# 重名：保留先注册者（安全语义——自建技能不得覆盖第一方）
@registry.tool("t_smoke_a", "二次注册（应被忽略）", group="gX")
async def a2():
    return "a2"


check("重名保留先注册的 handler", registry.get_handler("t_smoke_a") is a)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
