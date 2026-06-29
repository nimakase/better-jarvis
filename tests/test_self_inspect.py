"""只读自省工具的安全测试 —— connectors/self_inspect。

守护：工具确实注册、归属标注正确、且对密钥/运行态/越界路径一律拒读。
只依赖 registry/safety/self_model（轻），沙箱可跑。
"""
import asyncio
import sys
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from core import registry  # noqa: E402
import connectors.self_inspect as si  # noqa: E402  导入即触发 @tool 注册

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


names = [d["name"] for d in registry.definitions()]
check("list_self_modules 已注册", "list_self_modules" in names)
check("read_self_source 已注册", "read_self_source" in names)


async def run():
    m = await si.list_self_modules()
    check("地图含两区且非空", ("PROTECTED" in m and "OPEN" in m and "controller.py" in m))

    # 归属标注：核心 vs 周边
    r = await si.read_self_source("core/registry.py")
    check("核心文件标注 PROTECTED", "PROTECTED" in r.splitlines()[1])
    r2 = await si.read_self_source("connectors/document.py")
    check("周边文件标注 OPEN", "OPEN" in r2.splitlines()[1])
    # 文档可读
    rdoc = await si.read_self_source("ARCHITECTURE.md")
    check("文档可读", rdoc.startswith("文件：ARCHITECTURE.md"))

    # 拒读：密钥 / 运行态 / 越界 / 缓存
    for bad in [".env", "data/memory.db", "../secrets.txt", "core/__pycache__/x.pyc"]:
        rr = await si.read_self_source(bad)
        check(f"拒读 {bad}", rr.startswith("拒绝") or "越界" in rr)

    # 非白名单扩展名拒读（即便路径不存在，扩展名先挡）
    rr = await si.read_self_source("frontend/icon.png")
    check("非白名单扩展名拒读", rr.startswith("拒绝"))

    # 不存在文件友好提示
    rr = await si.read_self_source("nope/ghost.py")
    check("不存在友好提示", "不存在" in rr)

    # 空路径
    rr = await si.read_self_source("")
    check("空路径提示", "需要提供文件路径" in rr)


asyncio.run(run())
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
