"""
工作流注册表 + 运行记录

把 core/workflow.py 的引擎提升为一等的、可发现 / 可触发 / 可观测能力——和项目里
tool / skill / report / intel-card 注册表同构的扩展模式。

每个工作流声明：
  id, name, description,
  builder            —— 运行时组装步骤（注入浏览器/agent 等运行时依赖）。可返回：
                        · list[wf.Step]，或
                        · {"steps": [...], "context": {...}?, "cleanup": callable?}
  confirm            —— 跑前是否需要用户确认（有可见副作用的设 True，如开浏览器/连 CRM）
  needs              —— 前置条件的人话说明（如"需先登录 HubSpot"）

run(id) → 组装步骤 → run_workflow → 落盘一条运行摘要（步骤状态/降级/耗时，天然可观测）
→ 返回结果与 context。运行记录存 DATA_DIR/workflows/runs.json（最多 50 条）。
产出的落库（如"今日名单"）由各工作流自己的 output 步骤负责，注册表保持通用。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from core import workflow as wf

logger = logging.getLogger("jarvis.workflow_registry")

try:
    import config
    _DIR = config.DATA_DIR / "workflows"
except Exception:
    _DIR = Path(__file__).resolve().parent / "workflows"
_RUNS = _DIR / "runs.json"

# id -> {id, name, description, builder, confirm, needs}
_WORKFLOWS: dict[str, dict] = {}


def register_workflow(wf_id: str, name: str, description: str,
                      builder: Callable, *, confirm: bool = True, needs: str = "",
                      dispatch: str = "sync") -> None:
    """dispatch: "sync"（同步跑完再返回，短流程用）| "detach"（派发即返回，
    长耗时流程用——主对话不被扣押，结果经 delivery 推送交付）。"""
    _WORKFLOWS[wf_id] = {
        "id": wf_id, "name": name, "description": description,
        "builder": builder, "confirm": confirm, "needs": needs,
        "dispatch": dispatch if dispatch in ("sync", "detach") else "sync",
    }


def get(wf_id: str) -> Optional[dict]:
    return _WORKFLOWS.get(wf_id)


def list_workflows() -> list[dict]:
    return [{k: v[k] for k in ("id", "name", "description", "confirm", "needs", "dispatch")}
            for v in _WORKFLOWS.values()]


def catalog_block() -> str:
    """注入 system prompt 的工作流目录。空则返回空串。"""
    if not _WORKFLOWS:
        return ""
    lines = ["【可运行的工作流】（多步骤任务；仅在用户明确要求运行或定时触发时用 run_workflow）"]
    for v in _WORKFLOWS.values():
        needs = f" 前置：{v['needs']}" if v["needs"] else ""
        conf = "（有副作用，跑前先告知并确认）" if v["confirm"] else ""
        lines.append(f"- {v['id']}（{v['name']}）：{v['description']}{needs}{conf}")
    return "\n".join(lines)


# ── 运行记录（可观测）──────────────────────────────────────────────────────────

def _load_runs() -> list:
    if _RUNS.exists():
        try:
            return json.loads(_RUNS.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_runs(runs: list) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    _RUNS.write_text(json.dumps(runs[:50], ensure_ascii=False, indent=2), encoding="utf-8")


def recent_runs(limit: int = 10) -> list:
    return _load_runs()[:limit]


def last_run(wf_id: str) -> Optional[dict]:
    for r in _load_runs():
        if r.get("id") == wf_id:
            return r
    return None


def _record(rec: dict) -> None:
    runs = _load_runs()
    runs.insert(0, rec)
    _save_runs(runs)


# ── 运行 ────────────────────────────────────────────────────────────────────

async def run(wf_id: str, **kwargs) -> dict:
    """组装并运行一个工作流，落盘运行摘要，返回 {ok, run, context?/error}。"""
    spec = _WORKFLOWS.get(wf_id)
    if not spec:
        return {"ok": False, "error": f"未知工作流：{wf_id}"}

    cleanup = None
    now = datetime.now().isoformat(timespec="seconds")
    try:
        built = spec["builder"](**kwargs)
        if asyncio.iscoroutine(built):
            built = await built
        if isinstance(built, dict):
            steps = built.get("steps") or []
            ctx = built.get("context") or {}
            cleanup = built.get("cleanup")
        else:
            steps, ctx = built, {}
        wrun = await wf.run_workflow(spec["name"], steps, ctx)
    except Exception as e:
        rec = {"id": wf_id, "name": spec["name"], "status": "failed",
               "at": now, "error": str(e), "steps": []}
        _record(rec)
        return {"ok": False, "error": str(e), "run": rec}
    finally:
        if cleanup:
            try:
                cleanup()
            except Exception as e:
                # cleanup 通常关浏览器/CRM 会话，吞掉会泄漏进程/资源
                logger.warning("工作流 %s 收尾 cleanup 失败（可能泄漏浏览器/会话）：%s",
                               spec.get("name", "?"), e)

    rec = {
        "id": wf_id, "name": spec["name"], "status": wrun.status,
        "failed_at": wrun.failed_at,
        "stopped_at": getattr(wrun, "stopped_at", None),   # StopWorkflow 干净收尾发生处
        "seconds": round(wrun.seconds, 2), "at": now,
        "steps": [{"name": s.name, "status": s.status, "error": s.error} for s in wrun.steps],
        "summary": wrun.summary(),
    }
    _record(rec)
    return {"ok": wrun.status == "ok", "run": rec, "context": wrun.context}
