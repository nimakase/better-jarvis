"""
core/self_review.py — 定时反思 + 自我迭代闭环编排（阶段 3b · 自身受保护）

把一轮自我迭代串起来：
  读上轮复盘 → 构建反思 prompt → 调模型生成提案 → 按区位路由
    · OPEN     → 交执行器 SelfIterator 自动落地（先红后绿 + 全量 gate + 可回滚）
    · PROTECTED→ 生成 code_review 提案（diff + 影响 + 动机）送你人工审，绝不自动改
  → 写本轮复盘文档（下轮先读再优化）。

LLM 调用通过 model_fn 注入，编排逻辑因此可被确定性单测覆盖（不依赖真实模型/联网）。
顶层只引轻依赖；真实模型客户端在 _default_model_fn 内惰性构建。
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from core import self_model
from core.self_iteration import Outcome, Proposal, SelfIterator
from core.results import Action, code_review

REPO = self_model.REPO_ROOT
DEFAULT_REVIEW_DIR = REPO / "docs" / "self_review"

ModelFn = Callable[[str], Awaitable[str]]

# —— 必要性闸：只有"真缺陷"才自动落地，杜绝为改而改 ——
AUTO_CATEGORIES = {"bugfix", "robustness", "correctness"}  # 可自动落地的类别
AUTO_SEVERITIES = {"high", "medium"}                       # 且严重度须达标
COOLDOWN_CYCLES = 3   # 最近 N 轮改过的文件，本轮不再碰（防来回折腾）


def necessity_verdict(p: Proposal) -> str:
    """返回 'auto'（够格自动落地）或 'defer'（非必要，仅记录不自动改）。"""
    cat = (p.category or "").strip().lower()
    sev = (p.severity or "").strip().lower()
    if cat in AUTO_CATEGORIES and sev in AUTO_SEVERITIES and (p.defect or "").strip():
        return "auto"
    return "defer"


# ── 反思 prompt（含已敲定的「好测试」硬要求）────────────────────────────────────
_TEST_RULES = """配套测试的硬性要求（决定改动能否自动落地，务必照做）：
1) 独立脚本风格：开头把仓库根加入 sys.path；失败时 sys.exit(非0)，全过 sys.exit(0)。
2) 先红后绿：测试必须在【你改动之前】的代码上失败、在改动之后通过——
   也就是它必须真正测到你改动的那个行为。改前改后都通过的测试会被自动判为无效并打回。
3) 必须 import / 引用被改的模块，测它**可观察的行为/契约**，不要测内部实现细节。
4) 至少包含一个对抗用例或边界用例，不能只走顺利路径。
5) 禁止 assert True 之类恒真占位；测试要短小、确定、可重复。"""

_NECESSITY_RULES = """必要性优先（先诊断、再开方，不是为改而改）：
- 每条提案必须先指出一个**具体缺陷**(defect)，而不是泛泛的"可以更优雅/更好"。
- 标注 category：bugfix（修错）/ robustness（健壮性，如未处理的异常/边界）/
  correctness（逻辑不正确）/ readability（可读性）/ style（风格）/ dedup（去重）。
- 标注 severity：high / medium / low。
- 只有 category∈{bugfix,robustness,correctness} 且 severity∈{high,medium} 且给出了具体
  defect 的提案才会**自动落地**；readability/style/低严重度的只会被记录、不自动改——
  所以如果某处只是"风格上可以更好"，**请直接不要提**（输出更短）。
