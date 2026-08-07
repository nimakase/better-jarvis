"""
core/insight.py — 通用再分析模块（任务 #21）

背景：用户的原始问题——"对于一个任务，假设说他去我的 CRM 系统里分析一个客户
的数据……输出的结果是一个表格。对于这个表格中的某些内容，他是否能够做到在
我没提的情况下给出一个建议或者一些看法。这可以说是一种对结果的再分析，而不
是只把结果抛给我。这有一个通用框架可以做到吗，有 agent 已经实现了吗？"

答案：有，这是"insight generation"（BI 工具/报表类 agent 里的常见模式）的
轻量版——不是重新生成结果，而是在已产出的结果之上加一层"这些数字有什么
值得你注意的地方"的再分析。本模块提供两条独立轨道：

  1. 确定性规则（analyze()）：零延迟、可测试、不需要调模型——缺失/异常值、
     占比过度集中、跟上次快照相比的显著变化。这几类规则不依赖任何具体业务
     领域知识，可以套在任意"扁平字典/表格摘要"数据上，这是本模块的默认路径。
  2. 模型辅助（analyze_with_model()，显式可选）：规则覆盖不到的开放式判断
     （"这几个数字放在一起说明什么"）。需要显式调用，不在 analyze() 里自动跑
     （避免每次再分析都产生一次模型调用的延迟/成本），失败降级为空，不阻断
     调用方主流程。

刻意不做的事：不内置任何具体业务规则（如"HubSpot 账户超过 N 天未跟进算冷"）——
那是业务知识，应该由调用方提供（如 core/group_memory 里记的业务规则），这里
只提供"怎么发现值得说的东西"的通用机制，不假装懂任何具体业务。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SEVERITY_LEVELS = ("info", "notice", "warning")   # 由轻到重


@dataclass
class Insight:
    field: str
    message: str
    severity: str = "info"
    evidence: dict = field(default_factory=dict)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ── 确定性规则（零依赖，任意扁平字典都能跑）───────────────────────────────────

def rule_missing_or_zero(data: dict, fields: list[str],
                         label_map: Optional[dict] = None) -> list[Insight]:
    """指定字段若缺失/为 0/为空，各出一条 notice——常代表"这一步其实没跑到/
    没数据"，容易被当成"正常的 0"悄悄忽略掉。"""
    label_map = label_map or {}
    out = []
    for f in fields:
        v = data.get(f, None)
        if v is None or v == 0 or v == "" or v == []:
            out.append(Insight(f, f"{label_map.get(f, f)} 为空/0，可能这一步没有实际产出，值得确认",
                               "notice"))
    return out


def rule_threshold(data: dict, field_: str, *, gt: Optional[float] = None, lt: Optional[float] = None,
                   message: str = "", severity: str = "warning") -> Optional[Insight]:
    """字段值超过/低于给定阈值时出一条洞察。message 支持 {value} 占位符。"""
    v = _num(data.get(field_))
    if v is None:
        return None
    hit = (gt is not None and v > gt) or (lt is not None and v < lt)
    if not hit:
        return None
    msg = message.format(value=v) if message else f"{field_} = {v}，超出给定阈值"
    return Insight(field_, msg, severity)


def rule_share_dominance(data: dict, breakdown_field: str, *, threshold: float = 0.7,
                         severity: str = "notice") -> Optional[Insight]:
    """breakdown_field 指向一个 {类别: 数量} 字典时，若某一类占比超过 threshold，
    提示"这批结果高度集中在某一类"（只负责发现、不下判断——可能意味着规则或
    数据源有偏，也可能就是事实）。"""
    breakdown = data.get(breakdown_field)
    if not isinstance(breakdown, dict) or not breakdown:
        return None
    total = sum(v for v in breakdown.values() if isinstance(v, (int, float)))
    if total <= 0:
        return None
    top_key, top_val = max(breakdown.items(),
                           key=lambda kv: kv[1] if isinstance(kv[1], (int, float)) else 0)
    share = top_val / total
    if share < threshold:
        return None
    return Insight(breakdown_field,
                   f"{breakdown_field} 里「{top_key}」占比 {share:.0%}，高度集中在这一类",
                   severity, evidence={"breakdown": breakdown})


