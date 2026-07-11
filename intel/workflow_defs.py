"""
注册 jarvis 工作流（导入即注册，由 main 在启动时加载）。

当前注册：
  - prospect_daily（今日潜客名单）：选节点 → 联网生成候选 → 接意向信号 →
    HubSpot 匹配富化 → 排序 → 出富 xlsx 并把"今日名单"落库喂情报台卡。
  - signal_collection（采集今日信号）：独立实例跑采集提示词 → 联网广扫 →
    解析结构化信号 JSON → 入库（喂情报台看板与市场情报日报）。

报告类产出由报告框架（core/reports + generate_report）承担，不在此重复成工作流。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from core import workflow as wf
from core import workflow_registry as wr

_TOP_FIELDS = ("rank", "company_name", "intent_tier", "crm_state", "weight",
               "hubspot_owner", "surplus_signals", "website")


def _store_dir() -> Path:
    import config
    d = config.DATA_DIR / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store_today_prospects(records: list, res: dict) -> None:
    """把本次产出的名单落成结构化 JSON，供情报台「今日名单」卡读取。"""
    items = [{k: r.get(k) for k in _TOP_FIELDS} for r in (records or [])[:50]]
    payload = {
        "date": date.today().isoformat(),
        "count": len(records or []),
        "degraded": bool((res or {}).get("degraded")),
        "signal_stale": bool((res or {}).get("signal_stale")),
        "signal_age_days": (res or {}).get("signal_age_days"),
        "xlsx_path": (res or {}).get("path"),
        "items": items,
    }
    (_store_dir() / "today_prospects.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_today_prospects() -> dict | None:
    p = _store_dir() / "today_prospects.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _build_prospect_daily():
    """运行时组装潜客工作流（注入浏览器/agent 等运行时依赖）。"""
    import config
    from intel import signal_library as sl
    from prospecting import workflows as pw

    # 潜客树：jarvis 拥有自己的副本（data/prospect_tree.json）并就地推进（mark_done 写回）。
    # 解析顺序：env JARVIS_PROSPECT_TREE（如需指向别处）→ 仓库 data/ 副本 → DATA_DIR。
    owned = config.BASE_DIR / "data" / "prospect_tree.json"
    candidates = []
    if getattr(config, "PROSPECT_TREE_PATH", ""):
        candidates.append(Path(config.PROSPECT_TREE_PATH))
    candidates.append(owned)
    candidates.append(config.DATA_DIR / "prospect_tree.json")
    tree_path = next((p for p in candidates if p.exists()), owned)
    db_path = sl.DEFAULT_DB
    prompt_path = Path(__file__).resolve().parent / "prospect_generation_prompt.md"

    generate_fn = pw.make_llm_generate_fn(prompt_path)
    preflight_fn, match_fn, close = pw.make_hubspot_runtime()
    base_output = pw.make_output_fn(config.DATA_DIR / "prospects", "prospect", "今日潜客名单")

    def output_fn(ctx):
        res = base_output(ctx) or {}
        _store_today_prospects(ctx.get("enrich") or [], res)
        return res

    steps = pw.build_prospect_workflow(
        tree_path=tree_path, db_path=db_path,
        generate_fn=generate_fn, preflight_fn=preflight_fn,
        match_fn=match_fn, output_fn=output_fn,
    )
    return {"steps": steps, "context": {}, "cleanup": close}


wr.register_workflow(
    "prospect_daily", "今日潜客名单",
    "选潜客树节点 → 联网生成候选 → 接意向信号 → HubSpot 匹配富化 → 排序 → 出富 xlsx 并落「今日名单」",
    _build_prospect_daily, confirm=True,
    needs="需 prospect_tree.json 与已登录 HubSpot；未登录则自动降级为仅按意向排序的名单（仍可出）",
)


# ── 信号采集工作流（替代"在对话里让模型自己搜+乱建工具"的不可靠路径）──────────────
def _build_signal_collection():
    """采集今日全行业信号：独立 controller 跑固定采集提示词 → 解析 JSON → 入库。

    用工作流把采集框死：提示词要求"只输出 JSON 数组"，独立实例靠 :online 联网检索，
    全程确定性、不发挥、不自建工具。产出入库后即喂情报台看板与市场情报日报。
    """
    from intel import signal_library as sl

    prompt_path = Path(__file__).resolve().parent / "signal_collection_prompt.md"
    template = prompt_path.read_text(encoding="utf-8")

    async def collect(ctx: dict) -> list:
        from core.controller import JarvisController
        sc = JarvisController(interactive=False)  # 后台实例：不写用户档案/不自建工具
        text = ""
        async for ev in sc.chat(template):
            # 只取正文 text；模型若误调工具，其进度走 type=="tool"，这里忽略
            if isinstance(ev, dict) and ev.get("type") == "text":
                text += ev["text"]
        # 从输出里抽出 JSON 数组（容忍模型在数组前后带少量说明文字）
        i, j = text.find("["), text.rfind("]")
        if i >= 0 and j > i:
            try:
                data = json.loads(text[i:j + 1])
                return data if isinstance(data, list) else []
            except Exception:
                return []
        return []

    def ingest(ctx: dict) -> dict:
        signals = ctx.get("collect") or []
        if not signals:
            return {"inserted": 0, "merged": 0, "skipped": 0, "note": "采集未产出有效信号"}
        return sl.ingest_signals(signals)

    return [
        wf.Step("collect", collect),
        wf.Step("ingest", ingest),
    ]


wr.register_workflow(
    "signal_collection", "采集今日信号",
    "独立实例联网广扫电子元件/整机制造行业 → 结构化信号 JSON → 入库（喂情报台看板与市场情报日报）",
    _build_signal_collection, confirm=False,
    needs="依赖模型联网检索（:online）；约耗 1–3 分钟，产出当日信号入库",
)
