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
    """该类目还没跑的区域，按 leaf.regions 声明顺序。"""
    done = set(leaf.get("done_regions") or [])
    return [r for r in (leaf.get("regions") or []) if r not in done]


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


def _pick(pairs: list[tuple[dict, dict]]) -> Optional[tuple[dict, dict]]:
    if not pairs:
        return None
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

    返回里带 hs_codes 与人话区域名（取自树顶层 regions_meta）——
    HS 码框定「这个类目到底指哪些成品」，人话区域名避免模型把 "SEA" 猜成别的东西。
    """
    tree = json.loads(Path(tree_path).read_text(encoding="utf-8"))
    pairs = _candidates(tree)
    chosen = _pick(pairs)
    if chosen is None:
        return None
    leaf, sec = chosen
    meta = tree.get("regions_meta") or {}
    remaining = _remaining_regions(leaf)
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
        "hs_codes": leaf.get("hs_codes", []),
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
        p.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    return found


# 【v0.4：意向打分已整条移除】原先这里有 attach_intent()，用信号库给每条候选打
# intent_score + 信号标记。移除的理由不是它写得不对，而是它**服务错了场景**：
# 潜客名单服务 0→1 大范围开发（周期以月计），信号服务存量决策（时效几周、赛道级粒度）。
# 【机会轨也早已迁走】（v0.2）信号点名的公司现在是日报的「点名公司（金线索）」板块。
#
# 【v0.5：assemble() 也删了】它最后只剩给每条打一个 record["track"]="coverage"
# 标记——全仓库没有任何读它的地方（投递用的 track 是另一个字符串参数）。
# 生成层产出的候选（category / confidence / evidence / components / contact_rationale）
# 现在直接进 pipeline.enrich_records，不再有中间组装层。
