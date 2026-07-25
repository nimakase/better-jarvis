"""
core/consolidation.py — 记忆巩固作业（⑭ · 从「在线声明」到「离线巩固」）

问题：此前长期记忆靠模型在【对话进行中】判断「值不值得记」——最差的时机，
标准是硬 prompt 写死的，必然漏记隐性偏好 + 记进噪音。
人不是这样记的：先全部进短期，离线时做巩固——重要的升长期，过时的修正，
其余衰减。transcript（history）和监督信号（signals）已把原料存齐，
本模块补上巩固作业本身。

一轮闭环：
  取材（近期对话 + 监督信号 + 现有档案）
    → 模型提「记忆操作」提案（跑在只读子 agent 里，spawn 承载）
    → 机械闸逐条过滤（证据必填 / 词面去重转确认 / 每轮限量 / 只软删）
    → 写回 L1 档案 → 产出复盘文档（人可审）

安全设计（与 self_review 同一哲学——护栏机械化，不靠模型自觉）：
  - add 必须带 evidence（对话原话出处），无证据一律打回；
  - 与现有事实词面高度重合的 add 自动降级为 confirm（防档案膨胀）；
  - 每轮最多 3 条新增（宁缺毋滥）；
  - 删除只有软删（supersede，可 restore），巩固作业永远拿不到硬删；
  - remember_fact 快捷通道保留：用户明确说「记住」仍即时生效。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from core import profile
from core import signals as _signals
from core.capability import _score, _tokens
from core.json_salvage import salvage_json_array

REPO = Path(__file__).resolve().parent.parent
DEFAULT_REVIEW_DIR = REPO / "docs" / "consolidation"

MAX_ADDS_PER_RUN = 3          # 每轮新增上限（宁缺毋滥）
DUP_THRESHOLD = 0.5           # add 与现有事实词面重合（双向取大）≥ 此值 → 降级为 confirm
MATERIAL_CHAR_CAP = 9000      # 喂给模型的对话材料上限

ModelFn = Callable[[str], Awaitable[str]]


# ── 取材 ──────────────────────────────────────────────────────────────────────

def gather_material(recent_messages: int = 60) -> dict:
    """近期对话（跨全部会话线）+ 监督信号 + 现有档案。任一源失败则该源为空。"""
    convo_lines: list[str] = []
    try:
        from core import history
        for conv in history.list_conversations():
            msgs = history.get_messages(conversation_id=conv["id"], limit=recent_messages)
            for m in msgs[-recent_messages:]:
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    convo_lines.append(f"[{m.get('role')}] {content.strip()}")
    except Exception:
        pass
    convo = "\n".join(convo_lines)
    if len(convo) > MATERIAL_CHAR_CAP:
        convo = convo[-MATERIAL_CHAR_CAP:]   # 保最近的

    try:
        sigs = _signals.recent(limit=30)
    except Exception:
        sigs = []

    return {
        "conversations": convo,
        "signals": sigs,
        "facts": profile.list_facts(),
    }


# ── 提示词 ────────────────────────────────────────────────────────────────────

_RULES = """巩固纪律（严格遵守）：
1) 只提【长期稳定】的事实/偏好/习惯：风险偏好、家庭成员、固定习惯、长期目标、
   关键日期、表达偏好（如「答案要短」「先给结论」）。一次性任务信息一律不记。
2) 敏感信息（证件号/卡号/健康状况等）不进档案——证件有保险箱，健康不该记。
3) 每条 add 必须带 evidence：用户的【原话引用】。没有原话支撑的推测不要提。
4) 监督信号（用户的纠正/放弃/重试）是最有价值的材料——从中提炼「用户希望
   我怎么做事」的偏好；被纠正过的旧事实要提 revise 或 supersede。
