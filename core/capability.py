"""
core/capability.py — 能力索引（「这件事我已经能做了吗」的统一入口）

病根：能力清单散在四处——registry(工具)、workflow_registry(工作流)、
reports(报告类型)、scheduler(定时任务)——没有统一入口，造工具前无从查重，
这是重复造轮子的根因。

本模块把四处聚合成一份统一清单，并提供轻量检索：
  - gather()：全量能力清单 [{kind, name, description, detail}]
  - search(query, top_k)：按 query 找最相关的已有能力（含相关度分）
  - overview()：人/模型可读的能力总览

检索用【确定性的词面匹配】（中文二元组 + 英文词），不依赖嵌入模型：
  - 造工具查重的场景里，请求和工具描述用词高度同域（都在讲同一件业务），
    词面重叠已有很强判别力；
  - 确定性 → 可单测、离线可跑、零启动成本。嵌入语义召回留给 L4/后续增强。

先查后建闸（tool_builder.create_tool 调 check_duplicate）：
  命中高相似 → 返回提示，要求显式说明差异后才继续新建。
  优先级：复用 > 扩展 > 组合 > 新建。
"""
from __future__ import annotations

import re
from typing import Optional

_WORD = re.compile(r"[a-zA-Z0-9_]+")
_CJK = re.compile(r"[一-鿿]")

# 汉语高频虚词/泛词——出现在几乎所有描述里，参与匹配只会制造假相似
_STOP_BIGRAMS = {
    "一个", "工具", "用户", "使用", "调用", "生成", "返回", "支持", "可以",
    "需要", "进行", "或者", "以及", "的时", "时候", "查询", "信息", "获取",
}


def _tokens(text: str) -> set[str]:
    """中文按二元组、英文按词切分（去停用）。"""
    text = (text or "").lower()
    toks = set(_WORD.findall(text))
    cjk = _CJK.findall(text)
    cjk_str = "".join(cjk)
    for i in range(len(cjk_str) - 1):
        bg = cjk_str[i:i + 2]
        if bg not in _STOP_BIGRAMS:
            toks.add(bg)
    return toks


def _score(query_toks: set[str], target_toks: set[str]) -> float:
    """非对称重叠率：query 的词有多大比例出现在 target 里（查重视角）。"""
    if not query_toks or not target_toks:
        return 0.0
    hit = len(query_toks & target_toks)
    return hit / len(query_toks)


# ── 聚合 ──────────────────────────────────────────────────────────────────────

def gather() -> list[dict]:
    """聚合全部已有能力。每项：{kind, name, description}。各源失败单独跳过。"""
    out: list[dict] = []

    # 1) 工具（registry：连接器 + 元工具 + 已激活技能）
    try:
        from core import registry
        for s in registry.iter_specs():
            out.append({"kind": "tool" if s.origin == "builtin" else "skill",
                        "name": s.name, "description": s.description})
    except Exception:
        pass

    # 2) 未激活的技能（草稿/试用也算「已有」，查重时必须看见）
    try:
        from core.tool_builder import list_skills_info
        seen = {c["name"] for c in out}
        for sk in list_skills_info():
            tool_name = sk.get("tool_name") or sk.get("name")
            if tool_name not in seen:
                out.append({"kind": f"skill({sk.get('status', '?')})",
                            "name": tool_name,
                            "description": sk.get("description", "")})
    except Exception:
        pass

    # 3) 工作流
    try:
        from core import workflow_registry
        for wf in workflow_registry.list_workflows():
            out.append({"kind": "workflow", "name": wf.get("id") or wf.get("name", ""),
                        "description": f"{wf.get('name', '')} {wf.get('description', '')}"})
    except Exception:
        pass

    # 4) 报告类型
    try:
        from core import reports
        for rt in reports.list_types():
            out.append({"kind": "report", "name": rt.get("type_id") or rt.get("id", ""),
                        "description": f"{rt.get('name', '')} {rt.get('description', '')}"})
    except Exception:
        pass

    # 5) 已发现的 MCP 工具（只读缓存快照，不在这里触发新连接——零配置时天然为空，
    #    对现有查重行为零影响；见 core/mcp_discovery.py 任务 #6）
    try:
        from core import mcp_discovery
        for server, result in mcp_discovery.cached_snapshot().items():
            if not result.get("ok"):
                continue
            for t in result.get("tools", []):
                out.append({"kind": "mcp_tool(未接入)", "name": f"{server}.{t['name']}",
                            "description": t.get("description", "")})
    except Exception:
        pass

    return out


# ── 检索 ──────────────────────────────────────────────────────────────────────

def search(query: str, top_k: int = 5, min_score: float = 0.15) -> list[dict]:
    """找与 query 最相关的已有能力。返回 [{kind, name, description, score}]。"""
    q = _tokens(query)
    scored = []
    for cap in gather():
        s = _score(q, _tokens(f"{cap['name']} {cap['description']}"))
        if s >= min_score:
            scored.append({**cap, "score": round(s, 3)})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:top_k]


def check_duplicate(request: str, threshold: float = 0.45) -> Optional[str]:
    """先查后建闸：request 若与已有能力高度重合，返回提示文本；否则 None。

    阈值取 0.45：请求里近半词面已被某个现有能力覆盖 → 大概率重复。
    """
    hits = [h for h in search(request, top_k=3) if h["score"] >= threshold]
    if not hits:
        return None
    lines = ["先别新建——以下已有能力与这个需求高度重合：", ""]
    for h in hits:
        lines.append(f"  [{h['kind']}] {h['name']}（相关度 {h['score']:.0%}）："
                     f"{h['description'][:80]}")
    lines += [
        "",
        "优先级：复用 > 扩展（edit_tool）> 组合调用 > 新建。",
        "若确认这些都不满足、必须新建，请把【与上述能力的具体差异】写进"
        " clarifications 参数（例如「differs: 现有 X 只做 A，本工具要做 B」）后重调"
        " create_tool——不说明差异不放行。",
    ]
    return "\n".join(lines)


def overview() -> str:
    """能力总览（回答「你能干什么」，也供渐进披露/自省用）。"""
    caps = gather()
    if not caps:
        return "能力清单为空。"
    by_kind: dict[str, list[dict]] = {}
    for c in caps:
        by_kind.setdefault(c["kind"], []).append(c)
    lines = [f"贾维斯能力索引（共 {len(caps)} 项）", ""]
    for kind, items in by_kind.items():
        lines.append(f"◆ {kind}（{len(items)}）")
        for c in items:
            desc = (c["description"] or "").replace("\n", " ")[:60]
            lines.append(f"  - {c['name']}：{desc}")
    return "\n".join(lines)