def rule_delta(data: dict, prev_data: Optional[dict], field_: str, *, pct_threshold: float = 0.3,
              severity: str = "notice") -> Optional[Insight]:
    """跟上一次快照比，该字段变化幅度超过 pct_threshold 时提示显著变化。
    prev_data 为空（没有历史快照可比）时直接跳过，不瞎猜、不误报。"""
    if not prev_data:
        return None
    cur = _num(data.get(field_))
    prev = _num(prev_data.get(field_))
    if cur is None or prev is None:
        return None
    if prev == 0:
        if cur == 0:
            return None
        return Insight(field_, f"{field_} 从 0 变为 {cur}，是新出现的", severity)
    pct = (cur - prev) / abs(prev)
    if abs(pct) < pct_threshold:
        return None
    direction = "上升" if pct > 0 else "下降"
    return Insight(field_, f"{field_} 较上次{direction} {abs(pct):.0%}（{prev} → {cur}）", severity)


def analyze(data: dict, *, missing_fields: Optional[list[str]] = None,
           dominance_fields: Optional[list[str]] = None,
           prev_data: Optional[dict] = None, delta_fields: Optional[list[str]] = None,
           thresholds: Optional[list[dict]] = None) -> list[Insight]:
    """跑一遍通用规则集，按调用方给的字段清单/阈值配置生成洞察列表。
    所有参数都可选——不传等于"这类规则在这份数据上不适用/不检查"，不会替调用方
    瞎猜哪些字段有意义（那是业务知识，不该由这个通用模块臆断）。"""
    out: list[Insight] = []
    if missing_fields:
        out += rule_missing_or_zero(data, missing_fields)
    for f in dominance_fields or []:
        r = rule_share_dominance(data, f)
        if r:
            out.append(r)
    for f in delta_fields or []:
        r = rule_delta(data, prev_data, f)
        if r:
            out.append(r)
    for t in thresholds or []:
        r = rule_threshold(data, t["field"], gt=t.get("gt"), lt=t.get("lt"),
                           message=t.get("message", ""), severity=t.get("severity", "warning"))
        if r:
            out.append(r)
    return out


def render(insights: list[Insight]) -> str:
    """渲成可直接拼进消息的一段文字。空列表返回空串（没什么可说就不硬凑
    "一切正常"这种废话，沉默本身就是信息）。"""
    if not insights:
        return ""
    order = {s: i for i, s in enumerate(SEVERITY_LEVELS)}
    insights = sorted(insights, key=lambda x: -order.get(x.severity, 0))
    icon = {"info": "·", "notice": "⚠️", "warning": "🔴"}
    lines = ["【补充观察】（自动再分析，供参考，不代表结论）"]
    for ins in insights:
        lines.append(f"{icon.get(ins.severity, '·')} {ins.message}")
    return "\n".join(lines)


async def analyze_with_model(data: dict, context: str = "", *, model: Optional[str] = None) -> list[Insight]:
    """开放式模型辅助洞察（规则覆盖不到的"这几个数字放一起说明什么"）。显式调用，
    不在 analyze() 里自动跑。失败/解析不出结构一律降级为空列表，不阻断调用方。"""
    import json

    import config
    from core.llm import get_client

    prompt = (
        "以下是一份结构化的分析结果摘要（JSON），请找出【最多 3 条】值得使用者注意"
        "但可能被忽略的观察或建议——不要重复摘要里已经写明的数字本身，只说"
        "「这些数字放在一起意味着什么」这类判断。每条一行，格式：\n"
        "严重度(info/notice/warning)|简短判断\n"
        "没有值得说的就只回复 NONE。\n\n"
        f"背景：{context or '（无额外背景）'}\n\n"
        f"数据：{json.dumps(data, ensure_ascii=False, default=str)[:3000]}"
    )
    try:
        client = get_client()
        resp = await client.chat.completions.create(
            model=model or config.CLAUDE_MODEL_LIGHT, max_tokens=400, timeout=30,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception:
        return []
    if not text or text.upper().strip() == "NONE":
        return []
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        if "|" not in line:
            continue
        sev, _, msg = line.partition("|")
        sev = sev.strip().lower()
        if sev not in SEVERITY_LEVELS:
            sev = "info"
        msg = msg.strip()
        if msg:
            out.append(Insight("_model", msg, sev))
    return out
