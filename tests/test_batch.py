"""tests/test_batch.py — core/batch.py 可恢复批处理原语 纯逻辑单测。
跑:python -m tests.test_batch"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import batch  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ── 基本成功路径 ────────────────────────────────────────────────
res = batch.run_batch(["a", "b", "c"], key_fn=str, worker_fn=lambda x: x.upper())
check([k for k, _ in res.succeeded] == ["a", "b", "c"], "全部处理")
check([v for _, v in res.succeeded] == ["A", "B", "C"], "worker_fn 返回值被保留")
check(res.failed == [], "无失败")
check(res.skipped_already_done == 0, "无跳过")
check(res.processed_count == 3, "processed_count = 成功+失败")

# ── 断点续跑：done_keys 命中的条目不进 worker_fn ──────────────────
calls = []
def _worker(x):
    calls.append(x)
    return x.upper()

res2 = batch.run_batch(["a", "b", "c"], key_fn=str, worker_fn=_worker, done_keys={"a", "c"})
check(calls == ["b"], f"只处理未完成的, got {calls}")
check(res2.skipped_already_done == 2, "两条已完成被跳过计数")
check([k for k, _ in res2.succeeded] == ["b"], "succeeded 只含新处理的")

# ── 单条失败不拖垮整批（默认 on_item_error=continue）─────────────
def _flaky(x):
    if x == "b":
        raise ValueError("boom")
    return x.upper()

res3 = batch.run_batch(["a", "b", "c"], key_fn=str, worker_fn=_flaky)
check([k for k, _ in res3.succeeded] == ["a", "c"], "b 失败不影响 a/c 继续处理")
check(len(res3.failed) == 1 and res3.failed[0].key == "b", "b 记进 failed")
check("ValueError" in res3.failed[0].error and "boom" in res3.failed[0].error, "错误信息含类型+原因")
check(res3.aborted_at is None, "continue 模式不设 aborted_at")

# ── on_item_error=abort：一条失败立刻停止 ─────────────────────────
res4 = batch.run_batch(["a", "b", "c"], key_fn=str, worker_fn=_flaky, on_item_error="abort")
check([k for k, _ in res4.succeeded] == ["a"], "abort 模式:b 之前的照常跑")
check(len(res4.failed) == 1 and res4.failed[0].key == "b", "b 记进 failed")
check(res4.aborted_at == "b", "aborted_at 记录中止点")
check(res4.processed_count == 2, "abort 后 c 完全没跑,processed_count 只算到 b")

# ── limit：只限【未完成】条目数,已完成的不占名额 ─────────────────
res5 = batch.run_batch(["a", "b", "c", "d"], key_fn=str, worker_fn=lambda x: x,
                       done_keys={"a"}, limit=2)
check([k for k, _ in res5.succeeded] == ["b", "c"], f"limit=2 只处理前两个未完成的, got {res5.succeeded}")
check(res5.skipped_already_done == 1, "已完成的 1 条不占 limit 名额")

# ── snapshot_fn：失败时附诊断快照 ─────────────────────────────────
def _flaky2(x):
    if x == "b":
        raise ValueError("boom")
    return x

res6 = batch.run_batch(["a", "b"], key_fn=str, worker_fn=_flaky2,
                       snapshot_fn=lambda item, exc: {"raw_input": item, "exc_type": type(exc).__name__})
check(res6.failed[0].snapshot == {"raw_input": "b", "exc_type": "ValueError"}, "快照记录了原始输入")

# snapshot_fn 自己炸了也不能让整条失败记录丢掉
res7 = batch.run_batch(["b"], key_fn=str, worker_fn=_flaky2,
                       snapshot_fn=lambda item, exc: 1 / 0)
check(len(res7.failed) == 1 and res7.failed[0].snapshot is None, "snapshot_fn 自身出错被吞掉,不影响失败记录")

# ── summary 可读性 ──────────────────────────────────────────────
check("2 成功" in res3.summary() and "1 失败" in res3.summary(), "summary 含成功/失败计数")
check("中止于 b" in res4.summary(), "abort 模式 summary 含中止点")

# ── 空输入 / 全部已完成 ──────────────────────────────────────────
res8 = batch.run_batch([], key_fn=str, worker_fn=lambda x: x)
check(res8.processed_count == 0 and res8.skipped_already_done == 0, "空输入安全")

res9 = batch.run_batch(["a", "b"], key_fn=str, worker_fn=lambda x: 1 / 0, done_keys={"a", "b"})
check(res9.processed_count == 0 and res9.skipped_already_done == 2, "全部已完成时 worker_fn 完全不被调用")

print("✅ test_batch 全部通过")
