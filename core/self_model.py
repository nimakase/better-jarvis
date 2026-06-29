"""
core/self_model.py — 自我模型 / 代码边界的单一事实源（机器可读）

定义贾维斯哪些代码是「核心（PROTECTED · 地基，不自动迭代）」、
哪些是「周边（OPEN · 业务能力，可被自我迭代触碰）」。

设计原则（与用户决策对齐）：
- PROTECTED：框架与安全不变量。99% 不动；要改必须人工审核，
  且需附「改了会怎样（影响）+ 为什么改（动机）」，绝不自动应用。
- OPEN：具体业务能力。可被自我迭代修改，但必须「测试通过才自动生效 + 可回滚」。
- fail-safe：未被显式列入 OPEN 的一切，一律按 PROTECTED 处理。
  即「默认不可自动改」——新增文件天生受保护，必须显式开放。

本文件自身、以及 tests/ 都属于 PROTECTED：
  · 边界不能被自我迭代偷偷挪动；
  · 测试是「绿了就自动生效」的安全网，若可自改即可作弊，必须人工把关。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# —— 核心：框架与安全不变量（不自动迭代；要改须人工审核 + 影响 + 动机）——
PROTECTED: dict[str, str] = {
    # 边界与安全网自身
    "core/self_model.py":     "边界定义自身；移动边界 = 拆护栏，必须人工",
    "core/self_iteration.py": "自我迭代执行器；若可自改即可拆掉自己的护栏，必须人工",
    "tests/":                 "测试是自动生效的安全网；若可自改即可作弊，必须人工",
    # 框架与装配
    "config.py":            "配置与密钥载入",
    "main.py":              "装配入口；改坏即无法启动",
    "core/registry.py":     "工具注册中心，全系统单一事实来源",
    "core/controller.py":   "主控对话循环（大脑接线）",
    "core/context.py":      "会话管理（多会话隔离）",
    "core/results.py":      "结构化返回与带外动作通道（脱敏边界）",
    "core/safety.py":       "路径/文件名防穿越（安全边界本身）",
    "core/tool_builder.py": "工具自建系统 + code_review 人工闸门（元能力）",
    "core/memory.py":       "Fernet 加密 KV 基础设施 + 密钥管理",
    "core/workflow.py":     "工作流引擎（轨道，不许改）",
    "core/scheduler.py":    "定时调度基础设施",
    # 证件保险箱安全边界（物理上在 connectors/，逻辑上属核心）
    "connectors/vault.py":       "证件保险箱加密存储核心",
    "connectors/credentials.py": "证件工具（真实号绝不上云的边界）",
    "connectors/cred_ocr.py":    "证件本地 OCR（不上云）",
    # 传输层 + 证件揭示带外分发（安全管线）
    "web/":                 "传输层路由 + 证件揭示带外分发（安全管线）",
}

# —— 周边：具体业务能力（可自我迭代；测试绿才自动生效 + 可回滚）——
OPEN: dict[str, str] = {
    # 业务工具（connectors，均 @tool）
    "connectors/document.py":           "读文档",
    "connectors/doc_vault.py":          "文档保险箱",
    "connectors/profile_tools.py":      "写用户档案",
    "connectors/calendar_tools.py":     "日历工具",
    "connectors/calendar_providers.py": "日历数据源",
    "connectors/calendar_card.py":      "日历情报卡",
    "connectors/report_tools.py":       "报告工具",
    "connectors/workflow_tools.py":     "工作流工具",
    "connectors/delivery_control.py":   "投递控制",
    "connectors/memory_tools.py":       "（已中和的空模块）",
    # 业务逻辑层（core 里偏内容的）
    "core/reports.py":          "报告类型注册",
    "core/report_render.py":    "报告渲染",
    "core/intel_cards.py":      "情报台卡片",
    "core/calendar.py":         "内置日历业务逻辑",
    "core/delivery.py":         "投递业务逻辑",
    "core/availability.py":     "可用性/休假",
    "core/profile.py":          "用户档案业务规则",
    "core/history.py":          "对话存档业务规则",
    "core/workflow_registry.py":"工作流注册/运行记录",
    "core/skill_policy.py":     "技能策略",
    # 情报与潜客（目录整体开放）
    "intel/":       "情报层：信号库、工作流定义、领域 prompt/规格",
    "prospecting/": "HubSpot 潜客匹配/富化流水线",
    "skills/":      "运行时自建技能（已沙箱）",
    # 前端（开放但无测试覆盖——见 classify 注记）
    "frontend/":    "PWA 前端（注：无测试覆盖，自动迭代暂不应触碰）",
}


def _rel(path: str | Path) -> str:
    """归一化为「相对仓库根、posix 风格」的路径字符串。"""
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(REPO_ROOT)
        except ValueError:
            return Path(path).as_posix()  # 仓库外，原样返回
    return p.as_posix()


def _dir_match(rel: str, table: dict[str, str]) -> str | None:
    """目录前缀匹配，返回最长匹配的 key（如 'web/' 命中 'web/chat.py'）。"""
    best = None
    for key in table:
        if key.endswith("/") and (rel.startswith(key) or rel == key.rstrip("/")):
            if best is None or len(key) > len(best):
                best = key
    return best


def classify(path: str | Path) -> tuple[str, str]:
    """把一个路径归类为 'protected' 或 'open'，并给出原因。

    匹配优先级：精确文件 > 目录前缀；PROTECTED 优先于 OPEN；
    全不命中 → ('protected', fail-safe)。
    """
    rel = _rel(path)
    if rel in PROTECTED:
        return ("protected", PROTECTED[rel])
    if rel in OPEN:
        return ("open", OPEN[rel])
    prot = _dir_match(rel, PROTECTED)
    if prot:
        return ("protected", PROTECTED[prot])
    opn = _dir_match(rel, OPEN)
    if opn:
        return ("open", OPEN[opn])
    return ("protected", "未在 OPEN 白名单内 —— 默认保护（fail-safe）")


def is_writable_by_self_iteration(path: str | Path) -> bool:
    """自我迭代闭环是否可「自动」修改该路径——仅 OPEN 为真。

    PROTECTED（含未知路径）一律返回 False：要改只能走人工审核通道。
    """
    return classify(path)[0] == "open"


def to_markdown() -> str:
    """生成人类可读镜像（docs/SELF_MODEL.md 由此产出，避免与代码漂移）。"""
    lines = [
        "# 贾维斯自我模型 · 代码边界",
        "",
        "> 本文件由 `core/self_model.py` 自动生成，请勿手改。",
        "> 改边界 = 改 `core/self_model.py`（它自身受保护，须人工审核）。",
        "",
        "判据：**改了会动摇框架/安全的 = 核心（保护）；改了只影响某个业务好坏的 = 周边（开放）。**",
        "未列入「开放」的一切默认按「保护」处理（fail-safe）。",
        "",
        "## 🔒 PROTECTED（核心 · 不自动迭代；要改须人工 + 影响 + 动机）",
        "",
        "| 路径 | 职责 |",
        "|------|------|",
    ]
    for k, v in PROTECTED.items():
        lines.append(f"| `{k}` | {v} |")
    lines += [
        "",
        "## 🟢 OPEN（周边 · 可自我迭代；测试绿才自动生效 + 可回滚）",
        "",
        "| 路径 | 职责 |",
        "|------|------|",
    ]
    for k, v in OPEN.items():
        lines.append(f"| `{k}` | {v} |")
    lines += [
        "",
        "---",
        "未匹配任何条目的路径（如 `data/`、`deploy/`、`evals/`、新增文件）→ **默认 PROTECTED**。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(to_markdown())