5) 现有档案里已覆盖的不要重复 add；表述过时的用 revise；不再成立的用 supersede。
6) 没有值得记的就输出 []。一轮最多提 5 条操作，宁缺毋滥。"""

_FORMAT = """只输出一个 JSON 数组（无其它文字），元素为记忆操作：
[
  {"op": "add",       "text": "事实内容（≤300字）", "evidence": "用户原话引用"},
  {"op": "confirm",   "id": 3},
  {"op": "revise",    "id": 3, "text": "修正后的表述", "evidence": "依据的原话"},
  {"op": "supersede", "id": 5, "reason": "为何不再成立"}
]"""


def build_prompt(material: dict) -> str:
    facts_block = "\n".join(
        f"  #{f['id']} {f['text']}（最近确认 {str(f.get('last_confirmed_at') or f['created_at'])[:10]}）"
        for f in material["facts"]) or "  （空）"
    sig_block = "\n".join(
        f"  [{s['kind']}] 用户说「{s['user_text']}」（此前贾维斯：{s['prev_excerpt'][:60]}…）"
        for s in material["signals"]) or "  （无）"
    return "\n\n".join([
        "你是贾维斯的记忆整理者。任务：复盘近期与用户 Ned 的交流，对【常驻用户档案"
        "（L1 长期记忆）】提出增/确认/修正/过时标记操作。",
        "## 现有档案（操作里的 id 指这里）\n" + facts_block,
        "## 监督信号（用户的纠正/放弃/重试——最高价值材料）\n" + sig_block,
        "## 近期对话摘录\n" + (material["conversations"] or "（无）"),
        "## " + _RULES,
        "## 输出格式\n" + _FORMAT,
    ])


# ── 解析与机械闸 ──────────────────────────────────────────────────────────────

def parse_ops(text: str) -> list[dict]:
    raw = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    data = salvage_json_array(raw)
    return [d for d in data if isinstance(d, dict) and d.get("op")]


def apply_ops(ops: list[dict]) -> list[dict]:
    """逐条过机械闸并执行。返回 [{op, verdict, detail}] 审计记录。"""
    outcomes: list[dict] = []
    adds_done = 0
    existing = profile.list_facts()

    for op in ops[:8]:   # 总量硬顶（防模型话多）
        kind = (op.get("op") or "").strip()

        if kind == "add":
            text = (op.get("text") or "").strip()
            evidence = (op.get("evidence") or "").strip()
            if not text:
                outcomes.append({"op": op, "verdict": "打回", "detail": "text 为空"})
                continue
            if not evidence:
                outcomes.append({"op": op, "verdict": "打回",
                                 "detail": "无 evidence（原话出处必填）"})
                continue
            if adds_done >= MAX_ADDS_PER_RUN:
                outcomes.append({"op": op, "verdict": "打回",
                                 "detail": f"本轮新增已达上限 {MAX_ADDS_PER_RUN}"})
                continue
            # 词面去重（双向取大——同义改写两个方向的覆盖率都该看）：
            # 与现有活跃事实高度重合 → 降级为 confirm
            toks = _tokens(text)

            def _sim(f):
                ft = _tokens(f["text"])
                return max(_score(toks, ft), _score(ft, toks))

            dup = next((f for f in existing if _sim(f) >= DUP_THRESHOLD), None)
            if dup:
                profile.confirm_fact(dup["id"])
                outcomes.append({"op": op, "verdict": "降级为确认",
                                 "detail": f"与 #{dup['id']}「{dup['text'][:30]}…」高度重合"})
                continue
            r = profile.add_fact(text, evidence=evidence)
            if r["ok"]:
                adds_done += 1
                existing = profile.list_facts()
            outcomes.append({"op": op, "verdict": "已新增" if r["ok"] else "打回",
                             "detail": r["message"]})

        elif kind == "confirm":
            r = profile.confirm_fact(int(op.get("id") or 0))
            outcomes.append({"op": op, "verdict": "已确认" if r["ok"] else "打回",
                             "detail": r["message"]})

        elif kind == "revise":
            fid = int(op.get("id") or 0)
            text = (op.get("text") or "").strip()
            if not text or not (op.get("evidence") or "").strip():
                outcomes.append({"op": op, "verdict": "打回",
                                 "detail": "revise 需要 text + evidence"})
                continue
            if not any(f["id"] == fid for f in existing):
                outcomes.append({"op": op, "verdict": "打回", "detail": f"#{fid} 不存在"})
                continue
            profile.update_fact(fid, text)
            profile.confirm_fact(fid)
            outcomes.append({"op": op, "verdict": "已修正", "detail": f"#{fid}"})

        elif kind == "supersede":
            fid = int(op.get("id") or 0)
            r = profile.supersede_fact(fid, reason=op.get("reason") or "")
            outcomes.append({"op": op, "verdict": "已标过时(软删)" if r["ok"] else "打回",
                             "detail": r["message"]})

        else:
            outcomes.append({"op": op, "verdict": "打回", "detail": f"未知操作 {kind!r}"})

    return outcomes


# ── 复盘文档 ──────────────────────────────────────────────────────────────────

def _write_review(rdir: Path, outcomes: list[dict], n_ops: int) -> Path:
    rdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    lines = [f"# 记忆巩固复盘 · {ts}", "",
             f"模型共提 {n_ops} 条操作，执行结果：", ""]
    for o in outcomes:
        op = o["op"]
        desc = op.get("text") or op.get("reason") or f"#{op.get('id')}"
        lines.append(f"- [{o['verdict']}] {op.get('op')}：{str(desc)[:80]} — {o['detail']}")
    if not outcomes:
        lines.append("- （无操作——近期没有值得巩固的内容）")
    lines += ["", "> 软删可用 profile.restore_fact(id) 恢复；档案可在设置面板人工增删。"]
    p = rdir / f"{ts}.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ── 主入口 ────────────────────────────────────────────────────────────────────

_AGENTIC_HINT = """
【可用工具】你运行在只读子 agent 里，可调 recall(query) 语义召回过往情节、
lookup_entity 查实体记忆，用于核对「这件事此前是否记过/怎么记的」。
查证后按输出格式给出 JSON 数组。"""


async def _default_model_fn(prompt: str) -> str:
    """默认：跑在只读子 agent 里（spawn 承载，隔离 + 白名单 + 预算）。"""
    from core.spawn import spawn
    res = await spawn(prompt + _AGENTIC_HINT, label="记忆巩固",
                      max_rounds=5, timeout_s=420, contract=False)
    return res.raw_text or ""


async def run_consolidation(model_fn: Optional[ModelFn] = None, *,
                            material: Optional[dict] = None,
                            review_dir: Optional[Path] = None) -> dict:
    """跑一轮巩固。model_fn/material 可注入（测试确定性）。"""
    mat = material if material is not None else gather_material()
    fn = model_fn or _default_model_fn
    text = await fn(build_prompt(mat))
    ops = parse_ops(text)
    outcomes = apply_ops(ops)
    review = _write_review(Path(review_dir or DEFAULT_REVIEW_DIR), outcomes, len(ops))
    return {
        "n_ops": len(ops),
        "applied": [o for o in outcomes if o["verdict"].startswith("已")],
        "rejected": [o for o in outcomes if o["verdict"] == "打回"],
        "demoted": [o for o in outcomes if o["verdict"] == "降级为确认"],
        "review_path": str(review),
    }
