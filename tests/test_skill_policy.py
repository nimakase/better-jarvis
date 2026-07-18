"""自建技能策略测试 —— 造工具质量护栏的回归网。

由 oem_ems_screener 的返工总结而来：那一版工具通过了当时全部三道门（静态校验、
API 一致性、子进程冒烟），却在真跑时列索引错位、静默只抓第一页、input() 卡死。
根因是【注入给生成器的 API 面】和【真正要用的 API 面】不一致，且没有任何一道门
检查"运行环境假设"。这里把补上的两条护栏钉死，防止以后被悄悄改回去。

零重依赖（skill_policy 只用 ast/pathlib），任何环境可跑。
"""
import sys
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from core import skill_policy as sp  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ── 1. 运行环境契约：stdin 类调用必须是阻断级 ──────────────────────────────
for fn in ["input", "getpass", "breakpoint"]:
    code = f"async def t():\n    {fn}()\nTOOL_DEF = {{}}\n"
    check(f"check_runtime_contract 拦截 {fn}()", len(sp.check_runtime_contract(code)) > 0)

check(
    "check_runtime_contract 不误报干净代码",
    sp.check_runtime_contract("async def t():\n    return 'ok'\nTOOL_DEF = {}\n") == [],
)
check(
    "check_runtime_contract 容忍语法错误（另有报错）",
    sp.check_runtime_contract("def (((") == [],
)

# ── 2. building block 注入面：语义信息必须真的被渲染出去 ────────────────────
api = sp.building_blocks_api_text()

# 2a. 显式暴露的私有方法要出现（它们才是正确用法，只给公共签名会逼模型自造）
for m in ["_extract_name", "_extract_owner", "_column_idx", "_collect_column_index_map"]:
    check(f"注入面包含私有复用方法 {m}", m in api)

# 2b. 模块级函数/常量要出现（技能必须 import 它们，猜不出来）
for s in ["resolve_paths", "ensure_directories", "NAME_COL_LABEL", "HUBSPOT_ROW_SELECTOR"]:
    check(f"注入面包含 {s}", s in api)

# 2c. 语义说明与范例要出现——这是"签名对了但用法错了"的唯一解药
check("注入面包含【使用约定】", "使用约定" in api)
check("注入面包含【正确用法范例】", "正确用法范例" in api)
check("注入面点明 data-column-index 不是位置下标", "不是 td 的位置下标" in api)
check("注入面点明 profile 目录须与 app 共用", "JARVIS_DATA_DIR" in api)
check("注入面点明分页不可静默截断", "静默数据截断" in api)

# 2d. 每个 building block 声明的 symbols 都要真能在源码里找到（防止改名后静默漏注入）
repo = Path(JARVIS)
for mod, meta in sp.BUILDING_BLOCKS.items():
    src = repo / (mod.replace(".", "/") + ".py")
    check(f"{mod} 源文件存在", src.is_file())
    if not src.is_file():
        continue
    text = src.read_text(encoding="utf-8")
    for sym in (meta.get("symbols") or []) + (meta.get("constants") or []):
        check(f"{mod} 的 {sym} 在源码中存在", f"{sym}" in text)
    for cls, privs in (meta.get("expose_private") or {}).items():
        for p in privs:
            check(f"{cls}.{p} 在源码中存在", f"def {p}(" in text)

# ── 3. 一致性检查仍然有效（不能因为暴露了私有方法就放行臆造）────────────────
hallucinated = (
    "from prospecting.hubspot_worker import HubSpotBrowser\n"
    "async def t():\n"
    "    b = HubSpotBrowser(None, None)\n"
    "    b.fetch_everything()\n"
    "TOOL_DEF = {}\n"
)
check("check_building_block_usage 仍拦截臆造方法", len(sp.check_building_block_usage(hallucinated)) > 0)

real = (
    "from prospecting.hubspot_worker import HubSpotBrowser\n"
    "async def t():\n"
    "    b = HubSpotBrowser(None, None)\n"
    "    b._extract_name(None)\n"
    "TOOL_DEF = {}\n"
)
check("check_building_block_usage 放行真实私有方法", sp.check_building_block_usage(real) == [])

# ── 4. 造工具提示词必须带上用户业务背景 ────────────────────────────────────
# 动机：oem_ems_screener 复盘发现造工具的提示词只有「工具名 + 需求」，用户档案完全
# 进不去，于是业务判定类工具的判据只能照需求字面直译（"是不是制造商" vs 真正需要的
# "在供应链的哪一端"）。这里只校验【源码层面的接线】，不 import tool_builder
# （它依赖 openai/apscheduler 等重包，本测试要保持零重依赖、任何环境可跑）。
tb = (repo / "core" / "tool_builder.py").read_text(encoding="utf-8")

check("tool_builder 定义了 _user_context_text", "def _user_context_text(" in tb)
check("_user_context_text 读用户档案 profile.build_block", "profile.build_block()" in tb)
check("create_tool 注入了业务背景",
      "base = _user_context_text() + env + ref" in tb)
check("edit_tool 也注入了业务背景",
      "base = (_user_context_text() + env + ref" in tb)
check("注入可被 JARVIS_TOOL_AUTHOR_PROFILE=0 关闭",
      "JARVIS_TOOL_AUTHOR_PROFILE" in tb)