- 没有真正的缺陷就输出 []。一次最多 3 条，宁缺毋滥。"""

_OUTPUT_FORMAT = """只输出一个 JSON 数组（不要其它解释文字），每个元素是一条提案：
[
  {
    "path": "相对仓库根的文件路径，如 connectors/document.py",
    "defect": "诊断出的具体缺陷（必要性依据；没有就别提这条）",
    "category": "bugfix | robustness | correctness | readability | style | dedup",
    "severity": "high | medium | low",
    "rationale": "为什么改（动机，一句话）",
    "impact": "改了会怎样（影响面，一句话）",
    "new_code": "改动后该文件的【完整】内容",
    "test_code": "配套测试脚本的完整内容"
  }
]"""


def build_reflection_prompt(focus_source: dict[str, str], last_review: str,
                            module_map: str, trouble: str = "") -> str:
    parts = [
        "你是贾维斯的自我维护者。目标：在【不破坏行为】的前提下，对【周边(OPEN)】代码做小步、稳妥的优化"
        "（可读性、健壮性、去重、修小 bug、补边界处理）。不要为改而改，不要大重构。",
        "",
        "## 代码边界（务必遵守）",
        "🔒 PROTECTED 的文件你也可以提改进建议，但它们不会自动生效、会转人工审核；"
        "🟢 OPEN 的文件若你给出合格的配套测试，将自动落地。地图：",
        module_map,
        "",
    ]
    if trouble:
        parts += [
            "## 真实运行故障画像（最高优先级的缺陷来源——有真实失败先修真实失败）",
            trouble,
            "",
        ]
    parts += [
        "## 上一轮复盘（接着它继续，不要重复已做/已否决的）",
        last_review or "（无，首轮）",
        "",
        "## 本轮聚焦的源码",
    ]
    for path, src in focus_source.items():
        parts.append(f"### {path}\n```python\n{src}\n```")
    parts += ["", "## " + _NECESSITY_RULES, "", "## " + _TEST_RULES,
              "", "## 输出格式", _OUTPUT_FORMAT]
    return "\n".join(parts)


# ── 解析模型输出 ──────────────────────────────────────────────────────────────
def parse_proposals(text: str) -> list[Proposal]:
    """从模型输出里稳健地抽出提案数组；坏元素跳过。

    2026-07-22 起统一走 core.json_salvage（括号深度抢救）——此前这里是朴素
    `\\[.*\\]` 正则，正是 json_salvage 文档头警告的写法：提案输出一截断整批丢。
    提案里带整文件代码，截断概率不低，抢救价值大。"""
    from core.json_salvage import salvage_json_array, strip_fence
    data = salvage_json_array(strip_fence(text or ""))
    if not isinstance(data, list) or not data:
        return []
    out: list[Proposal] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        path = (item.get("path") or "").strip()
        new_code = item.get("new_code")
        test_code = item.get("test_code")
        if not path or not isinstance(new_code, str) or not isinstance(test_code, str):
            continue  # 缺必需字段，跳过
        out.append(Proposal(
            path=path, new_code=new_code, test_code=test_code,
            rationale=(item.get("rationale") or "").strip(),
            impact=(item.get("impact") or "").strip(),
            category=(item.get("category") or "").strip(),
            severity=(item.get("severity") or "").strip(),
            defect=(item.get("defect") or "").strip(),
        ))
    return out


# ── 一轮闭环 ──────────────────────────────────────────────────────────────────
async def run_cycle(model_fn: ModelFn, *, iterator: Optional[SelfIterator] = None,
                    focus_source: Optional[dict[str, str]] = None,
                    review_dir: Optional[Path] = None,
                    module_map: Optional[str] = None) -> dict:
    # 任务 #17：一次反思周期铸一个 trace_id，全程内 telemetry.record 自动带上——
    # 事后能用 core.telemetry.calls_by_trace() 查这一轮反思触发的所有工具调用
    # （如 it.execute() 内部跑测试套件时的工具调用），不用再靠时间戳模糊对齐。
    from core import trace as _trace
    with _trace.scope(_trace.get() or _trace.new_id("review")):
        return await _run_cycle_body(
            model_fn, iterator=iterator, focus_source=focus_source,
            review_dir=review_dir, module_map=module_map)


async def _run_cycle_body(model_fn: ModelFn, *, iterator: Optional[SelfIterator] = None,
                          focus_source: Optional[dict[str, str]] = None,
                          review_dir: Optional[Path] = None,
                          module_map: Optional[str] = None) -> dict:
    it = iterator or SelfIterator()
    rdir = Path(review_dir or DEFAULT_REVIEW_DIR)
    src = focus_source if focus_source is not None else _default_focus(it.repo)
    mmap = module_map if module_map is not None else _module_map(it)

    last = _read_last_review(rdir)
    # 遥测注入（core/telemetry）：真实失败画像 > 读源码猜缺陷。失败降级为空。
    try:
        from core import telemetry as _telemetry
        trouble = _telemetry.trouble_report(days=7)
    except Exception:
        trouble = ""
    prompt = build_reflection_prompt(src, last, mmap, trouble=trouble)
    text = await model_fn(prompt)
    proposals = parse_proposals(text)

    recent = _recently_applied(rdir)
    # ㉕ 盲区修复：冷却不能只看自己写的复盘——外部手（Ned/外部工具）刚改过的
    # 文件同样该让路，否则反思会对着别人刚调好的文件重新提改动。读真实 git 历史
    # （近 2 天任何作者碰过的文件都冷却；大批外部提交后反思歇两天是合理的防御）。
    try:
        from core import drift as _drift
        recent |= _drift.recently_touched(days=2, repo=it.repo)
    except Exception:
        pass

    outcomes: list[Outcome] = []
    review_actions: list[Action] = []
    for p in proposals:
        # 冷却期：最近几轮改过的文件本轮不碰，防来回折腾
        if p.path in recent:
            outcomes.append(Outcome("skipped", p.path,
                                    f"冷却期内（最近 {COOLDOWN_CYCLES} 轮改过），跳过"))
            continue

        zone, reason = it.classify(p.path)
        if zone != "open":
            # 受保护：始终送人工（diff + 影响 + 动机）
            review_actions.append(_protected_proposal_action(it, p, reason))
            outcomes.append(Outcome("needs_human", p.path,
                                    "受保护文件，已生成提案送人工审核"))
            continue

        # 周边：过必要性闸——只有"真缺陷"才自动落地
        if necessity_verdict(p) == "auto":
            outcomes.append(it.execute(p))
        else:
            outcomes.append(Outcome("deferred", p.path,
                                    f"非必要({p.category or '未分类'}/{p.severity or '未定级'})，仅记录不自动改"))

    summary_path = _write_review(rdir, proposals, outcomes)
    return {
        "applied": [o.path for o in outcomes if o.status == "applied"],
        "rejected": [(o.path, o.reason) for o in outcomes if o.status == "rejected"],
        "failed": [(o.path, o.reason) for o in outcomes if o.status == "failed"],
        "needs_human": [o.path for o in outcomes if o.status == "needs_human"],
        "deferred": [o.path for o in outcomes if o.status == "deferred"],
        "skipped": [o.path for o in outcomes if o.status == "skipped"],
        "review_actions": review_actions,
        "summary_path": str(summary_path),
        "n_proposals": len(proposals),
    }


def _protected_proposal_action(it: SelfIterator, p: Proposal, reason: str) -> Action:
    cur = ""
    f = it.repo / p.path
    if f.exists():
        cur = f.read_text(encoding="utf-8")
    diff = "".join(difflib.unified_diff(
        cur.splitlines(keepends=True), p.new_code.splitlines(keepends=True),
        fromfile=f"a/{p.path}", tofile=f"b/{p.path}"))
    msg = (f"【受保护·需人工确认】{p.path}\n"
           f"为什么改：{p.rationale or '（未给）'}\n"
           f"改了会怎样：{p.impact or '（未给）'}\n"
           f"（属核心边界：{reason}）")
    return code_review(name=p.path, code=p.new_code,
                       validation={"diff": diff, "zone": "protected"}, message=msg)


# ── 复盘文档循环 ──────────────────────────────────────────────────────────────
def _read_last_review(rdir: Path) -> str:
    if not rdir.exists():
        return ""
    files = sorted(rdir.glob("*.md"))
    return files[-1].read_text(encoding="utf-8") if files else ""


def _recently_applied(rdir: Path, k: int = COOLDOWN_CYCLES) -> set[str]:
    """从最近 k 份复盘里抽出"已落地"的文件路径，用于冷却期判断。"""
    if not rdir.exists():
        return set()
    out: set[str] = set()
    for f in sorted(rdir.glob("*.md"))[-k:]:
        for m in re.finditer(r"已落地\]\s*`([^`]+)`", f.read_text(encoding="utf-8")):
            out.add(m.group(1))
    return out


def _write_review(rdir: Path, proposals: list[Proposal], outcomes: list[Outcome]) -> Path:
    rdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    lines = [f"# 自我迭代复盘 · {ts}", "",
             f"本轮提案 {len(proposals)} 条。结果：", ""]
    for o in outcomes:
        mark = {"applied": "✅ 已落地", "rejected": "↩︎ 打回",
                "failed": "✖ 失败回滚", "needs_human": "🔒 送人工",
                "deferred": "·非必要(仅记录)", "skipped": "⏸ 冷却跳过"}.get(o.status, o.status)
        lines.append(f"- [{mark}] `{o.path}` — {o.reason}")
    if not outcomes:
        lines.append("- （无提案）")
    lines += ["", "## 给下一轮的提示",
              "- 已落地的不必重提；被打回/失败的若要再试，请换思路并确保测试先红后绿。",
              "- 送人工的提案在等用户裁决，未定前不要重复提同一处。"]
    path = rdir / f"{ts}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── 默认聚焦与地图（live 用；测试一般注入）────────────────────────────────────
def _module_map(it: SelfIterator) -> str:
    open_files = [f"🟢 {rel} — {why}" for rel, why in self_model.OPEN.items()]
    prot_files = [f"🔒 {rel} — {why}" for rel, why in self_model.PROTECTED.items()]
    return "OPEN(可自动落地):\n" + "\n".join(open_files) + \
           "\n\nPROTECTED(仅可送审):\n" + "\n".join(prot_files)


def _default_focus(repo: Path, limit: int = 2, max_lines: int = 400) -> dict[str, str]:
    """默认挑几个存在的 OPEN .py 源文件喂给模型（capped，避免 prompt 过大）。"""
    out: dict[str, str] = {}
    for rel in self_model.OPEN:
        if rel.endswith("/") or not rel.endswith(".py"):
            continue
        f = repo / rel
        if not f.exists():
            continue
        lines = f.read_text(encoding="utf-8").splitlines()
        if len(lines) > max_lines:
            continue  # 跳过过大的，留给针对性聚焦
        out[rel] = "\n".join(lines)
        if len(out) >= limit:
            break
    return out


async def _default_model_fn(prompt: str) -> str:
    """单发退路：用主控模型跑一次（惰性引依赖，沙箱/测试不触发）。"""
    import config
    from core.llm import get_client
    client = get_client(timeout=300)   # 反思单发出整文件代码，给长超时（core/llm 单一构建点）
    resp = await client.chat.completions.create(
        model=config.CLAUDE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return resp.choices[0].message.content or ""


_AGENTIC_HINT = """
【你有只读自省工具，先查证再开方】
你运行在一个隔离的只读子 agent 里，可以调用工具核实事实后再提提案：
- list_self_modules / read_self_source(path) / read_symbol(module, name)：
  读自己的真实源码——聚焦源码不够看时，把可疑文件完整读出来再判断；
- search_capability(intent)：查某能力是否已存在（避免提案重复造已有的东西）。
纪律：提案里引用的每一行现状（函数签名、行为、缺陷）都必须来自你真读过的源码，
不许凭聚焦片段外推。查证预算有限（几轮），把它花在你真正要改的文件上。
最后按输出格式给出 JSON 数组（没有真缺陷就输出 []）。
"""


async def _agentic_model_fn(prompt: str) -> str:
    """live 默认（阶段 1 起）：反思跑在隔离的只读子 agent 里（core/spawn）。

    相比单发 _default_model_fn 的升级：反思者能主动 read_self_source 核实、
    search_capability 查重，提案基于真读过的代码而非喂给它的两个片段。
    白名单只读 + 轮次/超时预算 + 上下文隔离由 spawn 机械保证。
    spawn 不可用/空输出时退回单发（反思绝不因基础设施故障而中断）。
    """
    try:
        from core.spawn import spawn
        res = await spawn(prompt + _AGENTIC_HINT, label="自我反思",
                          max_rounds=6, timeout_s=600, contract=False)
        if (res.raw_text or "").strip():
            return res.raw_text
    except Exception:
        pass
    return await _default_model_fn(prompt)
