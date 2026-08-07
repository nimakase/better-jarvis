"""prospecting/outreach_state.py — 冷开发状态判定(v2,纯逻辑,零依赖、可单测)。

设计见 docs/客户循环-view管理重设计.md「附二」。v2 改动(2026-08-05):
  废弃旧的「离上封 >14 天 = 待处理」(ACTIVE_DAYS=14)—— 那与 Ned 实际 2 个月一轮的节奏不符,
  会把"正等着发下一轮"的账户误判成待处理(= 之前 401 误标的一个诱因)。

改成【两个时钟】:
  节奏钟(Ned 自己的 sequence:3 轮 × 每轮 3 封同标题连发,轮间隔 ~2 月):
    not_started —— 从没发过
    sequencing  —— 还在跑(三轮没完),且距上封 ≤ ROUND_DUE_DAYS(在节奏上,别动)
    round_due   —— 还在跑,但距上封 > ROUND_DUE_DAYS(该发下一轮了,提醒 Ned)
    exhausted   —— 三轮跑完仍没回(决策点:换 contact 重开 / 撒手让其 decay)
    replied     —— 有联系人回过信(最高优先)
  死线钟(公司 time-decay):由 HubSpot 原生 Decay Stage 属性【读入】,原样透传(decay_stage),
    orchestration 用它把临近 final warning 的账户单独拎出提醒,不在本模块算。

轮次从发送日期【聚簇】反推(同一轮 3 封连发挤在一起,轮间隔 ~2 月):相邻发送间隔
  > NEW_ROUND_GAP_DAYS 即新一轮。公司 auto-sequence 用 Ned 身份发、只在停手约 4.5 个月后触发,
  故它会是一个"前置大空档(> COMPANY_SEQ_GAP_DAYS)"的簇 → 识别为系统轮、不计进 Ned 的轮次。

联系人明细(谁发过几封、岗位、未试过谁)照样算出来放进结果供换人参考,但【不参与状态判定】。
本模块只算,不读 HubSpot、不问 Breeze、不写 view。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from prospecting.account_grading import _to_date  # 复用日期解析(纯函数)

# ── 旋钮 ──────────────────────────────────────────────────────
NEW_ROUND_GAP_DAYS = 30      # 相邻发送间隔 > 此值 → 视作新一轮(轮内连发挤在一起,轮间隔~2月)
ROUND_DUE_DAYS = 60          # 距上封 > 此值且三轮没跑完 → 该发下一轮(2 个月节奏)
MAX_ROUNDS = 3               # Ned 一个 sequence 固定三轮
COMPANY_SEQ_GAP_DAYS = 120   # 某簇前置空档 > 此值 → 公司 auto-sequence(非 Ned 轮),剔除
CORE_MAINTAIN_DAYS = 60      # core 维护默认间隔(无 tier 时);按 tier 见下

# core 维护间隔按分层(Core 永不 decay,这是纯关系保温 nudge,与公司 decay 死线无关)
CORE_MAINTAIN_DAYS_BY_TIER = {"T0": 45, "T1": 90, "T2": 60}

# Decay Stage 兜底阈值(HubSpot 没开 Decay Stage 列时,用 Last Activity Date 近似公司 6 个月线)。
# 手册阶段含"~2 周"松弛,故为估算;有权威 Decay Stage 属性时优先用它。
DECAY_INACTIVITY_DAYS = 137      # 4 个月 2 周:进入衰减
DECAY_FINAL_WARNING_DAYS = 188   # 末周之前 = in_decay;之后 = final_warning
DECAY_REASSIGN_DAYS = 195        # 之后视作已回收

VIEW_NAME = {
    "replied": "已回复·待跟进",
    "reply_pending": "待分类回复",    # ⚠ provisional:检测到 inbound、待 reply_classify 判真假;
                                      #    编排应先分类再写 view,正常不应残留到写库(残留则 view 未配→跳过)
    "not_started": "未开发",
    "sequencing": "开发中",
    "round_due": "开发中",           # 与 sequencing 同 view;差别在 next_round_due 标记驱动提醒
    "exhausted": "待处理·换人或放弃",
}


def _norm_name(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def _dates(sent_dates) -> list:
    return sorted(d for d in (_to_date(x) for x in (sent_dates or [])) if d)


def _cluster(dates: list) -> list:
    """把排序好的发送日期聚成簇(相邻间隔 ≤ NEW_ROUND_GAP_DAYS 归同簇)。返回 [[date,...],...]。"""
    clusters: list = []
    for d in dates:
        if clusters and (d - clusters[-1][-1]).days <= NEW_ROUND_GAP_DAYS:
            clusters[-1].append(d)
        else:
            clusters.append([d])
    return clusters


def analyze_rounds(all_dates: list) -> dict:
    """从账户全部发送日期反推轮次。返回 {ned_rounds, last_ned, has_system, last_any}。

    Ned 的轮 = 从头连续、簇间空档 ≤ COMPANY_SEQ_GAP_DAYS 的那些簇;一旦出现前置大空档的簇,
    它及其之后全算系统轮(公司 auto-sequence),不计进 ned_rounds。
    """
    clusters = _cluster(all_dates)
    ned: list = []
    broken = False
    for cl in clusters:
        if broken:
            continue
        if ned and (cl[0] - ned[-1][-1]).days > COMPANY_SEQ_GAP_DAYS:
            broken = True                # 这簇及之后都是系统轮
            continue
        ned.append(cl)
    return {
        "ned_rounds": len(ned),
        "last_ned": ned[-1][-1] if ned else None,
        "has_system": broken,
        "last_any": clusters[-1][-1] if clusters else None,
    }


def account_outreach_state(contacts: list, all_contacts: Optional[list] = None,
                           decay_stage: Optional[str] = None,
                           reply_is_real: Optional[bool] = None,
                           today: Optional[date] = None) -> dict:
    """算某 prospecting 账户的冷开发状态(v2)。

    contacts:     [{"name","job_title","sent_dates":[ISO...],"replied":bool}] —— Breeze 抽的。
                  ⚠ 这里的 `replied` 只是【事实:有 inbound 回来】,不代表是真回复(可能是 OOO/自动回复)。
    all_contacts: [{"name","job_title"}] —— 账户全部关联联系人(算"还剩谁没试过",供换人参考)。可空。
    decay_stage:  HubSpot 原生 Decay Stage(死线钟),原样透传;None=没读到/不适用。
    reply_is_real:【是不是真回复】的判断,来自 reply_classify(判断步),不在本模块判:
                  True=真回复→"replied";False=OOO/none等非真回复→当没回复(回落 sequencing/轮次);
                  None=还没判→"reply_pending"(provisional,编排应先跑 reply_classify 再定局)。
    today:        判定基准日;默认今天(UTC)。

    优先级:检测到 inbound(且非判假) > 从没发过 > 三轮跑完 > 该发下轮 > 在跑。
    """
    today = today or datetime.now(timezone.utc).date()
    replied = [c.get("name") for c in (contacts or []) if c.get("replied")]  # 事实:有 inbound

    tried, all_dates = [], []
    for c in (contacts or []):
        ds = _dates(c.get("sent_dates"))
        all_dates += ds
        tried.append({"name": c.get("name"), "job_title": c.get("job_title"),
                      "n_sent": len(ds), "last": ds[-1].isoformat() if ds else None,
                      "replied": bool(c.get("replied"))})
    all_dates.sort()

    ra = analyze_rounds(all_dates)
    rounds_done = ra["ned_rounds"]
    last_ned = ra["last_ned"]
    days_since = (today - last_ned).days if last_ned else None
    next_round_due = False

    # 检测到 inbound 且没被判成"非真回复"→ 交给 reply_classify 定局(True→replied / None→待分类);
    # 判成 False(OOO/none)→ 当没回复,回落到轮次状态。
    if replied and reply_is_real is not False:
        state = "replied" if reply_is_real is True else "reply_pending"
    elif not all_dates:
        state = "not_started"
    elif rounds_done >= MAX_ROUNDS:
        state = "exhausted"
    elif days_since is not None and days_since > ROUND_DUE_DAYS:
        state = "round_due"
        next_round_due = True
    else:
        state = "sequencing"

    emailed = {_norm_name(c.get("name")) for c in (contacts or []) if c.get("name")}
    untouched = [{"name": c.get("name"), "job_title": c.get("job_title")}
                 for c in (all_contacts or []) if _norm_name(c.get("name")) not in emailed]

    return {
        "state": state,
        "view": VIEW_NAME[state],
        "rounds_done": rounds_done,
        "next_round_due": next_round_due,
        "has_system_sequence": ra["has_system"],
        "last_outreach": last_ned.isoformat() if last_ned else None,
        "days_since_last": days_since,
        "decay_stage": decay_stage,
        "replied_contacts": replied,
        "tried_contacts": tried,          # 明细(供换人参考,不参与判定)
        "untouched_contacts": untouched,
        "untouched_count": len(untouched),
    }


def core_maintenance_due(last_touch, tier: Optional[str] = None, today: Optional[date] = None,
                         interval_days: Optional[int] = None) -> bool:
    """core 账户是否到维护点:从没 touch → 是;上次 touch 距今 ≥ 间隔 → 是。

    间隔取值:显式 interval_days > 按 tier 查 CORE_MAINTAIN_DAYS_BY_TIER > 默认 CORE_MAINTAIN_DAYS(60)。
    Core 永不 decay,这是纯关系保温提醒;任何 touch(Last Activity Date 前移)即重置。
    """
    interval = interval_days if interval_days is not None else \
        CORE_MAINTAIN_DAYS_BY_TIER.get(tier or "", CORE_MAINTAIN_DAYS)
    today = today or datetime.now(timezone.utc).date()
    d = _to_date(last_touch)
    if d is None:
        return True
    return (today - d).days >= interval


def derive_decay_stage(last_activity, today: Optional[date] = None) -> Optional[str]:
    """Decay Stage 兜底:HubSpot 没开该列时,从 Last Activity Date 近似公司 time-decay 阶段。

    仅 Prospecting 有意义(Core 不 decay)。返回 None(安全/未进衰减)/ "in_decay" / "final_warning" /
    "reassigned"。阈值为估算(见常量),有权威 Decay Stage 属性时应优先用它,不要用这个。
    """
    today = today or datetime.now(timezone.utc).date()
    d = _to_date(last_activity)
    if d is None:
        return None
    days = (today - d).days
    if days < DECAY_INACTIVITY_DAYS:
        return None
    if days < DECAY_FINAL_WARNING_DAYS:
        return "in_decay"
    if days < DECAY_REASSIGN_DAYS:
        return "final_warning"
    return "reassigned"