check("提示模型把隐含业务假设写进 docstring", "docstring" in tb and "假设" in tb)
check("提示模型忽略无关个人条目", "家人" in tb or "无关的个人条目" in tb)

# ── 5. 需求澄清门：必须存在，且必须【问一轮就走】────────────────────────────
# 动机：oem_ems_screener 返工的根因几乎全在需求层面（"全部"其实有 22 页、
# "OEM/EMS"其实指供应链下游），问一句就能避免。
# 但这个门最大的风险是【变成盘问】——造个小工具被追着问三轮就没人用了。
# 下面把「只问一轮」和「默认不问」这两条保证钉死。
check("存在澄清门 _gather_clarifications", "async def _gather_clarifications(" in tb)
check("create_tool 接受 clarifications 参数",
      "async def create_tool(name: str, request: str, clarifications: str = \"\")" in tb)
check("clarifications 非空即跳过澄清门（保证只问一轮）",
      "if not clarifications.strip():" in tb)
check("create_tool 的 schema 暴露了 clarifications", '"clarifications": {' in tb)
check("handler 透传 clarifications",
      "async def _handle_create_tool(name: str, request: str, clarifications: str = \"\")" in tb)
check("澄清门可被 JARVIS_TOOL_AUTHOR_CLARIFY=0 关闭", "JARVIS_TOOL_AUTHOR_CLARIFY" in tb)
check("问题数量有上限", "_MAX_CLARIFY_QUESTIONS" in tb)
check("提示词要求默认不问（宁可不问也不凑数）", "宁可不问也不要凑数" in tb)
check("提示词明确禁止问技术细节/客套话", "绝对不要问" in tb)
check("提示词要求给出默认假设（用户不回也能继续）", "默认假设" in tb)
check("澄清失败时降级为空、不阻断造工具", "澄清步骤绝不阻断造工具" in tb)

# ── 6. SELFTEST 钩子：冒烟阶段真的执行，且空测试必须被拦 ────────────────────
# 动机：冒烟此前只 import 不执行 handler，"能加载但结果是错的"全部逃逸。
check("冒烟脚本会执行 SELFTEST", "SELFTEST" in tb and "SELFTEST_RAN" in tb)
check("SELFTEST 支持 async", "inspect.isawaitable" in tb)
check("存在空测试检查 check_selftest_quality", "def check_selftest_quality(" in tb)
check("空测试检查已接入 validate_tool_code", "errors.extend(check_selftest_quality(code))" in tb)
check("SELFTEST 失败与 import 失败的报错要能区分",
      "SELFTEST 未通过" in tb and "import/加载失败" in tb)
check("提示词要求 SELFTEST 只跑纯逻辑", "只跑纯逻辑" in tb)
check("提示词点明空测试比没测试更糟", "空测试比没有测试更糟" in tb)

# ── 7. 环境探针：写代码前看一眼目标页面 ─────────────────────────────────────
from core import env_probe  # noqa: E402  （只依赖标准库，playwright 是惰性 import）

check("无 URL 的需求不触发探针（零成本）",
      env_probe.extract_urls("写一个查天气的工具，输入城市名返回温度") == [])
check("能从需求里提取 http(s) URL",
      env_probe.extract_urls("从 https://app.hubspot.com/x/list 抓名单") == ["https://app.hubspot.com/x/list"])
check("中文标点结尾要正确剥离",
      env_probe.extract_urls("打开 https://example.com/list。然后抓取") == ["https://example.com/list"])
check("拒绝带凭据的 URL（user:pass@）",
      env_probe.extract_urls("访问 https://user:pass@evil.com/x") == [])
check("拒绝非 http(s) 协议", env_probe.extract_urls("ftp://files.example.com/a") == [])
check("提取数量有上限",
      len(env_probe.extract_urls(" ".join(f"https://e{i}.com" for i in range(9)), limit=2)) == 2)
check("非法 URL 时 probe_page 直接返回空串（不启动浏览器）",
      env_probe.probe_page("ftp://x/y") == "" and env_probe.probe_page("not a url") == "")

ep = (repo / "core" / "env_probe.py").read_text(encoding="utf-8")
check("探针只读：不点击 / 不填表 / 不提交",
      ".click(" not in ep and ".fill(" not in ep and "submit" not in ep.lower())
check("探针文档写明 URL 只取自用户需求（防注入）", "不接受从页面内容里发现的 URL" in ep)
check("摘要有体积上限", "MAX_DIGEST_CHARS" in ep)
check("最有判别力的翻页控件排在摘要前部",
      ep.index("【翻页 / 分页控件】") < ep.index("【表头】") < ep.index("主内容区的 data-test-id"))
check("探针失败降级为空串、不阻断造工具", "降级为不注入" in ep)
check("tool_builder 接入了探针且有 URL 门控",
      "_gather_env_probe" in tb and "extract_urls" in tb)
check("探针可被 JARVIS_TOOL_ENV_PROBE=0 关闭", "JARVIS_TOOL_ENV_PROBE" in tb)

print()
if fails:
    print(f"{len(fails)} 项失败：" + "; ".join(fails))
    sys.exit(1)
print("全部通过")
