"""
core/spawn.py — 子 agent 抽象（阶段 1 骨架 · 安全相关，自身应受保护）

把一个任务交给独立的后台 controller 实例去跑，主线程只拿回一段结构化结论。
价值的 90% 来自【上下文隔离】：子 agent 烧几十轮工具调用，主线程只收
几百字的契约化结果——主线程的上下文预算被保护，这比省时间值钱。

三大机械护栏（不靠模型自觉）：
  ⑧ 白名单授权：默认只授 read_local + read_external 的工具（由 effects 分级
     推导），更高权限必须显式传 extra_tools。暴露层 + 执行层双重过滤。
     子 agent 天生 interactive=False，BACKGROUND_BLOCKED_TOOLS 叠加生效。
  ⑨ 预算与强制交回：轮次上限（controller.max_tool_rounds）+ 墙钟超时。
     超时不是失败——返回已产出的部分内容（partial=True），绝不白烧。
  契约化结果：要求子 agent 最后输出 JSON {conclusion, evidence,
     uncertainties, unfinished}；解析失败时优雅降级（全文塞进 conclusion）。

结果落盘：workspace/results/<时间>_<label>.json，并按 trial 登记进产物
图书馆（producer="spawn:<label>"，7 天保质期自动过期——中间产物不积灰）。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core import effects

# 结果契约：子 agent 收到的收尾指令
CONTRACT_NOTE = """
【结果契约（务必遵守）】完成任务后，最后单独输出一个 JSON 对象（不要代码围栏）：
{"conclusion": "结论，给主线程直接用的最终答案",
 "evidence": ["支撑结论的关键证据/来源，每条一句"],
 "uncertainties": ["不确定的点"],
 "unfinished": ["没做完的部分（没有则空数组）"]}
