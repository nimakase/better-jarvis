"""
core/chunking.py — 任务体量检测 + 自动切块扇出兜底（任务 #15）

背景：一则真实案例——某 agent 被要求翻译一整本书，自己判断任务太大，未经任何
护栏就在本地部署了一个小模型来完成任务（用户点评："这其实是一种没有设定好
安全栅栏导致的意外"）。判断"任务太大要拆"本身没错，错在拆完之后走的是一条
完全不受治理的路——自己装软件、自己起服务，没有白名单、没有超时、没有结果
契约、事后也没法审计。

贾维斯已经有一条受治理的"拆大活"路径：core.spawn.spawn_many（只读默认白名单、
强制超时、结果契约、落盘可审计，见任务 #13）。本模块只是把"体量检测 → 切块
→ 组织成 spawn_many 调用"这道工序标准化，让"任务太大"发生时贾维斯有一条
现成、安全的默认选项，不需要每次临场发明一套不受管的变通方法。

刻意保持简单：
  - token 数用粗略估算（4 字符 ≈ 1 token 的经验值），只用于判断数量级，
    不追求精确计数（不为此引入额外分词依赖）。
  - 切块按段落边界（"\\n\\n"）切，尽量不切碎一段话；单段本身超长时退化为硬切，
    保证任何输入都能被切完，不会卡死。
"""
from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数，只用于数量级判断（如"这段文本大概是几千还是几万 token"），
    不是精确计数。"""
    return max(0, len(text or "")) // 4


def is_oversized(text: str, budget_tokens: int = 6000) -> bool:
    """是否超出建议单次处理的预算。默认 6000 token 是"一次工具结果注入对话、
    还给其余内容留有余量"的经验值，场景不同可传更紧/更松的 budget。"""
    return estimate_tokens(text) > budget_tokens


def chunk_text(text: str, chunk_chars: int = 12000) -> list[str]:
    """按段落边界（空行分隔）切块，单块字符数尽量不超过 chunk_chars。
    单个段落本身就超过 chunk_chars 时（如没有换行的巨块）退化为硬切，保证
    任何输入都能被切完。空文本返回空列表。"""
    text = text or ""
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]

    paras = text.split("\n\n")
    chunks: list[str] = []
    cur = ""
    for i, p in enumerate(paras):
        piece = p if i == len(paras) - 1 else p + "\n\n"
        if len(piece) > chunk_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            for j in range(0, len(piece), chunk_chars):
                chunks.append(piece[j:j + chunk_chars])
            continue
        if cur and len(cur) + len(piece) > chunk_chars:
            chunks.append(cur)
            cur = piece
        else:
            cur += piece
    if cur:
        chunks.append(cur)
    return chunks


async def fanout_over_chunks(task_template: str, text: str, *, chunk_chars: int = 12000,
                             label: str = "chunked", concurrency: int = 3,
                             spawn_kwargs: "dict | None" = None) -> list:
    """把大文本切块，每块套进 task_template（须含 "{chunk}" 占位符）派给
    core.spawn.spawn_many——走既有的受治理路径（只读白名单/强制超时/结果契约/
    落盘可审计全部继承，不是另起一套）。按原文顺序返回 [SpawnResult, ...]。

    task_template 例："阅读以下文本片段并提炼要点，只输出要点，不要客套：\\n\\n{chunk}"
    """
    from core.spawn import spawn_many

    chunks = chunk_text(text, chunk_chars=chunk_chars)
    if not chunks:
        return []
    kwargs = spawn_kwargs or {}
    tasks = [
        {"task": task_template.format(chunk=c), "label": f"{label}_{i + 1}/{len(chunks)}", **kwargs}
        for i, c in enumerate(chunks)
    ]
    return await spawn_many(tasks, concurrency=concurrency)
