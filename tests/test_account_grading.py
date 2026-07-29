"""账户 6 格分级 + 首轮对账 单测(纯逻辑,零依赖)。

    .venv/bin/python tests/test_account_grading.py
退出码非零 = 有失败。
"""
import sys
from datetime import date
from pathlib import Path

JARVIS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, JARVIS)

from prospecting import account_grading as g   # noqa: E402

TODAY = date(2026, 7, 26)
fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def cell(fields):
    return g.classify(fields, today=TODAY)["cell"]


# ── 六格 ─────────────────────────────────────────────
check("core_hot: 有 open deal",
      cell({"num_associated_deals": 2, "num_open_deals": 1}) == "core_hot")
check("core_warm: 有 deal 无 open + 近期活动",
      cell({"num_associated_deals": 1, "num_open_deals": 0,
            "last_activity_date": "2026-06-01"}) == "core_warm")
check("core_cold: 有 deal 无 open + 活动超期",
      cell({"num_associated_deals": 1, "num_open_deals": 0,
            "last_activity_date": "2025-01-01"}) == "core_cold")
check("core_cold: 有 deal 无 open + 无活动日期",
      cell({"num_associated_deals": 3, "num_open_deals": 0,
            "last_activity_date": None}) == "core_cold")
check("prospecting_hot: 无 deal + engagement 很近",
      cell({"num_associated_deals": 0, "num_open_deals": 0,
            "last_engagement_date": "2026-07-20"}) == "prospecting_hot")
check("prospecting_warm: 无 deal + engagement 中窗",
      cell({"num_associated_deals": 0, "num_open_deals": 0,
            "last_engagement_date": "2026-06-15"}) == "prospecting_warm")
check("prospecting_cold: 无 deal + engagement 超期",
      cell({"num_associated_deals": 0, "num_open_deals": 0,
            "last_engagement_date": "2025-01-01"}) == "prospecting_cold")
check("prospecting_cold: 无 deal + 无 engagement",
      cell({"num_associated_deals": 0, "num_open_deals": 0}) == "prospecting_cold")

# ── 脏数据 / 边界 ────────────────────────────────────
check("脏数据: open>0 但 deals=0 也算 core",
      cell({"num_associated_deals": 0, "num_open_deals": 1}) == "core_hot")
from datetime import timedelta  # noqa: E402
_edge = (TODAY - timedelta(days=g.CORE_WARM_DAYS)).isoformat()   # 恰好 90 天前
check("边界: 活动=CORE_WARM_DAYS 当天算 warm",
      cell({"num_associated_deals": 1, "num_open_deals": 0,
            "last_activity_date": _edge}) == "core_warm")

# priority 数值映射
check("priority_value hot=5",
      g.classify({"num_associated_deals": 1, "num_open_deals": 1},
                 today=TODAY)["priority_value"] == 5)

# 日期解析容错
check("日期解析: 显示格式 '24 Jul 2026'",
      g._to_date("24 Jul 2026") == date(2026, 7, 24))
check("日期解析: HubSpot 单元格 '24 Jul 2026 07:31 GMT+8'",
      g._to_date("24 Jul 2026 07:31 GMT+8") == date(2026, 7, 24))
check("日期解析: None → None", g._to_date(None) is None)
check("日期解析: '--' → None", g._to_date("--") is None)

# ── 对账四桶 ─────────────────────────────────────────
co = {"type": "core", "priority": "hot"}
check("桶 match",
      g.reconcile(co, {"type": "core", "priority": "hot"})["bucket"] == g.BUCKET_MATCH)
check("桶 fill_blank(现有 priority 空)",
      g.reconcile(co, {"type": "core", "priority": "--"})["bucket"] == g.BUCKET_FILL_BLANK)
check("fill_blank 会给出要写的值",
      g.reconcile(co, {"type": "core", "priority": None})["write"] == {"priority": "hot"})
check("桶 priority_mismatch",
      g.reconcile(co, {"type": "core", "priority": "cold"})["bucket"] == g.BUCKET_PRIORITY_MISMATCH)
check("priority_mismatch 现归自动集,带写入值",
      g.reconcile(co, {"type": "core", "priority": "cold"})["write"] == {"priority": "hot"})
check("priority_mismatch 在 AUTO 桶", g.BUCKET_PRIORITY_MISMATCH in g.AUTO_BUCKETS)
# type 升级(prospecting→core,有 deal):安全自动,连 type+priority 一起写
r_prom = g.reconcile({"type": "core", "priority": "warm"},
                     {"type": "prospecting", "priority": None})
check("桶 type_promote(prospecting→core)", r_prom["bucket"] == g.BUCKET_TYPE_PROMOTE)
check("type_promote 自动写 type=core+priority",
      r_prom["write"] == {"type": "core", "priority": "warm"})