除这个 JSON 外，之前的过程性输出都不会被主线程阅读——结论必须自包含。
"""

DEFAULT_MAX_ROUNDS = 8
DEFAULT_TIMEOUT_S = 300.0


@dataclass
class SpawnResult:
    ok: bool
    label: str
    conclusion: str = ""
    evidence: list = field(default_factory=list)
    uncertainties: list = field(default_factory=list)
    unfinished: list = field(default_factory=list)
    partial: bool = False       # 超时/预算耗尽时为 True——内容仍可能有用
    error: str = ""
    result_path: str = ""       # 落盘位置（workspace/results/）
    raw_text: str = ""          # 子 agent 的全部文本输出（不截断；自由文本模式的主产物）

    def brief(self) -> str:
        """给主线程/用户看的一段话摘要。"""
        head = f"[子任务 {self.label}] "
        if not self.ok and not self.conclusion:
            return head + f"失败：{self.error or '无结论'}"
        note = "（部分结果：超预算被强制交回）" if self.partial else ""
        parts = [head + note, self.conclusion]
        if self.uncertainties:
            parts.append("不确定：" + "；".join(str(u) for u in self.uncertainties[:3]))
        if self.unfinished:
            parts.append("未完成：" + "；".join(str(u) for u in self.unfinished[:3]))
        return "\n".join(p for p in parts if p)


def _subagent_model(purpose: str = "default") -> str:
    """子 agent 用的模型：查 core/model_routing 按用途路由（任务 #20）。
    purpose="default" 等价于此前唯一支持的 env JARVIS_SUBAGENT_MODEL 覆盖——
    未传 purpose 时行为完全不变。留空 = 跟主线程同一个 config.CLAUDE_MODEL。"""
    from core import model_routing
    return model_routing.model_for(purpose)


# ── 白名单推导（⑧）──────────────────────────────────────────────────────────

def default_whitelist() -> set[str]:
    """默认授权集：已注册工具中 effect ≤ read_external 的（纯只读）。"""
    from core import registry
    out = set()
    for s in registry.iter_specs():
        if effects.rank(effects.effect_of(s.name)) <= effects.rank(effects.READ_EXTERNAL):
            out.add(s.name)
    return out


def build_whitelist(extra_tools: Optional[list] = None) -> set[str]:
    """默认只读集 ∪ 显式授予的更高权限工具（须逐个点名，不接受通配）。"""
    wl = default_whitelist()
    for t in extra_tools or []:
        wl.add(t)
    return wl


# ── 结果解析 ──────────────────────────────────────────────────────────────────

def parse_contract(text: str) -> Optional[dict]:
    """从子 agent 的输出里捞结果契约 JSON（最后一个含 conclusion 的顶层对象）。
    解析统一走 core.json_salvage（2026-07-22 收敛，此前这里自写了一份括号深度算法）。
    永不抛错；捞不到返回 None。"""
    from core.json_salvage import salvage_json_objects
    for obj in reversed(salvage_json_objects(text or "")):
        if "conclusion" in obj:
            return obj
    return None


# ── 落盘 ──────────────────────────────────────────────────────────────────────

def _results_dir() -> Path:
    d = Path(__file__).resolve().parent.parent / "workspace" / "results"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _persist(res: SpawnResult) -> None:
    """结果落 workspace/results/ 并按 trial 登记图书馆。失败不阻断。"""
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        safe = re.sub(r"[^\w一-鿿\-]+", "_", res.label)[:40] or "task"
        p = _results_dir() / f"{ts}_{safe}.json"
        p.write_text(json.dumps({
            "label": res.label, "ok": res.ok, "partial": res.partial,
            "conclusion": res.conclusion, "evidence": res.evidence,
            "uncertainties": res.uncertainties, "unfinished": res.unfinished,
            "error": res.error, "created_at": ts,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        res.result_path = str(p)
        from core import artifacts
        artifacts.register(p, producer=f"spawn:{safe}", kind="其他",
                           label=f"子任务_{safe}", state="trial")
    except Exception:
        pass


# ── 主入口 ────────────────────────────────────────────────────────────────────

async def spawn(task: str, *, label: str = "", extra_tools: Optional[list] = None,
                max_rounds: int = DEFAULT_MAX_ROUNDS,
                timeout_s: float = DEFAULT_TIMEOUT_S,
                contract: bool = True,
                purpose: str = "default",
                _controller=None) -> SpawnResult:
    """把 task 交给一个隔离的子 agent，拿回契约化结果。

    - extra_tools：默认只读白名单之外要额外授予的工具名（显式逐个点名）。
    - contract：True（默认）注入结果契约并解析 JSON；False = 自由文本模式——
      调用方要的是子 agent 的完整原始输出（如 self_review 的提案数组），
      从 raw_text 取，不注入契约、不按契约解析。
    - purpose：按 core/model_routing 选子 agent 模型（任务 #20），默认 "default"
      与此前唯一行为等价。代码审查类子任务可传 "code_review" 之类的用途标签。
    - _controller：测试注入口（生产不传，内部按白名单新建后台 controller）。
    """
    label = label or (task[:20] + "…" if len(task) > 20 else task)

    if _controller is None:
        from core.controller import JarvisController
        ctl = JarvisController(
            interactive=False,
            allowed_tools=build_whitelist(extra_tools),
            max_tool_rounds=max_rounds,
            model=_subagent_model(purpose),
        )
    else:
        ctl = _controller

    full_text = ""

    prompt = task.strip() + ("\n" + CONTRACT_NOTE if contract else "")

    async def _run():
        nonlocal full_text
        async for ev in ctl.chat(prompt):
            if ev.get("type") == "text":
                full_text += ev.get("text", "")

    partial = False
    error = ""
    try:
        await asyncio.wait_for(_run(), timeout=timeout_s)
    except asyncio.TimeoutError:
        partial = True
        error = f"超时（{timeout_s:.0f}s），交回已产出的部分内容"
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"

    if not contract:
        res = SpawnResult(ok=bool(full_text.strip()) and not error, label=label,
                          partial=partial, error=error,
                          conclusion=full_text.strip()[:2000], raw_text=full_text)
        _persist(res)
        return res

    obj = parse_contract(full_text)
    if obj is not None:
        res = SpawnResult(
            ok=not error, label=label, partial=partial, error=error,
            conclusion=str(obj.get("conclusion") or ""),
            evidence=list(obj.get("evidence") or []),
            uncertainties=list(obj.get("uncertainties") or []),
            unfinished=list(obj.get("unfinished") or []),
            raw_text=full_text,
        )
    elif full_text.strip():
        # 降级：没按契约输出，但有内容——全文当结论，标注不确定
        res = SpawnResult(ok=not error, label=label, partial=partial, error=error,
                          conclusion=full_text.strip()[:2000], raw_text=full_text,
                          uncertainties=["子 agent 未按结果契约输出，以上为原文"])
    else:
        res = SpawnResult(ok=False, label=label, partial=partial,
                          error=error or "子 agent 无输出")

    _persist(res)
    return res


async def spawn_many(tasks: list[dict], concurrency: int = 3) -> list[SpawnResult]:
    """并发跑多个子任务（扇出调研形态）。tasks 每项是 spawn 的 kwargs（含 task）。"""
    sem = asyncio.Semaphore(concurrency)

    async def _one(kw: dict) -> SpawnResult:
        async with sem:
            return await spawn(**kw)

    return list(await asyncio.gather(*(_one(kw) for kw in tasks)))
