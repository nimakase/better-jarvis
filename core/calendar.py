"""
内置日历 —— 时间真源 / 单一事实来源（方案 C，阶段 1）

设计见仓库根目录《内置日历设计.md》。本模块是日历核心：

  - 一张可扩展事件表 calendar_events（kind + meta JSON 袋 → 新事件种类零 schema 迁移；
    rrule 字段 → 重复事件是第一类公民，自己展开，不外包 scheduler）。
  - 真源 vs 派生：用户事件 / 休假 rest / 一次性提醒 reminder 存表（真源）；证件保单到期、
    定时任务下次运行等不入表，由各来源 provider 读时现算（派生）。
  - register_source(name, fn)：来源注册表（扩展枢纽，对标 intel_cards.register_card）。
    新功能要上日历 = 在自己模块里 register_source(...)，日历核心不动。
  - agenda(start, end)：统一读 API，合并「表内事件（展开 rrule）」+「各 provider 派生条目」
    → 一条排好序的时间轴。

性能铁律：重复事件展开**必须**走 dateutil.rrule.between(start, end)（区间查询），
**禁止**从创建日全量展开后再过滤——这是无限重复规则唯一的性能隐患点。

存在同一个 memory.db（独立表 calendar_events），复用 core.memory 的连接助手，
与 history / profile / vault 同构。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, date, timezone, timedelta
from typing import Any, Callable, Optional

from dateutil import rrule as _rrule
from dateutil.parser import isoparse

from core.memory import _get_conn

# ── rrule 子集白名单（已定稿 2026-06-23，见设计文件 §10）─────────────────────────
# 支持：FREQ ∈ {DAILY,WEEKLY,MONTHLY,YEARLY} + INTERVAL + BYDAY(含序号 -1FR/2MO)
#       + BYMONTHDAY + BYMONTH + COUNT 或 UNTIL
# 明确不支持：BYSETPOS / BYWEEKNO / BYYEARDAY / BYHOUR|MINUTE|SECOND
_ALLOWED_FREQ = {"DAILY": _rrule.DAILY, "WEEKLY": _rrule.WEEKLY,
                 "MONTHLY": _rrule.MONTHLY, "YEARLY": _rrule.YEARLY}
_ALLOWED_PARTS = {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "COUNT", "UNTIL"}
_WEEKDAY = {"MO": _rrule.MO, "TU": _rrule.TU, "WE": _rrule.WE, "TH": _rrule.TH,
            "FR": _rrule.FR, "SA": _rrule.SA, "SU": _rrule.SU}

# 全天事件默认提醒时刻（无显式 notify 时，见设计文件 §9）
DEFAULT_ALLDAY_NOTIFY = "09:00"


# ── 时间助手 ────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(dt: Any) -> datetime:
    """把 ISO 字符串 / date / datetime 统一成 datetime；纯日期按当天 00:00。"""
    if isinstance(dt, datetime):
        return dt
    if isinstance(dt, date):
        return datetime(dt.year, dt.month, dt.day)
    s = str(dt).strip()
    return isoparse(s)


def _is_date_only(dt: Any) -> bool:
    """判断输入是否为「纯日期、无时刻」（用于把窗口右端补成当天 23:59:59）。"""
    if isinstance(dt, datetime):
        return False
    if isinstance(dt, date):
        return True
    s = str(dt).strip()
    return bool(s) and ("T" not in s and ":" not in s)


def _parse_window_end(dt: Any) -> datetime:
    """窗口右端：纯日期（如 2026-06-30）含义是「整天」，补到当天 23:59:59，
    避免当天带时刻的事件/重复发生被 midnight 边界漏掉。"""
    end = _parse(dt)
    if _is_date_only(dt):
        return end.replace(hour=23, minute=59, second=59)
    return end


# ── 建表 ────────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """建表，幂等。"""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS calendar_events (
                id            TEXT PRIMARY KEY,
                title         TEXT NOT NULL,
                start         TEXT NOT NULL,        -- ISO 日期或日期时间
                end           TEXT,                 -- 可空；区间事件
                all_day       INTEGER DEFAULT 0,
                kind          TEXT NOT NULL DEFAULT 'event',  -- event/rest/reminder/...
                rrule         TEXT,                 -- 可空；RRULE 子集字符串
                notify        TEXT,                 -- 可空；JSON：提醒设置
                notify_state  TEXT,                 -- 已推送标记（JSON / 占用位）
                source        TEXT NOT NULL DEFAULT 'user',   -- user / system
                meta          TEXT,                 -- JSON 扩展袋
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_calevt_kind  ON calendar_events(kind);
            CREATE INDEX IF NOT EXISTS idx_calevt_start ON calendar_events(start);
        """)


