"""
工作流运行器（Workflow runner）—— 把"多步骤流水线"做成 jarvis 的一等能力。

一个工作流 = 有序的 Step 列表，步骤间用共享 context(dict) 传递数据。
每步是一个可调用对象（同步或 async），接收 context、返回结果（写回 ctx[step.name]）。

每步可声明错误策略：
  - abort   ：失败即整流程失败（默认）
  - skip    ：失败则跳过该步，继续
  - degrade ：失败则置一个降级标志（如 hubspot 匹配挂了），继续——
              这就是 intel「matcher 失效仍出清单」的通用化。
并支持 retries 重试。

run_workflow 返回 WorkflowRun（含每步状态 + 最终状态 + context），
天然是一份可观测的运行记录。

新增层，纯标准库；不改 controller/scheduler 现有路径。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

StepFn = Callable[[dict], Union[Any, Awaitable[Any]]]


class StopWorkflow(Exception):
    """步骤主动要求【干净收尾】：不是失败，后续步骤不再执行。

    用途：流程走到某处发现"今天没有可做的事 / 前置条件不满足且已妥善善后"
    （如潜客树全部跑完、HubSpot 未登录但本批已存盘待续跑）。
    与 abort 的区别：run.status 仍是 ok，不会被当成故障告警；
    与 skip 的区别：skip 只跳过一步，这里是整条流程到此为止。
    """

    def __init__(self, reason: str = ""):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Step:
    name: str
    fn: StepFn
    retries: int = 0
    on_error: str = "abort"              # abort | skip | degrade
    degrade_flag: Optional[str] = None   # on_error=degrade 时，置 ctx[flag]=True


@dataclass
class StepResult:
    name: str
    status: str           # ok | skipped | degraded | failed
    attempts: int
    error: Optional[str] = None
    seconds: float = 0.0


@dataclass
class WorkflowRun:
    name: str
    status: str                                   # ok | failed
    steps: list[StepResult] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    failed_at: Optional[str] = None
    stopped_at: Optional[str] = None              # StopWorkflow 干净收尾发生在哪一步
    seconds: float = 0.0
    trace_id: str = ""                            # 任务 #17：这条 run 期间所有工具调用
                                                   # 的 telemetry 记录都能靠这个 id 串起来

    def summary(self) -> str:
        marks = {"ok": "✓", "skipped": "−", "degraded": "≈", "failed": "✗", "stopped": "◼"}
        lines = [f"[{self.status}] workflow {self.name} ({self.seconds:.2f}s)"]
        for s in self.steps:
            tail = f" — {s.error}" if s.error else ""
            lines.append(f"  {marks.get(s.status, '?')} {s.name} ({s.attempts}x){tail}")
        return "\n".join(lines)


async def _call(fn: StepFn, ctx: dict) -> Any:
    out = fn(ctx)
    if asyncio.iscoroutine(out):
        out = await out
    return out


async def run_workflow(name: str, steps: list[Step], context: Optional[dict] = None,
                       logger=None, trace_id: Optional[str] = None) -> WorkflowRun:
    """trace_id（任务 #17）：不传则若已在某个 trace 里（嵌套调用）复用外层 trace，
    否则铸一个新的（wf_<name>_...）。整条 run 期间 core.trace.get() 都返回它，
    这条 run 里所有 telemetry.record（工具调用）自动带上，事后可用
    telemetry.calls_by_trace(run.trace_id) 串起这次 run 的全部工具调用。"""
    from core import trace as _trace

    ctx = context if context is not None else {}
    run = WorkflowRun(name=name, status="ok", context=ctx)
    effective_trace = trace_id if trace_id is not None else (_trace.get() or _trace.new_id(f"wf_{name}"))
    run.trace_id = effective_trace
    t0 = time.time()

    with _trace.scope(effective_trace):
        for step in steps:
            s0 = time.time()
            attempts = 0
            last_err: Optional[Exception] = None
            while attempts <= step.retries:
                attempts += 1
                try:
                    out = await _call(step.fn, ctx)
                    if out is not None:
                        ctx[step.name] = out
                    run.steps.append(StepResult(step.name, "ok", attempts, seconds=time.time() - s0))
                    last_err = None
                    break
                except StopWorkflow as stop:
                    # 干净收尾：不算失败，整条流程到此为止（不重试、不走错误策略）
                    run.steps.append(StepResult(step.name, "stopped", attempts,
                                                stop.reason or None, time.time() - s0))
                    run.stopped_at = step.name
                    run.seconds = time.time() - t0
                    return run
                except Exception as exc:
                    last_err = exc
                    if logger:
                        logger.warning("workflow %s step %s attempt %s failed: %s", name, step.name, attempts, exc)
                    if attempts <= step.retries:
                        continue
            if last_err is None:
                continue

            # 重试耗尽 → 按错误策略处理
            secs = time.time() - s0
            if step.on_error == "skip":
                run.steps.append(StepResult(step.name, "skipped", attempts, str(last_err), secs))
                continue
            if step.on_error == "degrade":
                if step.degrade_flag:
                    ctx[step.degrade_flag] = True
                run.steps.append(StepResult(step.name, "degraded", attempts, str(last_err), secs))
                continue
            # abort
            run.steps.append(StepResult(step.name, "failed", attempts, str(last_err), secs))
            run.status = "failed"
            run.failed_at = step.name
            run.seconds = time.time() - t0
            return run

        run.seconds = time.time() - t0
        return run
