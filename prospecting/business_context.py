"""prospecting/business_context.py — 业务上下文「挂件」。

读 docs/业务逻辑与工作流程.md(缓存一次),供客户循环那几个 LLM 判断【按需带上】(判回复 / 读处置)。
⚠ 刻意【不进常驻 prompt】——常驻用户档案只放蒸馏的 3 条(见 scripts.seed_business_profile),
   这份全文只挂在需要精度的 pipeline 调用上,避免撑大每轮主对话。
文档是唯一真源:改文档 → 上下文自动更新,不在代码里复制一份。
"""
from __future__ import annotations

from pathlib import Path

_CACHE: str | None = None


def business_context(max_chars: int = 7000) -> str:
    """返回业务流程文档全文(缓存;截断到 max_chars)。读不到则返回空串,调用方自然降级。"""
    global _CACHE
    if _CACHE is None:
        try:
            p = Path(__file__).resolve().parent.parent / "docs" / "业务逻辑与工作流程.md"
            _CACHE = p.read_text(encoding="utf-8") if p.exists() else ""
        except Exception:
            _CACHE = ""
    return _CACHE[:max_chars]
