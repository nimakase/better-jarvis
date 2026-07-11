"""
情报台 API —— 给前端 /intel 页面提供聚合数据与操作。

  GET  /api/intel/overview          信号流 + 热点赛道 + 待审核 + 机会轨 + 投递状态
  POST /api/intel/review/{id}/decide 批准/否决一条行业级审核
"""
from datetime import date

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from intel import signal_library as sl
from core import delivery
from core import availability
from core import intel_cards

router = APIRouter()

# 兜底：确保信号库表已建，避免空库时 overview 报错
try:
    sl.init_db()
except Exception:
    pass


# 信号类型中文名（卡片展示用）
_TLABEL = {
    "pricing": "涨价", "lead_time": "交期", "shortage": "缺货", "oversupply": "过剩",
    "demand_shift": "需求下滑", "capacity": "产能", "layoff": "裁员", "eol_pcn": "EOL",
    "closure": "关厂", "m_and_a": "并购", "write_down": "减值", "policy": "政策",
}


def _sev_tone(sev) -> str:
    sev = sev or 0
    return "red" if sev >= 4 else ("yellow" if sev >= 3 else "dim")


# ────────────────── 情报台卡片 provider（按异常浮现、空则隐藏）──────────────────

async def _card_today_prospects() -> dict:
    try:
        from intel.workflow_defs import load_today_prospects
        data = load_today_prospects()
    except Exception:
        data = None
    if not data or not data.get("items"):
        return {"has_content": False}
    tier_tone = {"A": "red", "B": "yellow", "C": "dim"}
    items = [{
        "text": f'{r.get("company_name", "")}（{r.get("crm_state", "")}{("·" + r.get("hubspot_owner")) if r.get("hubspot_owner") else ""}）',
        "badge": r.get("intent_tier"),
        "tone": tier_tone.get(r.get("intent_tier"), "dim"),
        "url": r.get("website") or None,
    } for r in data["items"][:12]]
    note = f'{data.get("date", "")} · 共 {data.get("count", 0)} 家'
    if data.get("degraded"):
        note += "（未匹配 HubSpot，仅按意向排序；登录后重跑可补匹配）"
    if data.get("signal_stale"):
        age = data.get("signal_age_days")
        note += ("（信号库为空，意向排序仅供参考）" if age is None
                 else f"（信号已 {age} 天未更新，意向排序仅供参考）")
    return {"has_content": True, "urgency": "high", "items": items, "note": note}


async def _card_reviews() -> dict:
    rows = sl.list_review("pending")
    items = [{
        "text": f'{r["sector_id"]} · {r.get("reason", "")}',
        "tone": "yellow",
        "actions": [
            {"label": "提前跟进", "url": f'/api/intel/review/{r["id"]}/decide', "body": {"approved": True}},
            {"label": "忽略", "url": f'/api/intel/review/{r["id"]}/decide', "body": {"approved": False}},
        ],
    } for r in rows]
    return {"has_content": bool(items), "urgency": "high" if items else "low", "items": items}


async def _card_opportunities() -> dict:
    opps = sl.company_pointed_signals(min_implication=3)
    items = [{
        "text": f'{o.get("company_name")} {o.get("note") or ""}'.strip(),
        "badge": _TLABEL.get(o.get("signal_type"), o.get("signal_type")),
        "tone": "red",
        "url": o.get("website") or None,
    } for o in opps]
    return {"has_content": bool(items), "urgency": "high" if items else "low", "items": items}


async def _card_signal_metrics() -> dict:
    grouped = sl.query_report(days=14)
    total = sum(len(v) for v in grouped.values())
    price = sum(len(grouped.get(t, [])) for t in ("pricing", "lead_time", "shortage", "oversupply"))
    event = total - price
    hot = sl.detect_hot_sectors(threshold=3.0)
    metrics = [
        {"label": "活跃信号", "value": total},
        {"label": "价格供需", "value": price},
        {"label": "行业事件", "value": event},
        {"label": "热点赛道", "value": len(hot)},
    ]
    return {"has_content": total > 0, "metrics": metrics}


async def _card_hot_sectors() -> dict:
    hot = sl.detect_hot_sectors(threshold=3.0)[:8]
    items = [{
        "text": h["sector_id"],
        "badge": round(h["total_strength"], 1),
        "tone": "red" if i == 0 else ("yellow" if i < 3 else "dim"),
    } for i, h in enumerate(hot)]
    return {"has_content": bool(items), "items": items}


