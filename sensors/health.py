"""
sensors/health.py — 自体健康感官（㉓ · 放权前置：他得知道自己是死是活）

职责边界：只管【我自己好不好】——
  - 工作流健康：最近运行里谁失败了（workflow_registry 运行记录）
  - 工具故障：近 2 天失败 ≥2 次的工具（telemetry 遥测）
  - 自我漂移：工作树/外部提交（core/drift）

「你的事务时限」（证件/合同/订阅到期等）不属于健康——归内置日历（时间真源，
calendar.register_source），每轮经「近期日程」块注入，勿在此重复。
全部 best-effort，失败静默缺席。
"""
from __future__ import annotations

from core import world_state


def collect_workflow_health() -> dict:
    try:
        from core import workflow_registry as wr
        runs = wr.recent_runs(limit=8)
        if not runs:
            return {}
        failed = [r for r in runs if r.get("status") == "failed"]
        if not failed:
            return {"工作流": f"近 {len(runs)} 次运行全部正常"}
        names = "、".join(sorted({str(r.get("name") or r.get("id")) for r in failed}))
        return {"工作流": f"近 {len(runs)} 次中 {len(failed)} 次失败（{names}）——可主动告知用户或排查"}
    except Exception:
        return {}


def collect_tool_health() -> dict:
    try:
        from core import telemetry
        troubled = [s for s in telemetry.stats(days=2) if (s.get("failures") or 0) >= 2]
        if not troubled:
            return {}
        parts = [f"{s['tool']}(失败{s['failures']}/{s['n']})" for s in troubled[:3]]
        return {"工具故障": "，".join(parts)}
    except Exception:
        return {}



# 注：证件/文档到期【不在这里】。时限类事件的归属地是内置日历（时间真源，
# connectors/calendar_providers 已注册 credentials_expiry / documents_expiry，
# 每轮经「近期日程」块注入）。健康感官只管「我自己好不好」；
# 未来新的时限类事件（保单/合同/订阅）一律走 calendar.register_source，别加在这。
# ——2026-07-22 曾在此重复实现过一版 collect_expiry，Ned 问「为什么这样处理」后
# 查实日历早已覆盖，删除。教训：加感官前先 search_capability。


def collect_delivery_health() -> dict:
    """近 3 天有没有推送/交付没送达（㉔ 回执）。让「我以为发了其实没发」可见。"""
    try:
        from core import telemetry
        n = telemetry.recent_delivery_failures(days=3)
        if n:
            return {"投递": f"近 3 天 {n} 次推送未送达——可能收件人没配好或渠道故障，建议查"}
        return {}
    except Exception:
        return {}


def collect_drift() -> dict:
    try:
        from core import drift
        return drift.provider()
    except Exception:
        return {}


# ── 注册（健康类变化慢，TTL 放长）────────────────────────────────────────────

world_state.register_provider("工作流健康", collect_workflow_health, ttl_s=300)
world_state.register_provider("工具健康", collect_tool_health, ttl_s=300)
world_state.register_provider("投递健康", collect_delivery_health, ttl_s=300)
world_state.register_provider("自我漂移", collect_drift, ttl_s=600)
