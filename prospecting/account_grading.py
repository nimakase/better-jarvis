"""prospecting/account_grading.py — 账户 6 格分级(纯逻辑,零依赖、可单测)。

设计见 docs/客户循环与Breeze设计方案.md 第三节。两轴:
  ACCOUNT TYPE = 关系耐久度:core(有 ≥1 个 deal)/ prospecting(无 deal)
  PRIORITY     = 当前热度:hot / warm / cold

判据(全部只吃可靠账户级字段,零 Breeze):
  core:       open deal>0 → hot;否则 last_activity 在 CORE_WARM_DAYS 内 → warm;再否则 cold(仍留 core)
  prospecting: last_engagement 在 PROSPECT_HOT_DAYS 内 → hot;PROSPECT_WARM_DAYS 内 → warm;否则 cold

这里只【算】,不读 HubSpot、不写回。读取(②b)与写回(③/对账)在别处。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional, Union

# ── 可调旋钮(交回测校准;初值拍脑袋)──────────────────────────────
CORE_WARM_DAYS = 90        # core 无 open deal 时,活动在此天数内算 warm,否则 cold
PROSPECT_HOT_DAYS = 14     # prospecting engagement 在此天数内算 hot
PROSPECT_WARM_DAYS = 60    # prospecting engagement 在此天数内算 warm,否则 cold

# priority 底层是数值(HubSpot 该属性),已确认:Hot=5 / Warm=3 / Cold=1 / dead=0。
# classify() 只会输出 hot/warm/cold;dead 是 Ned 手动标"死账户"的保护值,分类器绝不产出、
# 也绝不自动覆盖(见 reconcile 的 dead 保护)。
PRIORITY_VALUE = {"hot": 5, "warm": 3, "cold": 1, "dead": 0}

_DateLike = Union[date, datetime, str, None]


def _to_date(v: _DateLike) -> Optional[date]:
    """把 date/datetime/ISO 字符串规整成 date;认不出或空 → None。

    ②b 应尽量传规范 ISO(YYYY-MM-DD);这里对常见格式做防御式兜底。
    """
    if v is None or v == "" or v == "--":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    # ISO 优先
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    # 兜底:dateutil 模糊解析(吃 "24 Jul 2026 07:31 GMT+8" 这类 HubSpot 单元格)。
    # python-dateutil 是项目依赖;万一没装则安静放弃。
    try:
        from dateutil import parser as _dtparser
        return _dtparser.parse(s, fuzzy=True).date()
    except Exception:
        return None


def _age_days(v: _DateLike, today: Optional[date]) -> Optional[int]:
    d = _to_date(v)
    if d is None:
        return None
    today = today or datetime.now(timezone.utc).date()
    return (today - d).days


def classify(fields: dict, today: Optional[date] = None) -> dict:
    """算一个账户的分级。

    fields 需含(缺失按最保守处理):
      num_associated_deals: int   有 deal 数(=core 触发)
      num_open_deals:       int   open deal 数(=core hot 信号)
      last_activity_date:   date/str/None
      last_engagement_date: date/str/None
    返回 {"type","priority","cell","priority_value"}。
    """
    n_deals = int(fields.get("num_associated_deals") or 0)
    n_open = int(fields.get("num_open_deals") or 0)

    # 有 open deal 隐含有 deal;两者任一 >0 即 core(防脏数据)
    is_core = n_deals > 0 or n_open > 0

    if is_core:
        atype = "core"
        if n_open > 0:
            prio = "hot"
        else:
            age = _age_days(fields.get("last_activity_date"), today)
            prio = "warm" if (age is not None and age <= CORE_WARM_DAYS) else "cold"
    else:
        atype = "prospecting"
        age = _age_days(fields.get("last_engagement_date"), today)
        if age is not None and age <= PROSPECT_HOT_DAYS:
            prio = "hot"
        elif age is not None and age <= PROSPECT_WARM_DAYS:
            prio = "warm"
        else:
            prio = "cold"

    return {
        "type": atype,
        "priority": prio,
        "cell": f"{atype}_{prio}",
        "priority_value": PRIORITY_VALUE.get(prio),
    }


# ── 首轮对账:计算值 vs 现有值 → 四桶 ────────────────────────────
BUCKET_MATCH = "match"
BUCKET_FILL_BLANK = "fill_blank"              # 现有 priority 空 → 安全自动填
BUCKET_PRIORITY_MISMATCH = "priority_mismatch"  # priority 分歧 → 人工复核
BUCKET_TYPE_PROMOTE = "type_promote"         # prospecting→core(有 deal)升级 → 安全自动
BUCKET_TYPE_DEMOTE = "type_demote"           # core→prospecting 降级 → 人工(会武装自动发信)
BUCKET_TYPE_DEMOTE_RECENT = "type_demote_recent"  # Core 无 deal 但建号很新 → deal 待补,暂不降
BUCKET_SKIP_DEAD = "skip_dead"               # 现有=0(dead)手动死标 → 保护,绝不自动改

# 降级候选="真死号"才算:必须【建号够老】且【长期无活动】。任一不满足→暂不降(是活关系或新号)。
#   - 建号不足 90 天 → 新拉的客户(如 Paragon 站点),deal 待补,别误降;
#   - 最后活动在 180 天(≈6 个月)内 → 你近期还在往来的活关系(如 Triode 2 月还有活动),别降。
# 与工作流文档「6 个月无活动才衰减」对齐。两阈值都是旋钮。
CORE_DEMOTE_MIN_AGE_DAYS = 90
CORE_DEMOTE_MIN_INACTIVE_DAYS = 180

# 可自动落值的桶(其余进人工复核清单)。priority_mismatch 归自动:现有值多为历史
# 噪音(继承/旧手动/公司默认打标),首轮信任计算值。type_demote 仍留人工(降级高风险)。
# ⚠ 应用时机 = 第一次正式夜间循环,不在搭建期手动写。
AUTO_BUCKETS = {BUCKET_FILL_BLANK, BUCKET_TYPE_PROMOTE, BUCKET_PRIORITY_MISMATCH}


def _norm(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = str(s).strip().lower()
    return None if s in ("", "--") else s


def reconcile(computed: dict, existing: dict, created=None, last_activity=None,
              today=None) -> dict:
    """比对算出值与现有值,判桶(见设计文档「首轮对账」)。

    existing:      {"type": core/prospecting/None, "priority": hot/warm/cold/None(空/--视作 None)}
    created:       账户创建日期;last_activity:最后活动日期。二者用于"真死号才降级"闸——
                   建号不足 90 天、或最后活动在 180 天内的 Core-无deal,视作活关系/新号,不降。
    优先级:dead 保护 > type 不一致 > 现有 priority 空(填空)> priority 不一致 > 一致。
    返回 {"bucket","computed","existing","write"(该桶建议要写的字段,None=不写)}。
    """
    ex_type = _norm(existing.get("type"))
    ex_prio = _norm(existing.get("priority"))
    co_type = _norm(computed.get("type"))
    co_prio = _norm(computed.get("priority"))

    # dead 保护:Ned 手动标了 0(dead) → 绝不自动动(否则会"复活"他判死的账户)。
    if ex_prio == "dead":
        return {"bucket": BUCKET_SKIP_DEAD, "computed": computed, "existing": existing, "write": None}

    if ex_type is not None and ex_type != co_type:
        if co_type == "core":
            # prospecting→core:有 deal 却没升 Core。升级安全(Core 永不衰减)→ 自动。
            # 顺带落 priority(升级后按 core 算的热度)。
            bucket = BUCKET_TYPE_PROMOTE
            write = {"type": "core", "priority": co_prio}
        else:
            # core→prospecting:标了 Core 却无 deal。只有"真死号"才当降级候选——
            # 建号很新(新拉客户 deal 待补,如 Paragon)或近期有活动(活关系,如 Triode 2月还有往来)
            # 都不降,归 demote_recent 暂放。
            age_created = _age_days(created, today)
            age_activity = _age_days(last_activity, today)
            recently_created = age_created is not None and age_created < CORE_DEMOTE_MIN_AGE_DAYS
            recently_active = age_activity is not None and age_activity < CORE_DEMOTE_MIN_INACTIVE_DAYS
            if recently_created or recently_active:
                bucket = BUCKET_TYPE_DEMOTE_RECENT
            else:
                bucket = BUCKET_TYPE_DEMOTE   # 老 + 长期无活动 + 无 deal → 真死号候选(仍人工)
            write = None
    elif ex_prio is None:
        bucket = BUCKET_FILL_BLANK             # 现有为空,安全填
        write = {"priority": co_prio}
    elif ex_prio != co_prio:
        # 现有值多为噪音,首轮信任计算值 → 自动纠正(写计算出的 priority)。
        bucket = BUCKET_PRIORITY_MISMATCH
        write = {"priority": co_prio}
    else:
        bucket = BUCKET_MATCH
        write = None

    return {"bucket": bucket, "computed": computed, "existing": existing, "write": write}


def build_write_plan(report: dict) -> dict:
    """把对账报告拆成「自动写清单」和「人工降级清单」。纯逻辑,不执行写入。

    report: account_reader.grade_all() 的产出({"buckets": {bucket: [reconcile结果...]}})。
    返回:
      auto        - 可自动写(fill_blank/type_promote/priority_mismatch),每项带 write 载荷
      manual_demote - type_demote:core→无 deal,需人工;动作=降 prospecting + Sequence Opt-Out
    应用时机 = 第一次正式夜间循环。
    """
    buckets = report.get("buckets", {})
    auto, manual_demote, recent_hold = [], [], []
    for bucket, items in buckets.items():
        for it in items:
            if bucket in AUTO_BUCKETS and it.get("write"):
                auto.append(it)
            elif bucket == BUCKET_TYPE_DEMOTE:
                d = dict(it)
                d["write"] = {"type": "prospecting", "sequence_opt_out": True}
                manual_demote.append(d)
            elif bucket == BUCKET_TYPE_DEMOTE_RECENT:
                recent_hold.append(it)   # 新建 Core 无 deal:暂不动,等 deal 补上或到龄再看
    return {
        "auto": auto,
        "manual_demote": manual_demote,
        "recent_hold": recent_hold,
        "counts": {"auto": len(auto), "manual_demote": len(manual_demote),
                   "recent_hold": len(recent_hold)},
    }
