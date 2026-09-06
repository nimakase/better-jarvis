"""
潜客生成编排（Prospect generation orchestration）

把确定性逻辑（选节点、推进树）从大模型手里收回到代码：
  - select_node()      读 prospect_tree，选出当天的 (产品类目, 区域) 组合
  - mark_node_done()   跑完写回进度（候选直接进 pipeline.enrich_records，无中间组装层）

大模型只负责「找真实合格公司 + 写清它造什么用什么料」
（见 intel/prospect_generation_prompt.md）。契约见 intel/prospect_pipeline_contract.md。

**v0.4：本包与信号库彻底解耦，不再 import signal_library。**
潜客名单服务 **0→1 大范围开发**：每天一批新公司去建立关系，周期以月计。
而信号是几周时效、且是赛道级/元件级的——一整个赛道的公司拿到同一个分数，
在一批候选内部几乎没有区分度，只会让不同日子的名单互相不可比。节奏和粒度都不匹配。
信号的真实用途是**存量**判断（手里的货要不要压、回头找买过某元件的老客户），
走情报侧的市场情报日报，与这条线无关。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


# ────────────────────────── 选节点（纯确定性：只读树，不受任何外部信号影响） ──────────────────────────

def _collect_leaves(node: dict) -> list[dict]:
    children = node.get("children")
    if not children:
        return [node]
    out: list[dict] = []
    for c in children:
        out += _collect_leaves(c)
    return out


def _remaining_regions(leaf: dict) -> list[str]:
    """该类目还没跑的区域，按 leaf.regions 声明顺序。

    2026-08-14：除 done_regions 外也扣掉 skipped_regions——后者是人主动跳过的
    （见 skip_node），不算真的跑完，但也不该再被 select_node() 选中。这一处改完，
    _candidates()/_pick()/select_node() 全部自动生效，不用分别改。
    """
    done = set(leaf.get("done_regions") or [])
    skipped = set(leaf.get("skipped_regions") or [])
    return [r for r in (leaf.get("regions") or []) if r not in done and r not in skipped]


def _candidates(tree: dict) -> list[tuple[dict, dict]]:
    """返回 [(leaf, top_sector), ...]，按 DFS 文件顺序，仅【还有区域没跑完】的叶子。

    v2.0 起判据从 leaf.status=='pending' 改为「还有剩余区域」——因为节点是
    (类目, 区域) 组合，一个类目要跑 3 次才算完。
    """
    pairs: list[tuple[dict, dict]] = []
    for sec in tree.get("sectors", []):
        for leaf in _collect_leaves(sec):
            if _remaining_regions(leaf):
                pairs.append((leaf, sec))
    return pairs


def _pick(pairs: list[tuple[dict, dict]],
         forced: Optional[dict] = None) -> Optional[tuple[dict, dict]]:
    """forced（可选）：{"leaf_id":..., "region": ...或None} —— 见 reset_node(force_next=True)。
    有 forced 且它指向的 (leaf,region) 现在确实还在候选池里，就无条件选它，
    优先级压过下面原有三层——这是"重跑某个特定节点"要的"下一次一定是它"，不是
    "它也进入正常排队"。forced 指向的东西已经不在候选池（比如region已经不对/
    已经被别的方式处理掉）就静默忽略，落回原有逻辑，不报错、不阻断。
    """
    if not pairs:
        return None
    if forced and forced.get("leaf_id"):
        want_region = forced.get("region")
        for p in pairs:
            leaf = p[0]
            if leaf.get("id") != forced["leaf_id"]:
                continue
            remaining = _remaining_regions(leaf)
            if want_region is None or want_region in remaining:
                return p
            break  # 找到了这个 leaf 但目标区域已经不在候选里，不用继续扫
    # 1) 【最优先】已经开跑但还没跑完三个区域的类目——把它跑完再换下一个。
    #    这是"同一类目连跑三天三区域"的实现：目的是拿到同类目、同口径的区域对照
    #    （欧洲 45 家 / 美洲 28 家 / 东南亚 9 家），从而看出区域优势是否分赛道。
    #    若允许中途跳走，对照就散了，只剩一堆拼不起来的马赛克。
    started = [p for p in pairs if p[0].get("done_regions")]
    if started:
        return started[0]
    # 2) 优先顶层 sector 为 partial 的（在已开工的赛道继续深挖）
    partial = [p for p in pairs if p[1].get("status") == "partial"]
    pool = partial or pairs
    # 3) 仍并列 → DFS 文件顺序第一个
    return pool[0]


def select_node(tree_path: str | Path) -> Optional[dict]:
    """选出当天节点 = (产品类目, **单个区域**)。全部完成时返回 None。

    v2.0 起一次只发一个区域。原因：一次生成的预算和注意力有限，让模型在一次输出里
    兼顾三个区域必然厚此薄彼（实际表现是把力气全花在第一个区域，后两个根本不看，
    而且不报错）。这是结构性的，改提示词解决不了——所以把「一次搜索内的分配问题」
    改成「跨天的调度问题」，每次只专注一个区域，挖透它。

    返回里带 naics_codes 与人话区域名（取自树顶层 regions_meta）——
    NAICS 码框定「这个类目到底指哪些成品」（v4.0 起从 HS 码换成 NAICS 码，见
    prospect_tree.json 顶层 note），人话区域名避免模型把 "SEA" 猜成别的东西。
    """
    tree = json.loads(Path(tree_path).read_text(encoding="utf-8"))
    pairs = _candidates(tree)
    forced = tree.get("forced_next")
    chosen = _pick(pairs, forced=forced)
    if chosen is None:
        return None
    leaf, sec = chosen
    meta = tree.get("regions_meta") or {}
    remaining = _remaining_regions(leaf)
    # forced_next 指定了具体区域、且这个区域确实还在候选里，就用它；否则照旧取
    # remaining[0]（forced 只点了 leaf_id、没点具体 region 时也走这条默认路径）。
    if forced and forced.get("leaf_id") == leaf.get("id") and forced.get("region") in remaining:
        region = forced["region"]
    else:
        region = remaining[0]

    # 下一个节点：本类目还有剩余区域就是它自己的下一个区域，否则换类目
    if len(remaining) > 1:
        nxt_label = f"{leaf.get('label')}（{meta.get(remaining[1], remaining[1])}）"
    else:
        rest = [p for p in pairs if p[0] is not leaf]
        nxt = _pick(rest)
        nxt_label = nxt[0].get("label") if nxt else None

    # 剩余节点数按 (类目, 区域) 组合算，不是按类目算
    remaining_combos = sum(len(_remaining_regions(p[0])) for p in pairs) - 1

    return {
        "id": leaf.get("id"),
        "label": leaf.get("label"),
        "region": region,                                  # 本次唯一的区域
        "region_label": meta.get(region, region),
        "node_key": f"{leaf.get('id')}:{region}",          # 落盘/记账用的唯一键
        "regions": leaf.get("regions", []),                # 该类目全部区域（参考）
        "done_regions": list(leaf.get("done_regions") or []),
        "naics_codes": leaf.get("naics_codes", []),
        "label_en": leaf.get("label_en") or leaf.get("label"),
        "max_rounds": leaf.get("max_rounds"),
        "sector_id": sec.get("id"),
        "sector_label": sec.get("label"),
        # v0.4 起不再有 node_sectors——它是【查信号用的赛道键】，随意向打分一起移除。
        # 树现在与信号库没有任何关联，选节点是纯粹的确定性推进。
        "remaining_pending": remaining_combos,
        "next_label": nxt_label,
    }


def mark_node_done(tree_path: str | Path, leaf_id: str,
                   region: Optional[str] = None) -> bool:
    """跑完一个 (类目, 区域) 后把该区域记进 done_regions 并写回树。

    三个区域都跑完，该类目 leaf.status 才转 done；再据此重算赛道状态
    （全部叶子 done→done，部分 done→partial）。

    region=None 时视为「整个类目一次性完成」（把所有区域都标掉）——只为兼容
    老调用方；正常路径都应该传 region。
    写的是 jarvis 自己的树副本（见 workflow_defs 路径解析），不影响外部数据源。
    """
    p = Path(tree_path)
    tree = json.loads(p.read_text(encoding="utf-8"))
    found = False
    for sec in tree.get("sectors", []):
        leaves = _collect_leaves(sec)
        for leaf in leaves:
            if leaf.get("id") != leaf_id:
                continue
            found = True
            regions = leaf.get("regions") or []
            done = list(leaf.get("done_regions") or [])
            if region is None:
                done = list(regions)
            elif region not in done:
                done.append(region)
            leaf["done_regions"] = done
            # 全部区域跑完才算这个类目完成
            leaf["status"] = "done" if all(r in done for r in regions) else "pending"
        # 仅当 sector 真有子节点（不是它自己当叶子）时才重算赛道状态
        if sec.get("children"):
            if all(l.get("status") == "done" for l in leaves):
                sec["status"] = "done"
            elif any(l.get("done_regions") for l in leaves):
                sec["status"] = "partial"
    if found:
        # 2026-08-14：这个 (leaf,region) 真的跑完了——如果它正好是 reset_node(
        # force_next=True) 排的队，插队任务已经完成，把 forced_next 花掉，
        # 免得它一直粘在树上、下次莫名其妙又被插队一次。
        forced = tree.get("forced_next")
        if forced and forced.get("leaf_id") == leaf_id and (
                forced.get("region") is None or forced.get("region") == region):
            del tree["forced_next"]
        p.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    return found


def reset_node(tree_path: str | Path, leaf_id: str,
               region: Optional[str] = None, *, force_next: bool = False) -> bool:
    """清空一个 (类目, 区域) 的完成/跳过记录，让它重新变成【待跑】。

    覆盖两个需求：
      - 「清空节点记录」：region=None 清掉这个类目【全部】区域的 done_regions +
        skipped_regions 记录（从头再来）；给具体 region 就只清那一个。
      - 「对特定节点重跑」：force_next=True 时额外把它设成 forced_next——
        _pick() 认这个标记，保证【下一次】select_node() 就选中它，不用等
        正常排队轮到（树里剩几百个待跑组合时，正常排队可能要很久才轮到）。

    ⚠️ 这只清树里的进度记账，**不撤销任何已经发生的副作用**——如果这个节点当初
    真的跑完过，候选很可能已经写进过 xlsx /「今日名单」，重跑不会撤销那些，
    只是让它有机会重新产出一批新的（新旧候选可能有重叠，这是预期行为）。
    """
    p = Path(tree_path)
    tree = json.loads(p.read_text(encoding="utf-8"))
    found = False
    for sec in tree.get("sectors", []):
        leaves = _collect_leaves(sec)
        for leaf in leaves:
            if leaf.get("id") != leaf_id:
                continue
            found = True
            regions = leaf.get("regions") or []
            targets = {region} if region else set(regions)
            done = set(leaf.get("done_regions") or []) - targets
            skipped = set(leaf.get("skipped_regions") or []) - targets
            leaf["done_regions"] = list(done)
            leaf["skipped_regions"] = list(skipped)
            notes = leaf.get("skip_reasons") or {}
            for t in targets:
                notes.pop(t, None)
            if notes:
                leaf["skip_reasons"] = notes
            elif "skip_reasons" in leaf:
                del leaf["skip_reasons"]
            leaf["status"] = "done" if regions and all(r in done for r in regions) else "pending"
        if sec.get("children"):
            if all(l.get("status") == "done" for l in leaves):
                sec["status"] = "done"
            elif any(l.get("done_regions") for l in leaves):
                sec["status"] = "partial"
            else:
                sec["status"] = "pending"
    if found:
        if force_next:
            tree["forced_next"] = {"leaf_id": leaf_id, "region": region}
        p.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    return found


def clear_forced_next(tree_path: str | Path) -> bool:
    """取消当前的 forced_next（改主意了，不想再让某个节点插队）。"""
    p = Path(tree_path)
    tree = json.loads(p.read_text(encoding="utf-8"))
    if "forced_next" in tree:
        del tree["forced_next"]
        p.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    return False


def skip_node(tree_path: str | Path, leaf_id: str,
             region: Optional[str] = None, reason: str = "") -> bool:
    """把一个 (类目, 区域) 标成【跳过】——不占轮次、不产出候选，但**不算已完成**。

    背景：2026-08-14 用户反馈"每次覚得某个节点不值得跑，都是自己写个脚本改树
    跳过"——那个脚本每次现造，没有正式入口，也没留下"为什么跳过""能不能撤销"
    的痕迹。这个函数就是把那个临时脚本收编成一个正式、幂等、可撤销（见
    unskip_node）的操作，写法照抄 mark_node_done 的骨架，但记进独立的
    skipped_regions 字段——跟 done_regions 分开记账，以后想搞清楚"这个类目
    是真跑完了还是当时被嫌弃跳过的"，两个字段一看就分得清，不会混在一起。

    region=None 时跳过这个类目【当前所有还没跑完的区域】（不动已经 done 的）。
    reason 可选，落进 skip_reasons（{区域: 理由} 字典），纯给人看，不影响任何判断逻辑。
    """
    p = Path(tree_path)
    tree = json.loads(p.read_text(encoding="utf-8"))
    found = False
    for sec in tree.get("sectors", []):
        for leaf in _collect_leaves(sec):
            if leaf.get("id") != leaf_id:
                continue
            found = True
            targets = [region] if region else _remaining_regions(leaf)
            skipped = set(leaf.get("skipped_regions") or [])
            skipped.update(t for t in targets if t)
            leaf["skipped_regions"] = list(skipped)
            if reason:
                notes = leaf.setdefault("skip_reasons", {})
                for t in targets:
                    if t:
                        notes[t] = reason
    if found:
        p.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    return found


def unskip_node(tree_path: str | Path, leaf_id: str,
                region: Optional[str] = None) -> bool:
    """撤销跳过——把 region(s) 从 skipped_regions 挪出来，重新回到候选池，
    下次 select_node() 就可能选中它。region=None 撤销这个类目全部已跳过区域。
    """
    p = Path(tree_path)
    tree = json.loads(p.read_text(encoding="utf-8"))
    found = False
    for sec in tree.get("sectors", []):
        for leaf in _collect_leaves(sec):
            if leaf.get("id") != leaf_id:
                continue
            found = True
            skipped = set(leaf.get("skipped_regions") or [])
            targets = [region] if region else list(skipped)
            skipped.difference_update(t for t in targets if t)
            leaf["skipped_regions"] = list(skipped)
            notes = leaf.get("skip_reasons") or {}
            for t in targets:
                notes.pop(t, None)
            if notes:
                leaf["skip_reasons"] = notes
            elif "skip_reasons" in leaf:
                del leaf["skip_reasons"]
    if found:
        p.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    return found


def tree_status(tree_path: str | Path, upcoming: int = 5) -> dict:
    """给人看的树状态摘要：三种 (类目,区域) 组合计数（已完成/已跳过/待跑），
    以及【接下来 upcoming 个会被 select_node() 依次选中的节点】——这是"可读、
    可选"的核心：在某个节点真正被跑到之前，就能先看到它排在队列里，决定要不
    要 skip_node() 掉，而不是等它已经在跑了才后悔。

    "接下来会选中谁"是在【内存里的树副本】上反复调用 _candidates()/_pick()、
    每选中一个就模拟"标它跑完"来推进——不读写磁盘、不影响真实树，只是借用
    select_node() 同一套确定性选择逻辑做预览。
    """
    tree = json.loads(Path(tree_path).read_text(encoding="utf-8"))
    meta = tree.get("regions_meta") or {}

    total = done = skipped = 0
    for sec in tree.get("sectors", []):
        for leaf in _collect_leaves(sec):
            regions = set(leaf.get("regions") or [])
            total += len(regions)
            done += len(set(leaf.get("done_regions") or []) & regions)
            skipped += len(set(leaf.get("skipped_regions") or []) & regions)

    import copy
    sim_tree = copy.deepcopy(tree)
    upcoming_list: list[dict] = []
    for _ in range(max(0, upcoming)):
        pairs = _candidates(sim_tree)
        forced = sim_tree.get("forced_next")
        chosen = _pick(pairs, forced=forced)
        if chosen is None:
            break
        leaf, sec = chosen
        remaining = _remaining_regions(leaf)
        if forced and forced.get("leaf_id") == leaf.get("id") and forced.get("region") in remaining:
            region = forced["region"]
        else:
            region = remaining[0]
        upcoming_list.append({
            "leaf_id": leaf.get("id"),
            "label": leaf.get("label"),
            "region": region,
            "region_label": meta.get(region, region),
        })
        leaf.setdefault("done_regions", [])
        leaf["done_regions"].append(region)   # 只改内存副本，模拟"这个组合跑完了"以推进下一次选择

    return {
        "total_combos": total,
        "done_combos": done,
        "skipped_combos": skipped,
        "pending_combos": total - done - skipped,
        "upcoming": upcoming_list,
        "forced_next": tree.get("forced_next"),   # 有值=有节点被 reset_node(force_next=True) 插队
    }


# 【v0.4：意向打分已整条移除】原先这里有 attach_intent()，用信号库给每条候选打
# intent_score + 信号标记。移除的理由不是它写得不对，而是它**服务错了场景**：
# 潜客名单服务 0→1 大范围开发（周期以月计），信号服务存量决策（时效几周、赛道级粒度）。
# 【机会轨也早已迁走】（v0.2）信号点名的公司现在是日报的「点名公司（金线索）」板块。
#
# 【v0.5：assemble() 也删了】它最后只剩给每条打一个 record["track"]="coverage"
# 标记——全仓库没有任何读它的地方（投递用的 track 是另一个字符串参数）。
# 生成层产出的候选（category / confidence / evidence / components / contact_rationale）
# 现在直接进 pipeline.enrich_records，不再有中间组装层。
