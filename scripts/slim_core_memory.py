"""
长期记忆瘦身（一次性迁移）—— 按 L1 准入标准收敛 core_memory。

做两件事，全程可回滚：
  ① 污染项软删（4 条）：BASE_DIR/prospect_tree 路径重复 3 份 + 已过期的树状态。
     这些是代码可推导 / 过期状态，本不该常驻。软删（supersede），可 restore。
  ② 降级到 L2 实体（7 条）：先 upsert 进 entities（事实不丢），再软删 L1 常驻位。
     低频事实（教育、长期目标/地理、家人、部署环境）不再每轮占 system prompt。

安全：每条按 (id, 关键词) 双重核对——当前该 id 的活跃事实文本必须包含关键词，
否则跳过并告警（防 id 漂移误删）。默认 DRY-RUN 只打印不改；确认无误后加 --apply 执行。

用法（项目根）：
    python scripts/slim_core_memory.py           # 预演，不改库
    python scripts/slim_core_memory.py --apply    # 真正执行
回滚：软删的可用 profile.restore_fact(id) 恢复；L2 实体可 entities 删。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import profile        # noqa: E402
from core import entities       # noqa: E402

APPLY = "--apply" in sys.argv

# ① 纯污染 → 软删，不进 L2。(id, 关键词, 软删原因)
PURGE = [
    (28, "prospect_tree.json 路径", "路径事实代码可推导且与 29/30 重复"),
    (29, "BASE_DIR =",              "BASE_DIR 代码可推导且重复"),
    (30, "BASE_DIR = Path",         "BASE_DIR 代码可推导且重复"),
    (31, "清空了 prospect 树历史",   "一次性过期状态（7-19 的临时备注）"),
]

# ② 降级 → 先 upsert 进 L2 实体，再软删 L1。
#    (id, 关键词, kind, name, fields, notes)
DEMOTE = [
    (4, "教育背景", "背景", "教育背景", {},
        "布里斯托大学 全球运营与供应链管理 硕士；澳门城市大学 国际酒店与旅游管理 本科"),
    (5, "猫名 Fore", "人物", "Fore", {"关系": "猫"}, "Ned 养的猫"),
    (6, "黄凯琳", "人物", "黄凯琳（Bronx）", {"关系": "伴侣"}, "Ned 的伴侣"),
    (7, "罗业军", "人物", "罗业军", {"关系": "父亲"}, "Ned 的父亲（母亲：余小兰）"),
    (8, "长期职业内核", "背景", "长期职业目标", {},
        "想进入并主导科技制造/硬件领域，追求实际经营权与决策权，厌恶大组织被动执行。"
        "受雇（运营→领导层）与自主创业两条路均开放。偏好市场化、开放文化，规避国企式环境。"),
    (9, "长期地理倾向", "背景", "长期地理倾向", {},
        "目前打算长期留深圳。移民/海外身份规划仅在未来真正有需求时启动，非当前目标。"),
    (26, "macmini", "背景", "部署环境", {"机器": "mac mini", "芯片": "M4", "内存": "16G"},
        "贾维斯部署在一台 M4 芯片、16G 内存的 mac mini 上"),
]


def _active_by_id() -> dict:
    return {f["id"]: f for f in profile.list_facts()}


def main() -> int:
    facts = _active_by_id()
    mode = "执行" if APPLY else "预演（不改库；加 --apply 才执行）"
    print(f"=== 长期记忆瘦身 · {mode} ===")
    print(f"当前活跃 {len(facts)} 条\n")

    def guard(fid, kw):
        f = facts.get(fid)
        if not f:
            print(f"  ⚠ [{fid}] 不存在或已软删，跳过")
            return None
        if kw not in f["text"]:
            print(f"  ⚠ [{fid}] 文本不含关键词「{kw}」（可能 id 漂移），跳过。实际：{f['text'][:40]}…")
            return None
        return f

    print("① 软删污染项：")
    purged = 0
    for fid, kw, reason in PURGE:
        f = guard(fid, kw)
        if not f:
            continue
        print(f"  ✓ [{fid}] {f['text'][:46]}…  ← {reason}")
        if APPLY:
            profile.supersede_fact(fid, reason=reason)
        purged += 1

    print("\n② 降级到 L2 实体（先存 entities 再软删 L1）：")
    demoted = 0
    for fid, kw, kind, name, fields, notes in DEMOTE:
        f = guard(fid, kw)
        if not f:
            continue
        print(f"  ✓ [{fid}] → L2 [{kind}] {name}")
        if APPLY:
            entities.upsert(kind, name, fields=fields, notes=notes)
            profile.supersede_fact(fid, reason=f"降级到 L2 实体：[{kind}] {name}")
        demoted += 1

    remaining = len(facts) - purged - demoted if APPLY else len(facts)
    print(f"\n小结：软删 {purged} 条 + 降级 {demoted} 条。")
    if APPLY:
        print(f"L1 活跃剩 {profile.count()} 条。回滚：profile.restore_fact(id)。")
    else:
        print("这是预演。确认无误后：python scripts/slim_core_memory.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