# ── rrule 子集校验 ──────────────────────────────────────────────────────────────

def validate_rrule(rule: str) -> tuple[bool, str]:
    """校验 RRULE 是否落在已定稿子集内。返回 (ok, message)。"""
    if not rule or not rule.strip():
        return True, ""
    parts: dict[str, str] = {}
    for seg in rule.strip().upper().split(";"):
        if not seg:
            continue
        if "=" not in seg:
            return False, f"RRULE 片段缺少 '='：{seg!r}"
        k, v = seg.split("=", 1)
        if k not in _ALLOWED_PARTS:
            return False, f"不支持的 RRULE 字段：{k}（子集仅允许 {sorted(_ALLOWED_PARTS)}）"
        parts[k] = v
    if "FREQ" not in parts:
        return False, "RRULE 必须含 FREQ"
    if parts["FREQ"] not in _ALLOWED_FREQ:
        return False, f"不支持的 FREQ：{parts['FREQ']}（仅 {sorted(_ALLOWED_FREQ)}）"
    if "COUNT" in parts and "UNTIL" in parts:
        return False, "COUNT 与 UNTIL 互斥，只能给一个"
    # 让 dateutil 真正解析一遍，借它兜住 BYDAY 序号 / 数值格式等细节
    try:
        _build_rrule(rule, datetime(2000, 1, 1))
    except Exception as e:
        return False, f"RRULE 无法解析：{e}"
    return True, ""


def _build_rrule(rule: str, dtstart: datetime) -> _rrule.rrule:
    """把子集 RRULE 字符串构造成 dateutil.rrule 对象（dtstart 为事件起点）。"""
    parts: dict[str, str] = {}
    for seg in rule.strip().upper().split(";"):
        if not seg or "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        parts[k] = v

    kwargs: dict[str, Any] = {"dtstart": dtstart}
    kwargs["freq"] = _ALLOWED_FREQ[parts["FREQ"]]
    if "INTERVAL" in parts:
        kwargs["interval"] = int(parts["INTERVAL"])
    if "COUNT" in parts:
        kwargs["count"] = int(parts["COUNT"])
    if "UNTIL" in parts:
        kwargs["until"] = _parse_until(parts["UNTIL"])
    if "BYMONTH" in parts:
        kwargs["bymonth"] = [int(x) for x in parts["BYMONTH"].split(",")]
    if "BYMONTHDAY" in parts:
        kwargs["bymonthday"] = [int(x) for x in parts["BYMONTHDAY"].split(",")]
    if "BYDAY" in parts:
        kwargs["byweekday"] = [_parse_byday(tok) for tok in parts["BYDAY"].split(",")]
    return _rrule.rrule(**kwargs)


def _parse_byday(token: str):
    """'MO' / '-1FR' / '2MO' → dateutil weekday（可带序号）。"""
    token = token.strip().upper()
    # 拆出可选的前导序号（含负号）
    if token and (token[0] in "+-" or token[0].isdigit()):
        j = 1 if token[0] in "+-" else 0
        while j < len(token) and token[j].isdigit():
            j += 1
        num = int(token[:j])
        wd = token[j:]
        return _WEEKDAY[wd](num)
    return _WEEKDAY[token]


def _parse_until(s: str) -> datetime:
    """UNTIL 接受 YYYYMMDD 或 ISO；统一成 datetime。"""
    s = s.strip()
    if len(s) == 8 and s.isdigit():
        return datetime(int(s[:4]), int(s[4:6]), int(s[6:8]), 23, 59, 59)
    return _parse(s)


# ── 行 → dict ───────────────────────────────────────────────────────────────────

