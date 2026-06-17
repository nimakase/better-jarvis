"""
情报台卡片注册表（Intel card registry）

情报台 = 注册进来的「卡片」的组合，而不是写死的版块。每张卡片自己回答"我现在
有没有值得显示的内容"，空的/安静的自动隐藏 → 天生抗信息堆砌。卡片按 tier 分三层：

  - action  ：需要你现在处理的（待审核、高意向新客、到期提醒……）
  - monitor ：可一眼扫的关键指标/趋势
  - status  ：状态灯 / 可下钻的明细（信号流、投递与连接状态）

未来接新信息域（如月度投资、日历、邮件）= 写一个 provider 注册一张卡片，前端零改动。
这是与项目里「工具注册中心」一致的注册式扩展模式。

provider 是 async，返回（任一字段可缺省）：
  {
    "has_content": bool,                 # False → 该卡隐藏
    "urgency": "high|normal|low",        # action 卡的紧迫度，影响排序/强调
    "metrics": [{"label","value","tone"?}],          # 指标瓦片
    "items":   [{"text","badge"?,"tone"?,"url"?,      # 列表行
                 "actions"?:[{"label","url","body"?}]}],  # 行内动作（前端 POST 后刷新）
    "note": str,                         # 卡片底部小字
  }
tone ∈ red|yellow|green|dim。
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

TIERS = ("action", "monitor", "status")

# [{id,title,tier,order,provider}]
_CARDS: list[dict] = []


def register_card(card_id: str, title: str, tier: str,
                  provider: Callable[[], Awaitable[dict]], *, order: int = 100) -> None:
    if tier not in TIERS:
        raise ValueError(f"未知 tier：{tier}（应为 {TIERS}）")
    # 重名替换，便于热重载/重复导入
    _CARDS[:] = [c for c in _CARDS if c["id"] != card_id]
    _CARDS.append({"id": card_id, "title": title, "tier": tier,
                   "order": order, "provider": provider})


def registered_ids() -> list[str]:
    return [c["id"] for c in _CARDS]


async def _run(card: dict) -> tuple[dict, dict]:
    try:
        data = await card["provider"]() or {}
    except Exception as e:  # 单卡失败不拖垮整个仪表盘
        data = {"has_content": False, "_error": str(e)}
    return card, data


async def build_dashboard() -> dict:
    """并发跑所有卡片 provider，按 tier 分层、隐藏空卡、组内按 order 排。"""
    if not _CARDS:
        return {"tiers": {t: [] for t in TIERS}}
    results = await asyncio.gather(*[_run(c) for c in _CARDS])
    tiers: dict[str, list] = {t: [] for t in TIERS}
    for card, data in results:
        if not data.get("has_content"):
            continue
        tiers[card["tier"]].append({
            "id": card["id"], "title": card["title"], "tier": card["tier"],
            "order": card["order"],
            "urgency": data.get("urgency", "normal"),
            "metrics": data.get("metrics", []),
            "items": data.get("items", []),
            "note": data.get("note", ""),
        })
    urgency_rank = {"high": 0, "normal": 1, "low": 2}
    for t in tiers:
        tiers[t].sort(key=lambda c: (urgency_rank.get(c["urgency"], 1), c["order"]))
    return {"tiers": tiers}
