"""
core/consolidation.py — 记忆巩固作业（⑭ · 从「在线声明」到「离线巩固」；
2026-08-12 补：过程性记忆巩固，见文末「过程记忆巩固」一节）

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

2026-08-12 起，同一套闸门哲学复用到【过程性记忆】（core/procedures.py）：
取材换成 self_review 复盘文档 + 对话/信号，产物从"用户档案里的事实"换成
"遇到某类问题该怎么解决"的经验。两条巩固线共享 parse_ops/_write_review 等
底层工具，各自的 gather_material_*/build_prompt_*/apply_ops_* 互不干扰
（写入的表也不同：core_memory vs procedural_memory）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from core import profile
from core import procedures as _procedures
from core import signals as _signals
from core.capability import _score, _tokens
from core.json_salvage import salvage_json_array

REPO = Path(__file__).resolve().parent.parent
DEFAULT_REVIEW_DIR = REPO / "docs" / "consolidation"
DEFAULT_REVIEW_DIR_PROCEDURAL = REPO / "docs" / "consolidation_procedural"
DEFAULT_SELF_REVIEW_DIR = REPO / "docs" / "self_review"

MAX_ADDS_PER_RUN = 3          # 每轮新增上限（宁缺毋滥）
MAX_PROCEDURE_ADDS_PER_RUN = 3
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

def _write_review(rdir: Path, outcomes: list[dict], n_ops: int, *,
                  title: str = "记忆巩固复盘",
                  restore_hint: str = "profile.restore_fact(id)") -> Path:
    rdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    lines = [f"# {title} · {ts}", "",
             f"模型共提 {n_ops} 条操作，执行结果：", ""]
    for o in outcomes:
        op = o["op"]
        desc = op.get("text") or op.get("problem") or op.get("reason") or f"#{op.get('id')}"
        lines.append(f"- [{o['verdict']}] {op.get('op')}：{str(desc)[:80]} — {o['detail']}")
    if not outcomes:
        lines.append("- （无操作——近期没有值得巩固的内容）")
    lines += ["", f"> 软删可用 {restore_hint} 恢复；档案可在设置面板人工增删。"]
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


# ═══════════════════════════════════════════════════════════════════════════
# 过程记忆巩固（2026-08-12 · 步骤③）
#
# 对象从「用户的事实/偏好」换成「遇到某类问题该怎么解决」的经验，写入
# core/procedures.py（procedural_memory 表，与 core_memory 独立）。取材换成
# self_review 复盘文档（诊断出的真缺陷是怎么修的）+ 对话/监督信号；机械闸哲学
# 与上面 L1 事实巩固完全一致（证据必填 / 词面去重 / 限量 / 只软删）。
# ═══════════════════════════════════════════════════════════════════════════

def gather_material_procedural(recent_reviews: int = 5, recent_messages: int = 40) -> dict:
    """过程性记忆的取材：self_review 复盘文档 + 近期对话/信号（排错往返最有价值）
    + 现有过程记忆（供去重）。任一源失败则该源为空，不影响其余取材。"""
    review_lines: list[str] = []
    try:
        rdir = DEFAULT_SELF_REVIEW_DIR
        if rdir.exists():
            files = sorted(rdir.glob("*.md"), reverse=True)[:recent_reviews]
            for f in files:
                review_lines.append(f"## {f.name}\n{f.read_text(encoding='utf-8')[:3000]}")
    except Exception:
        pass

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
        convo = convo[-MATERIAL_CHAR_CAP:]

    try:
        sigs = _signals.recent(limit=30)
    except Exception:
        sigs = []

    return {
        "self_reviews": "\n\n".join(review_lines),
        "conversations": convo,
        "signals": sigs,
        "procedures": _procedures.list_procedures(),
    }


_PROCEDURAL_RULES = """过程记忆巩固纪律（严格遵守）：
1) 只提【可复用的解题方法】：某类问题(报错/卡点/需求)遇到时该怎么排查、怎么修、
   踩过什么坑要避开。一次性、与本项目结构无关的琐事不要提。