def _row_to_event(r) -> dict:
    return {
        "id": r["id"],
        "title": r["title"],
        "start": r["start"],
        "end": r["end"],
        "all_day": bool(r["all_day"]),
        "kind": r["kind"],
        "rrule": r["rrule"],
        "notify": json.loads(r["notify"]) if r["notify"] else None,
        "notify_state": json.loads(r["notify_state"]) if r["notify_state"] else None,
        "source": r["source"],
        "meta": json.loads(r["meta"]) if r["meta"] else {},
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


# ── CRUD ────────────────────────────────────────────────────────────────────────

def create_event(title: str, start: Any, *, end: Any = None, all_day: bool = False,
                 kind: str = "event", rrule: Optional[str] = None,
                 notify: Any = None, source: str = "user",
                 meta: Optional[dict] = None) -> dict:
    """新建事件。返回 {ok, message, event?}。"""
    title = (title or "").strip()
    if not title:
        return {"ok": False, "message": "标题为空，未创建。"}
    try:
        start_dt = _parse(start)
    except Exception as e:
        return {"ok": False, "message": f"start 无法解析：{e}"}
    end_iso = None
    if end:
        try:
            end_iso = _parse(end).isoformat()
        except Exception as e:
            return {"ok": False, "message": f"end 无法解析：{e}"}
    if rrule:
        ok, msg = validate_rrule(rrule)
        if not ok:
            return {"ok": False, "message": msg}

    eid = uuid.uuid4().hex
    now = _now()
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO calendar_events
              (id, title, start, end, all_day, kind, rrule, notify, notify_state,
               source, meta, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (eid, title, start_dt.isoformat(), end_iso, 1 if all_day else 0,
              kind, (rrule or None),
              json.dumps(notify, ensure_ascii=False) if notify else None,
              None, source,
              json.dumps(meta, ensure_ascii=False) if meta else None,
              now, now))
    return {"ok": True, "message": "已创建。", "event": get_event(eid)}


def get_event(event_id: str) -> Optional[dict]:
    with _get_conn() as conn:
        r = conn.execute("SELECT * FROM calendar_events WHERE id = ?", (event_id,)).fetchone()
    return _row_to_event(r) if r else None


def list_events(kind: Optional[str] = None) -> list[dict]:
    """列原始表行（不展开 rrule）。"""
    with _get_conn() as conn:
        if kind:
            rows = conn.execute(
                "SELECT * FROM calendar_events WHERE kind = ? ORDER BY start", (kind,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM calendar_events ORDER BY start").fetchall()
    return [_row_to_event(r) for r in rows]


_UPDATABLE = {"title", "start", "end", "all_day", "kind", "rrule", "notify", "meta"}


def update_event(event_id: str, **fields) -> dict:
    cur = get_event(event_id)
    if not cur:
        return {"ok": False, "message": "事件不存在。"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _UPDATABLE:
            continue
        if k == "rrule" and v:
            ok, msg = validate_rrule(v)
            if not ok:
                return {"ok": False, "message": msg}
        if k in ("start", "end") and v:
            try:
                v = _parse(v).isoformat()
            except Exception as e:
                return {"ok": False, "message": f"{k} 无法解析：{e}"}
        if k == "all_day":
            v = 1 if v else 0
        elif k in ("notify", "meta") and v is not None and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return {"ok": False, "message": "没有可更新字段。"}
    sets.append("updated_at = ?")
    vals.append(_now())
    vals.append(event_id)
    with _get_conn() as conn:
        conn.execute(f"UPDATE calendar_events SET {', '.join(sets)} WHERE id = ?", vals)
    return {"ok": True, "message": "已更新。", "event": get_event(event_id)}


def delete_event(event_id: str) -> dict:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
    if cur.rowcount:
        return {"ok": True, "message": "已删除。"}
    return {"ok": False, "message": "事件不存在。"}


# ── 展开（区间查询，禁止全量）────────────────────────────────────────────────────

def _expand_event(ev: dict, win_start: datetime, win_end: datetime) -> list[dict]:
    """把一条表事件展开成落在 [win_start, win_end] 内的发生（occurrence）列表。

    重复事件用 dateutil.rrule.between 只算窗口内的发生（性能铁律）；
    非重复事件落在窗口内则原样返回一条。
    """
    start_dt = _parse(ev["start"])
    duration = None
    if ev.get("end"):
        try:
            duration = _parse(ev["end"]) - start_dt
        except Exception:
            duration = None

    if not ev.get("rrule"):
        # 单次事件：区间相交才算。区间事件按 [start, end] 与窗口相交判断。
        ev_end = start_dt + duration if duration else start_dt
        if ev_end >= win_start and start_dt <= win_end:
            return [_occurrence(ev, start_dt, duration)]
        return []

    rule = _build_rrule(ev["rrule"], start_dt)
    # inc=True 含端点；只展开窗口内，绝不从 dtstart 全量展开
    occs = rule.between(win_start, win_end, inc=True)
    return [_occurrence(ev, dt, duration) for dt in occs]


def _occurrence(ev: dict, occ_start: datetime, duration) -> dict:
    o = {
        "id": ev["id"],
        "title": ev["title"],
        "start": occ_start.isoformat(),
        "end": (occ_start + duration).isoformat() if duration else ev.get("end"),
        "all_day": ev.get("all_day", False),
        "kind": ev.get("kind", "event"),
        "source": ev.get("source", "user"),
        "meta": ev.get("meta", {}),
        "recurring": bool(ev.get("rrule")),
    }
    return o


# ── 来源注册表（扩展枢纽，对标 intel_cards.register_card）────────────────────────
# provider 签名：fn(start: datetime, end: datetime) -> list[CalEntry dict]
# CalEntry 至少含 {title, start}，可含 end/kind/source/meta；这些是“派生”条目，不入表。
_SOURCES: list[dict] = []


def register_source(name: str, fn: Callable[[datetime, datetime], list[dict]]) -> None:
    """注册一个派生来源。重名替换（便于热重载/重复导入）。"""
    _SOURCES[:] = [s for s in _SOURCES if s["name"] != name]
    _SOURCES.append({"name": name, "fn": fn})


def registered_sources() -> list[str]:
    return [s["name"] for s in _SOURCES]


# ── 统一读 API ──────────────────────────────────────────────────────────────────

def agenda(start: Any = None, end: Any = None, *, days: int = 7) -> list[dict]:
    """合并「表内事件（展开 rrule）」+「各 provider 派生条目」→ 排好序的时间轴。

    start 缺省 = 现在；end 缺省 = start + days 天。
    """
    win_start = _parse(start) if start else datetime.now()
    win_end = _parse_window_end(end) if end else (win_start + timedelta(days=days))

    out: list[dict] = []
    # 1) 表内真源：逐条按窗口展开
    for ev in list_events():
        try:
            out.extend(_expand_event(ev, win_start, win_end))
        except Exception:
            # 单条坏数据不拖垮整条时间轴
            continue
    # 2) 派生 provider：读时现算（单个 provider 失败不影响其余）
    for s in _SOURCES:
        try:
            for entry in (s["fn"](win_start, win_end) or []):
                e = dict(entry)
                e.setdefault("source", s["name"])
                e.setdefault("kind", "derived")
                e["derived"] = True
                out.append(e)
        except Exception:
            continue

    out.sort(key=lambda e: str(e.get("start") or ""))
    return out


# ── 环境感知块（阶段 2 接入 system prompt；此处先提供函数）────────────────────────

def build_block(days: int = 7, max_items: int = 12) -> str:
    """拼成注入 system prompt 的「近期日程」块；空则返回空串。小而精，有条数上限。"""
    items = agenda(days=days)[:max_items]
    if not items:
        return ""
    lines = [f"【近期日程（未来 {days} 天；如与用户最新说法冲突，以用户为准）】"]
    for e in items:
        when = str(e.get("start", ""))[:16].replace("T", " ")
        tag = {"rest": "[休假]", "reminder": "[提醒]"}.get(e.get("kind"), "")
        lines.append(f"- {when} {tag}{e.get('title', '')}".rstrip())
    return "\n".join(lines)


# ── 休假（rest）桥接 API（阶段 4：休假＝日历事件 kind=rest）────────────────────
# 休假是真源，存进 calendar_events（kind=rest）。delivery/availability 经这些函数
# 读写休假，不再各持一份 rest_periods。返回形状对齐旧 rest_periods，便于上层无感切换。

def add_rest(start: str, end: str, reason: str = "休假") -> dict:
    """登记一段休假（含首尾）。存为全天 rest 事件，confirmed 放 meta。"""
    return create_event(reason or "休假", start, end=end, all_day=True,
                        kind="rest", source="user",
                        meta={"reason": reason or "休假", "confirmed": False})


def clear_rests() -> int:
    """清除全部休假事件，返回删除条数。"""
    n = 0
    for ev in list_events("rest"):
        if delete_event(ev["id"]).get("ok"):
            n += 1
    return n


def list_rests() -> list[dict]:
    """列休假，形状对齐旧 rest_periods：{id, start, end, reason, confirmed}（日期为 YYYY-MM-DD）。"""
    out = []
    for ev in list_events("rest"):
        meta = ev.get("meta") or {}
        out.append({
            "id": ev["id"],
            "start": str(ev["start"])[:10],
            "end": str(ev.get("end") or ev["start"])[:10],
            "reason": meta.get("reason") or ev["title"],
            "confirmed": bool(meta.get("confirmed")),
        })
    return out


def confirm_rest(rest_id: str) -> bool:
    """把某段休假标记为已确认（写进 meta.confirmed；不影响闸门）。"""
    ev = get_event(rest_id)
    if not ev or ev.get("kind") != "rest":
        return False
    meta = dict(ev.get("meta") or {})
    meta["confirmed"] = True
    update_event(rest_id, meta=meta)
    return True


def active_rest(as_of: Optional[str] = None) -> Optional[dict]:
    """返回覆盖 as_of（默认今天）的休假，无则 None。供 delivery.is_paused 用。"""
    today = date.fromisoformat(as_of) if as_of else date.today()
    for r in list_rests():
        try:
            if date.fromisoformat(r["start"]) <= today <= date.fromisoformat(r["end"]):
                return r
        except Exception:
            continue
    return None


# ── 提醒巡检（阶段 5：心跳为纯机械、零 AI）──────────────────────────────────────
# notify 两种模式：{"type":"before","minutes":N}（挂 start，提前 N 分钟）
#                  {"type":"at","time":"HH:MM"}（绝对时刻；全天/区间事件用）。
# notify_state.last_fired 作水位：只发水位之后、且 <= now 的触发点，天然去重 + 防补发风暴。
# 补发窗口 grace_hours：醒来/重启后若最近触发点已超窗，只推进水位（标记），不推送。

def _reminder_trigger(notify: dict, occ_start: datetime, all_day: bool) -> Optional[datetime]:
    """由 notify 设置与一次发生(occurrence)算出该提醒的触发时刻。"""
    t = (notify or {}).get("type")
    if t == "before":
        try:
            return occ_start - timedelta(minutes=int(notify.get("minutes", 0)))
        except Exception:
            return None
    if t == "at":
        hhmm = notify.get("time") or (DEFAULT_ALLDAY_NOTIFY if all_day else "09:00")
        try:
            h, m = (int(x) for x in str(hhmm).split(":")[:2])
        except Exception:
            return None
        return occ_start.replace(hour=h, minute=m, second=0, microsecond=0)
    return None


def _reminder_occurrences(ev: dict, win_start: datetime, win_end: datetime) -> list[datetime]:
    start_dt = _parse(ev["start"])
    if not ev.get("rrule"):
        return [start_dt]
    try:
        return _build_rrule(ev["rrule"], start_dt).between(win_start, win_end, inc=True)
    except Exception:
        return []


def _set_notify_fired(event_id: str, fired_iso: str) -> None:
    with _get_conn() as conn:
        conn.execute("UPDATE calendar_events SET notify_state = ?, updated_at = ? WHERE id = ?",
                     (json.dumps({"last_fired": fired_iso}), _now(), event_id))


def collect_due_reminders(now: Optional[datetime] = None, grace_hours: int = 2) -> list[dict]:
    """扫所有带 notify 的事件，返回该推送的提醒并推进各自水位（标记已发）。

    多个错过的触发点合并为最近一个，避免补发风暴；最近触发点超出 grace 窗口则只标记不推。
    本函数纯机械：只读表 + 算时刻 + 翻状态位，不涉及任何模型调用。
    """
    now = now or datetime.now()
    grace = timedelta(hours=grace_hours)
    out: list[dict] = []
    for ev in list_events():
        notify = ev.get("notify")
        if not notify:
            continue
        state = ev.get("notify_state") or {}
        watermark = None
        if state.get("last_fired"):
            try:
                watermark = _parse(state["last_fired"])
            except Exception:
                watermark = None
        # 发生搜索窗口：覆盖水位到现在，左右各留一天兜住 before 偏移
        lo = (watermark or (now - grace - timedelta(days=1))) - timedelta(days=1)
        occs = _reminder_occurrences(ev, lo, now + timedelta(days=1))
        triggers = []
        for occ in occs:
            tt = _reminder_trigger(notify, occ, ev.get("all_day", False))
            if tt is None:
                continue
            if (watermark is None or tt > watermark) and tt <= now:
                triggers.append((tt, occ))
        if not triggers:
            continue
        triggers.sort()
        tt, occ = triggers[-1]            # 最近一个到点的；更早错过的合并掉
        _set_notify_fired(ev["id"], now.isoformat())  # 无论推不推，水位都前进
        if now - tt <= grace:
            out.append({
                "id": ev["id"], "title": ev["title"],
                "occ_start": occ.isoformat(), "trigger": tt.isoformat(),
                "kind": ev.get("kind", "event"), "all_day": ev.get("all_day", False),
            })
    return out


def format_reminder(item: dict) -> tuple[str, str]:
    """提醒文案——模板拼接，零 AI（阶段 5）。返回 (title, content)。"""
    when = str(item.get("occ_start", ""))[:16].replace("T", " ")
    kind = item.get("kind")
    if kind == "expiry":
        verb = "即将到期"
    elif item.get("all_day"):
        verb = "今日"
    else:
        verb = "即将开始"
    name = item.get("title", "")
    return f"提醒：{name}", f"{name} {verb}（{when}）"


# 初始化
init_db()
