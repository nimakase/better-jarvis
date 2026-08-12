"""
core/batch.py — 可恢复批处理原语（Batch resumable processing）

给"要挨个处理一批条目、单条可能失败、失败不该拖垮整批、崩了要能从断点续跑"这类场景
一个通用外壳——这正是 core/workflow.py 的 Step 粒度覆盖不到的那一层：workflow.py
的 Step 是"一步"（读表/算段/写 view 各一步），但很多 skill 真正的痛点在"一步内部
要循环处理成百上千个条目"，每条都可能单独失败、需要单独重试、需要单独记住"处理过了"。

设计对齐 core/workflow.py 的哲学：纯函数 + 小状态类，可确定性单测；调用方通过依赖
注入提供 worker_fn（真正干活的函数）与 done_keys（哪些已经处理过，通常来自某个
持久化 store），本模块不关心存储介质、不做持久化——"记住做过了"是调用方的事
（通常在拿到 succeeded 后自己写 store），这样断点续跑的语义完全由调用方的 store
决定，本模块只负责"过滤 + 单条容错 + 结果归类"。

典型改造对象：prospecting/view_manager.py 的 run_view_cycle 里手写的
`done = set(outreach_store.all_states().keys())` + 逐条 try/except + `[:limit]`
三件套——那是这个原语的一次性重复实现，抽出来后任何"要处理一批东西"的 skill都能复用，
不用每个都重新发明断点续跑 + 单条容错这两件事。

用法：
    result = run_batch(
        items=accounts,
        key_fn=outreach_store._norm,
        worker_fn=lambda acct: process_one_account(acct),
        done_keys=set(outreach_store.all_states().keys()),
        limit=50,
        snapshot_fn=lambda acct, exc: {"account": acct},  # 可选：失败时留一份诊断快照
    )
    for key, value in result.succeeded:
        ...  # 调用方自己决定怎么处置成功结果（写 store / 攒 proposals 等）
    for item_err in result.failed:
        errors.append({"account": item_err.key, "error": item_err.error})
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")


@dataclass
class BatchItemError:
    key: str
    error: str
    snapshot: Any = None   # 诊断快照：调用方提供的"当时收到了什么"，不只是一行异常文本
                            # （呼应"分步错误日志主动暴露问题"——留住触发失败的原始输入，
                            # 而不只是异常类型名，下次调试才有素材）


@dataclass
class BatchResult:
    succeeded: list[tuple[str, Any]] = field(default_factory=list)
    failed: list[BatchItemError] = field(default_factory=list)
    skipped_already_done: int = 0
    aborted_at: Optional[str] = None   # on_item_error="abort" 时，记录在哪个 key 上中止

    @property
    def processed_count(self) -> int:
        """本次实际跑过 worker_fn 的条目数（成功+失败，不含跳过的已完成项）。"""
        return len(self.succeeded) + len(self.failed)

    def summary(self) -> str:
        parts = [f"{len(self.succeeded)} 成功", f"{len(self.failed)} 失败",
                 f"{self.skipped_already_done} 已完成跳过"]
        if self.aborted_at:
            parts.append(f"中止于 {self.aborted_at}")
        return "、".join(parts)


def run_batch(
    items: list[T],
    key_fn: Callable[[T], str],
    worker_fn: Callable[[T], Any],
    *,
    done_keys: Optional[set] = None,
    on_item_error: str = "continue",
    limit: Optional[int] = None,
    snapshot_fn: Optional[Callable[[T, Exception], Any]] = None,
    logger=None,
) -> BatchResult:
    """对 items 逐条跑 worker_fn，具备断点续跑（done_keys）与单条容错（on_item_error）。

    - done_keys：已完成的 key 集合，key_fn(item) 命中则跳过（不调用 worker_fn，也不占
      limit 名额）。调用方决定这个集合从哪来（通常是某个持久化 store 的当前状态）——
      本函数不做持久化，只做过滤；真正"记住做过了"要靠调用方在拿到 succeeded 后自己写 store。
    - on_item_error："continue"（默认，单条失败记进 failed、继续处理下一条，模拟单户抖动
      不拖垮整批）| "abort"（一条失败立刻停止，不再处理后续条目——用于"这批条目之间有
      顺序依赖，前面错了后面没意义再跑"的场景）。
    - limit：本次最多处理多少条【未完成的】条目（用于分批/冷启动），不影响 skip 判定
      （已完成的不占 limit 名额，跟 view_manager 原有语义一致）。
    - snapshot_fn(item, exc)：可选，单条失败时用它生成一份诊断快照（比如某次外部调用的
      原始返回体），存进 BatchItemError.snapshot；本身抛错会被吞掉、snapshot 记为 None，
      不能让"记录诊断信息"这件事本身又造成一次失败。
    """
    done = done_keys or set()
    result = BatchResult()

    todo = [it for it in items if key_fn(it) not in done]
    result.skipped_already_done = len(items) - len(todo)
    if limit:
        todo = todo[:limit]

    for item in todo:
        key = key_fn(item)
        try:
            value = worker_fn(item)
            result.succeeded.append((key, value))
        except Exception as exc:  # noqa: BLE001 — 单条失败要被兜住，不能让一户拖垮整批
            snapshot = None
            if snapshot_fn:
                try:
                    snapshot = snapshot_fn(item, exc)
                except Exception:
                    snapshot = None
            result.failed.append(
                BatchItemError(key=key, error=f"{type(exc).__name__}: {exc}", snapshot=snapshot)
            )
            if logger:
                logger.warning("batch item %s failed: %s", key, exc)
            if on_item_error == "abort":
                result.aborted_at = key
                break

    return result
