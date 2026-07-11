"""
工作流 REST —— 列出 / 运行 / 最近运行记录（供 UI 触发与观测）。

运行有副作用的工作流（开浏览器/连 HubSpot）是用户的显式动作（点按钮），
因此 UI 触发等同于"明确要求"；不会有意外触发。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core import workflow_registry as wr

router = APIRouter()


@router.get("/api/workflows")
async def api_list_workflows():
    return JSONResponse({"workflows": wr.list_workflows(), "recent": wr.recent_runs(10)})


def _json_safe(ctx: dict) -> dict:
    """从工作流 context 里挑出可 JSON 序列化的顶层条目（运行时对象/浏览器等丢弃），
    供 UI 显示步骤产出（如采集入库条数）。"""
    import json as _json
    safe = {}
    for k, v in (ctx or {}).items():
        try:
            _json.dumps(v)
            safe[k] = v
        except Exception:
            continue
    return safe


@router.post("/api/workflows/{wf_id}/run")
async def api_run_workflow(wf_id: str):
    res = await wr.run(wf_id)
    # context 可能含运行时对象（浏览器等）不可序列化——只回可安全序列化的部分
    out = {k: v for k, v in res.items() if k != "context"}
    out["data"] = _json_safe(res.get("context") or {})
    return JSONResponse(out, status_code=200 if out.get("ok") or out.get("run") else 400)