check("type_promote 属于 AUTO 桶", g.BUCKET_TYPE_PROMOTE in g.AUTO_BUCKETS)

# type 降级(core→prospecting,无 deal,无创建日期→保守当老号):人工,不自动写
r_dem = g.reconcile({"type": "prospecting", "priority": "cold"},
                    {"type": "core", "priority": None})
check("桶 type_demote(core→prospecting)", r_dem["bucket"] == g.BUCKET_TYPE_DEMOTE)
check("type_demote 不自动写", r_dem["write"] is None)
check("type_demote 不在 AUTO 桶", g.BUCKET_TYPE_DEMOTE not in g.AUTO_BUCKETS)

# 建号年龄闸:Core 无 deal 但很新 → demote_recent(暂不降),老号 → demote
_recent = (TODAY - timedelta(days=20)).isoformat()
_old = (TODAY - timedelta(days=400)).isoformat()
_act_recent = (TODAY - timedelta(days=150)).isoformat()   # 5 个月前有活动(如 Triode)
check("Core无deal·新建20天 → demote_recent(不降)",
      g.reconcile({"type": "prospecting", "priority": "cold"}, {"type": "core", "priority": None},
                  created=_recent, today=TODAY)["bucket"] == g.BUCKET_TYPE_DEMOTE_RECENT)
check("Core无deal·老号但150天前有活动 → demote_recent(活关系,不降)",
      g.reconcile({"type": "prospecting", "priority": "cold"}, {"type": "core", "priority": None},
                  created=_old, last_activity=_act_recent, today=TODAY)["bucket"] == g.BUCKET_TYPE_DEMOTE_RECENT)
check("Core无deal·老号+长期无活动 → demote(真死号)",
      g.reconcile({"type": "prospecting", "priority": "cold"}, {"type": "core", "priority": None},
                  created=_old, last_activity=_old, today=TODAY)["bucket"] == g.BUCKET_TYPE_DEMOTE)
check("demote_recent 不在 AUTO 桶", g.BUCKET_TYPE_DEMOTE_RECENT not in g.AUTO_BUCKETS)
check("fill_blank 在 AUTO 桶", g.BUCKET_FILL_BLANK in g.AUTO_BUCKETS)

# ── build_write_plan:自动清单 vs 人工降级清单 ──────────
fake_report = {"buckets": {
    g.BUCKET_MATCH: [{"bucket": g.BUCKET_MATCH, "write": None, "account_name": "A"}],
    g.BUCKET_FILL_BLANK: [{"bucket": g.BUCKET_FILL_BLANK, "write": {"priority": "warm"}, "account_name": "B"}],
    g.BUCKET_PRIORITY_MISMATCH: [{"bucket": g.BUCKET_PRIORITY_MISMATCH, "write": {"priority": "cold"}, "account_name": "C"}],
    g.BUCKET_TYPE_PROMOTE: [{"bucket": g.BUCKET_TYPE_PROMOTE, "write": {"type": "core", "priority": "warm"}, "account_name": "D"}],
    g.BUCKET_TYPE_DEMOTE: [{"bucket": g.BUCKET_TYPE_DEMOTE, "write": None, "account_name": "E"}],
}}
plan = g.build_write_plan(fake_report)
check("plan 自动集含 fill_blank/priority_mismatch/type_promote 共 3", plan["counts"]["auto"] == 3)
check("plan match 不进自动集", all(x["account_name"] != "A" for x in plan["auto"]))
check("plan 人工降级 1 条", plan["counts"]["manual_demote"] == 1)
check("plan 降级动作=降 prospecting + opt-out",
      plan["manual_demote"][0]["write"] == {"type": "prospecting", "sequence_opt_out": True})

# ── dead 保护:现有=dead 一律不动 ──────────────────────
r_dead = g.reconcile({"type": "core", "priority": "warm"},
                     {"type": "core", "priority": "dead"})
check("现有 dead → 桶 skip_dead", r_dead["bucket"] == g.BUCKET_SKIP_DEAD)
check("现有 dead → 不写", r_dead["write"] is None)
check("skip_dead 不在 AUTO 桶", g.BUCKET_SKIP_DEAD not in g.AUTO_BUCKETS)
check("现有 dead + type 也变 → 仍 skip(dead 保护优先)",
      g.reconcile({"type": "prospecting", "priority": "cold"},
                  {"type": "core", "priority": "dead"})["bucket"] == g.BUCKET_SKIP_DEAD)

print("\n" + ("❌ 失败: " + str(fails) if fails else "✅ 全绿"))
sys.exit(1 if fails else 0)
