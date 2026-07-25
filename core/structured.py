"""
core/structured.py — 结构化输出封装（Instructor，可选增强）

问题：贾维斯多处让模型吐 JSON，现在靠 json_salvage【事后抢救】截断。Instructor 提供
【事前保证】：给 schema，校验不过就把错误喂回重试。二者互补——事前尽量吐对，
事后仍抢救兜底。

设计（与全局一贯风格一致）：
  - 未安装 instructor → 优雅降级：走 core.llm 直调 + 传入的 fallback_parser（默认
    json_salvage）。行为与现状一致，装了才升级，绝不因缺包报错。
  - 有限重试：max_retries 默认 2（撞 token 墙/prompt 写错时最多重试 N 次即抛，
    绝不无限重试——这是硬底线）。撞满后同样退回 fallback_parser，不让异常裸奔。
  - 只做"生成结构化数据"这一件事，不介入主对话循环（避免与 controller 打架）。

用法：
    from pydantic import BaseModel
    class DocFields(BaseModel):
        doc_type: str; fields: dict; expires_at: str | None
    obj = await extract(prompt, DocFields)          # dict 或 None
"""
from __future__ import annotations

from typing import Any, Callable, Optional


def instructor_available() -> bool:
    try:
        import instructor  # noqa: F401
        return True
    except Exception:
        return False


async def extract(prompt: str, schema: Any = None, *,
                  fallback_parser: Optional[Callable[[str], Any]] = None,
                  max_retries: int = 2, model: Optional[str] = None,
                  timeout: float = 120.0) -> Any:
    """让模型按 schema 产出结构化数据。

    - schema：Pydantic 模型类（instructor 用它约束+校验+重试）。为 None 时只走
      fallback_parser（纯抢救模式）。
    - fallback_parser：把模型原始文本解析成数据的函数；默认 json_salvage 对象抢救。
    返回：instructor 路径返回 schema 实例转的 dict；降级路径返回 fallback_parser 的结果。
    失败一律返回 None，不抛。
    """
    import config
    from core.json_salvage import salvage_json_objects

    if fallback_parser is None:
        def fallback_parser(text: str):  # noqa: E731
            objs = salvage_json_objects(text)
            return objs[0] if objs else None

    mdl = model or config.CLAUDE_MODEL

    # —— instructor 路径（装了才走）——
    if schema is not None and instructor_available():
        try:
            import instructor
            from core.llm import get_client
            client = instructor.from_openai(get_client(timeout=timeout))  # 单一构建点
            obj = await client.chat.completions.create(
                model=mdl, response_model=schema, max_retries=max_retries,
                messages=[{"role": "user", "content": prompt}],
            )
            # Pydantic v2 / v1 兼容
            if hasattr(obj, "model_dump"):
                return obj.model_dump()
            if hasattr(obj, "dict"):
                return obj.dict()
            return obj
        except Exception:
            # instructor 重试到上限仍失败 → 落到降级路径，绝不让异常裸奔
            pass

    # —— 降级路径：直调 + 抢救解析 ——
    try:
        from core.llm import get_client
        client = get_client(timeout=timeout)
        resp = await client.chat.completions.create(
            model=mdl, messages=[{"role": "user", "content": prompt}])
        text = resp.choices[0].message.content or ""
        return fallback_parser(text)
    except Exception:
        return None