async def _card_signal_feed() -> dict:
    grouped = sl.query_report(days=14)
    feed = []
    for t, items in grouped.items():
        for it in items:
            feed.append((it.get("severity") or 0, t, it))
    feed.sort(key=lambda x: -x[0])
    rows = [{
        "text": it.get("summary", ""),
        "badge": f'{_TLABEL.get(t, t)}·{it.get("severity") or "-"}',
        "tone": _sev_tone(it.get("severity")),
        "url": it.get("source_url") or None,
    } for sev, t, it in feed[:25]]
    return {"has_content": bool(rows), "items": rows, "note": "近 14 天信号流"}


async def _card_status() -> dict:
    tracks = (delivery.all_status() or {}).get("tracks", {})

    def _track_item(key, label):
        st = (tracks.get(key) or {}).get("status")
        paused = st == "paused"
        return {"text": f'{label}：{"暂停" if paused else "正常"}', "tone": "yellow" if paused else "green"}

    items = [_track_item("report", "日报投递"), _track_item("prospect", "潜客清单投递")]
    # HubSpot 连接状态降级为一颗状态灯（完整登录控件在「设置 · 连接」）
    try:
        from prospecting.login_manager import LoginManager
        hs = LoginManager.get().get_status()
    except Exception:
        hs = "unknown"
    hs_tone = {"ok": "green", "expired": "red", "failed": "red"}.get(hs, "yellow")
    hs_text = {"ok": "HubSpot 已连接", "expired": "HubSpot 登录过期", "failed": "HubSpot 登录失败"}.get(hs, "HubSpot 未连接")
    items.append({"text": hs_text, "tone": hs_tone})
    return {"has_content": True, "items": items}


intel_cards.register_card("today_prospects", "今日潜客名单", "action", _card_today_prospects, order=1)
intel_cards.register_card("reviews", "待人工审核 · 行业级强信号", "action", _card_reviews, order=10)
intel_cards.register_card("opportunities", "机会轨 · 点名公司线索", "action", _card_opportunities, order=20)
intel_cards.register_card("signal_metrics", "信号概览", "monitor", _card_signal_metrics, order=10)
intel_cards.register_card("hot_sectors", "余料热点赛道", "monitor", _card_hot_sectors, order=20)
intel_cards.register_card("signal_feed", "信号流", "status", _card_signal_feed, order=10)
intel_cards.register_card("status", "投递与连接", "status", _card_status, order=90)


@router.get("/api/intel/dashboard")
async def dashboard():
    """情报台数据：分层卡片（空卡已隐藏），前端通用渲染。"""
    data = await intel_cards.build_dashboard()
    data["date"] = date.today().isoformat()
    return JSONResponse(data)


@router.get("/api/intel/overview")
async def overview():
    grouped = sl.query_report(days=14)
    feed = []
    for t, items in grouped.items():
        for it in items:
            feed.append({
                "type": t,
                "severity": it.get("severity"),
                "summary": it.get("summary", ""),
                "source_url": it.get("source_url"),
                "date": it.get("date_collected"),
            })
    feed.sort(key=lambda x: -(x["severity"] or 0))

    hot = sl.detect_hot_sectors(threshold=3.0)
    reviews = sl.list_review("pending")
    opps = sl.company_pointed_signals(min_implication=3)

    return JSONResponse({
        "date": date.today().isoformat(),
        "counts": {t: len(v) for t, v in grouped.items()},
        "feed": feed[:25],
        "hot_sectors": [{"sector": h["sector_id"], "strength": round(h["total_strength"], 1),
                         "signals": len(h.get("signal_ids", []))} for h in hot[:8]],
        "reviews": [{"id": r["id"], "sector": r["sector_id"], "reason": r.get("reason", "")} for r in reviews],
        "opportunities": [{"company_name": o.get("company_name"), "website": o.get("website"),
                           "signal_type": o.get("signal_type"), "note": o.get("note")} for o in opps],
        "delivery": delivery.all_status(),
        "vacation_default": availability.get_vacation_default(),
    })


@router.post("/api/intel/review/{review_id}/decide")
async def decide_review(review_id: int, approved: bool = Body(..., embed=True)):
    """批准=该赛道可提前跟进（标记 approved）；否决=搁置。"""
    sl.decide_review(review_id, approved)
    return JSONResponse({"ok": True, "approved": approved})
