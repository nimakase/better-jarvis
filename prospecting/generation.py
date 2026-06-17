"""
潜客生成编排（Prospect generation orchestration）

把确定性逻辑（选节点、附加意向、机会轨、组装）从大模型手里收回到代码：
  - select_node()            读 prospect_tree，按既定优先级选出当天节点（顺序不被信号改写）
  - attach_intent()          用信号库给每条候选打意向分 + 信号标记（覆盖轨）
  - build_opportunity_track()点名公司 → 机会轨 bonus（不占配额）
  - assemble()               候选(来自生成提示词) + 机会轨 → 交给 pipeline.enrich_records

大模型只负责「找真实合格公司 + 写理由」（见 intel/prospect_generation_prompt.md）。
契约见 intel/prospect_pipeline_contract.md。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from intel import signal_library as sl

# signal_type → 富名单里的人话短标记
TYPE_LABEL = {
    "pricing": "涨价", "lead_time": "交期", "shortage": "缺货", "oversupply": "过剩",
    "demand_shift": "需求下滑", "capacity": "产能", "layoff": "裁员", "eol_pcn": "EOL",
    "closure": "关厂", "m_and_a": "并购", "write_down": "减值", "policy": "政策",
}


# ────────────────────────── 选节点（确定性，contract：顺序不被信号改写） ──────────────────────────

def _collect_leaves(node: dict) -> list[dict]:
    children = node.get("children")
    if not children:
        return [node]
    out: list[dict] = []
    for c in children:
        out += _collect_leaves(c)
    return out


def _candidates(tree: dict) -> list[tuple[dict, dict]]:
    """返回 [(leaf, top_sector), ...]，按 DFS 文件顺序，仅 status==pending 的叶子。"""
    pairs: list[tuple[dict, dict]] = []
    for sec in tree.get("sectors", []):
        for leaf in _collect_leaves(sec):
            if leaf.get("status") == "pending":
                pairs.append((leaf, sec))
    return pairs


def _pick(pairs: list[tuple[dict, dict]]) -> Optional[tuple[dict, dict]]:
    if not pairs:
        return None
    # 1) 优先顶层 sector 为 partial 的（在已开工的赛道继续深挖）
    partial = [p for p in pairs if p[1].get("status") == "partial"]
    pool = partial or pairs
    # 2) 优先 regions[0] 以 EU 开头（英国出发、触达成本最低）
    eu = [p for p in pool if (p[0].get("regions") or [""])[0].startswith("EU")]
    pool = eu or pool
    # 3) 仍并列 → DFS 文件顺序第一个
    return pool[0]


def select_node(tree_path: str | Path) -> Optional[dict]:
    """选出当天节点。全部完成时返回 None。"""
    tree = json.loads(Path(tree_path).read_text(encoding="utf-8"))
    pairs = _candidates(tree)
    chosen = _pick(pairs)
    if chosen is None:
        return None
    leaf, sec = chosen
    # 下一节点（排除本次后再选一次）
    rest = [p for p in pairs if p[0] is not leaf]
    nxt = _pick(rest)
    return {
        "id": leaf.get("id"),
        "label": leaf.get("label"),
        "regions": leaf.get("regions", []),
        "sector_id": sec.get("id"),
        "node_sectors": [sec.get("id"), leaf.get("id")],
        "remaining_pending": len(rest),
        "next_label": (nxt[0].get("label") if nxt else None),
    }


def mark_node_done(tree_path: str | Path, leaf_id: str) -> bool:
    """跑完一个节点后把它标 done 并写回树，使下次 select 推进到下一个 pending。

    顺带重算所在顶层赛道状态：全部叶子 done→done，部分 done→partial。
    写的是 jarvis 自己的树副本（见 workflow_defs 路径解析），不影响外部数据源。
    """
    p = Path(tree_path)
    tree = json.loads(p.read_text(encoding="utf-8"))
    found = False
    for sec in tree.get("sectors", []):
        leaves = _collect_leaves(sec)
        for leaf in leaves:
            if leaf.get("id") == leaf_id:
                leaf["status"] = "done"
                found = True
        # 仅当 sector 真有子节点（不是它自己当叶子）时才重算赛道状态
        if sec.get("children"):
            if all(l.get("status") == "done" for l in leaves):
                sec["status"] = "done"
            elif any(l.get("status") == "done" for l in leaves):
                sec["status"] = "partial"
    if found:
        p.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    return found


# ────────────────────────── 附加意向（覆盖轨） ──────────────────────────

def _tags_and_sources(signals: list[dict]) -> tuple[list[str], list[str], list[int]]:
    tags, sources, ids = [], [], []
    for s in signals:
        lbl = TYPE_LABEL.get(s.get("signal_type"), s.get("signal_type"))
        if lbl not in tags:
            tags.append(lbl)
        url = s.get("source_url")
        if url and url not in sources:
            sources.append(url)
        ids.append(s.get("id"))
    return tags, sources, ids


def attach_intent(records: list[dict], node_sectors: list[str],
                  db_path: str | Path = sl.DEFAULT_DB) -> list[dict]:
    """给每条候选打意向分 + 信号标记。

    意向 = 节点赛道信号强度（全节点共享） + 该公司元件命中的广义信号强度（按公司变化）。
    """
    sector_signals = sl.query_for_node(node_sectors, components=None, db_path=db_path)
    sector_strength = sum(s["strength"] for s in sector_signals)

    for r in records:
        comps = r.get("components") or []
        comp_signals = sl.query_for_node([], components=comps, db_path=db_path) if comps else []
        intent = sector_strength + sum(s["strength"] for s in comp_signals)

        tags, sources, ids = _tags_and_sources(sector_signals + comp_signals)
        r["track"] = r.get("track", "coverage")
        r["intent_score"] = round(intent, 2)
        r["surplus_signals"] = tags
        r["sources"] = sources
        r["signal_ids"] = ids
    return records


# ────────────────────────── 机会轨（点名公司） ──────────────────────────

def build_opportunity_track(db_path: str | Path = sl.DEFAULT_DB,
                            min_implication: int = 3) -> list[dict]:
    """信号点名的具体公司 → 机会轨富候选（track=opportunity，不占 100 配额）。"""
    out: list[dict] = []
    for s in sl.company_pointed_signals(db_path=db_path, min_implication=min_implication):
        lbl = TYPE_LABEL.get(s.get("signal_type"), s.get("signal_type"))
        sev = s.get("severity") or 3
        out.append({
            "company_name": s.get("company_name"),
            "website": s.get("website") or "",
            "country": s.get("country") or "",
            "components": [],
            "contact_rationale": s.get("note") or f"信号点名：{lbl}",
            "track": "opportunity",
            "intent_score": round(sev * 2.0, 2),   # 点名信号给高基线
            "surplus_signals": [f"点名·{lbl}"],
            "sources": [],
            "signal_ids": [s.get("signal_id")],
        })
    return out


# ────────────────────────── 组装 ──────────────────────────

def assemble(node: dict, candidates: list[dict],
             db_path: str | Path = sl.DEFAULT_DB,
             with_opportunity: bool = True) -> list[dict]:
    """候选(覆盖轨) + 机会轨 → 一份待富化(enrich_records)的富记录列表。"""
    for c in candidates:
        c["track"] = "coverage"
    attach_intent(candidates, node["node_sectors"], db_path=db_path)
    records = list(candidates)
    if with_opportunity:
        records += build_opportunity_track(db_path=db_path)
    return records
