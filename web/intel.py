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

router = APIRouter()

# 兜底：确保信号库表已建，避免空库时 overview 报错
try:
    sl.init_db()
except Exception:
    pass


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