2) problem 要写清楚"什么场景/什么症状会触发这条经验"，method 要写清楚具体怎么做
   （步骤、要查的文件、要跑的命令、根因），让贾维斯下次遇到同类问题能直接照做。
3) 每条 add 必须带 evidence：这个方法来自哪次 self_review 复盘 / 哪次对话的原话或
   文件路径。没有依据的推测不要提。
4) 现有过程记忆里已覆盖同类问题的不要重复 add；有更好/更准确解法的用 revise；
   已不适用（比如代码已重构，此路不通了）的用 supersede。
5) 没有值得记的就输出 []。一轮最多提 5 条操作，宁缺毋滥。"""

_PROCEDURAL_FORMAT = """只输出一个 JSON 数组（无其它文字），元素为过程记忆操作：
[
  {"op": "add", "problem": "触发场景/症状（≤200字）", "method": "具体解法/步骤（≤800字）", "evidence": "依据（复盘文档名或对话原话）"},
  {"op": "confirm",   "id": 3},
  {"op": "revise",    "id": 3, "problem": "...", "method": "修正后的解法", "evidence": "..."},
  {"op": "supersede", "id": 5, "reason": "为何不再适用"}
]"""


def build_prompt_procedural(material: dict) -> str:
    procs_block = "\n".join(
        f"  #{p['id']} 问题：{p['problem'][:60]} | 解法：{p['method'][:60]}…"
        for p in material["procedures"]) or "  （空）"
    sig_block = "\n".join(
        f"  [{s['kind']}] 用户说「{s['user_text']}」（此前贾维斯：{s['prev_excerpt'][:60]}…）"
        for s in material["signals"]) or "  （无）"
    return "\n\n".join([
        "你是贾维斯的过程记忆整理者。任务：复盘近期的 self_review 复盘文档、对话与"
        "监督信号，从中提炼「遇到某类问题该怎么解决」的可复用经验，对【过程记忆库】"
        "提出增/确认/修正/过时标记操作。",
        "## 现有过程记忆（操作里的 id 指这里）\n" + procs_block,
        "## 近期 self_review 复盘文档（诊断出的真缺陷是怎么修的）\n"
            + (material.get("self_reviews") or "（无）"),
        "## 监督信号（用户的纠正/放弃/重试）\n" + sig_block,
        "## 近期对话摘录\n" + (material["conversations"] or "（无）"),
        "## " + _PROCEDURAL_RULES,
        "## 输出格式\n" + _PROCEDURAL_FORMAT,
    ])


def apply_ops_procedural(ops: list[dict]) -> list[dict]:
    """逐条过机械闸并执行（过程记忆版——闸门纪律与 apply_ops 一致，存取换成
    core.procedures）。"""
    outcomes: list[dict] = []
    adds_done = 0
    existing = _procedures.list_procedures()

    for op in ops[:8]:
        kind = (op.get("op") or "").strip()

        if kind == "add":
            problem = (op.get("problem") or "").strip()
            method = (op.get("method") or "").strip()
            evidence = (op.get("evidence") or "").strip()
            if not problem or not method:
                outcomes.append({"op": op, "verdict": "打回", "detail": "problem/method 为空"})
                continue
            if not evidence:
                outcomes.append({"op": op, "verdict": "打回",
                                 "detail": "无 evidence（依据必填）"})
                continue
            if adds_done >= MAX_PROCEDURE_ADDS_PER_RUN:
                outcomes.append({"op": op, "verdict": "打回",
                                 "detail": f"本轮新增已达上限 {MAX_PROCEDURE_ADDS_PER_RUN}"})
                continue
            toks = _tokens(problem)

            def _sim(p):
                pt = _tokens(p["problem"])
                return max(_score(toks, pt), _score(pt, toks))

            dup = next((p for p in existing if _sim(p) >= DUP_THRESHOLD), None)
            if dup:
                _procedures.confirm_procedure(dup["id"])
                outcomes.append({"op": op, "verdict": "降级为确认",
                                 "detail": f"与 #{dup['id']}「{dup['problem'][:30]}…」高度重合"})
                continue
            r = _procedures.add_procedure(problem, method, evidence=evidence)
            if r["ok"]:
                adds_done += 1
                existing = _procedures.list_procedures()
            outcomes.append({"op": op, "verdict": "已新增" if r["ok"] else "打回",
                             "detail": r["message"]})

        elif kind == "confirm":
            r = _procedures.confirm_procedure(int(op.get("id") or 0))
            outcomes.append({"op": op, "verdict": "已确认" if r["ok"] else "打回",
                             "detail": r["message"]})

        elif kind == "revise":
            pid = int(op.get("id") or 0)
            problem = (op.get("problem") or "").strip()
            method = (op.get("method") or "").strip()
            if not (problem or method) or not (op.get("evidence") or "").strip():
                outcomes.append({"op": op, "verdict": "打回",
                                 "detail": "revise 需要 problem/method 至少一项 + evidence"})
                continue
            if not any(p["id"] == pid for p in existing):
                outcomes.append({"op": op, "verdict": "打回", "detail": f"#{pid} 不存在"})
                continue
            _procedures.update_procedure(pid, problem=problem or None, method=method or None)
            _procedures.confirm_procedure(pid)
            outcomes.append({"op": op, "verdict": "已修正", "detail": f"#{pid}"})

        elif kind == "supersede":
            pid = int(op.get("id") or 0)
            r = _procedures.supersede_procedure(pid, reason=op.get("reason") or "")
            outcomes.append({"op": op, "verdict": "已标过时(软删)" if r["ok"] else "打回",
                             "detail": r["message"]})

        else:
            outcomes.append({"op": op, "verdict": "打回", "detail": f"未知操作 {kind!r}"})

    return outcomes


_AGENTIC_HINT_PROCEDURAL = """
【可用工具】你运行在只读子 agent 里，可调 recall(query) 语义召回过往情节、
lookup_entity 查实体记忆、读源码文件核实"这个方法现在是否还成立"。
查证后按输出格式给出 JSON 数组。"""


async def _default_model_fn_procedural(prompt: str) -> str:
    from core.spawn import spawn
    res = await spawn(prompt + _AGENTIC_HINT_PROCEDURAL, label="过程记忆巩固",
                      max_rounds=5, timeout_s=420, contract=False)
    return res.raw_text or ""


async def run_procedural_consolidation(model_fn: Optional[ModelFn] = None, *,
                                       material: Optional[dict] = None,
                                       review_dir: Optional[Path] = None) -> dict:
    """跑一轮【过程性记忆】巩固——与 run_consolidation(L1事实)同一套机械闸哲学，
    对象换成"解决问题的方法"。适合定时触发（如每周）或 self_review/排错后手动
    触发（用户说「把这次踩坑的经验记一下」）。"""
    mat = material if material is not None else gather_material_procedural()
    fn = model_fn or _default_model_fn_procedural
    text = await fn(build_prompt_procedural(mat))
    ops = parse_ops(text)
    outcomes = apply_ops_procedural(ops)
    review = _write_review(Path(review_dir or DEFAULT_REVIEW_DIR_PROCEDURAL), outcomes, len(ops),
                           title="过程记忆巩固复盘",
                           restore_hint="procedures.restore_procedure(id)")
    return {
        "n_ops": len(ops),
        "applied": [o for o in outcomes if o["verdict"].startswith("已")],
        "rejected": [o for o in outcomes if o["verdict"] == "打回"],
        "demoted": [o for o in outcomes if o["verdict"] == "降级为确认"],
        "review_path": str(review),
    }
