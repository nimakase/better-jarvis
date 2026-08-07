"""
core/vision.py — 图片理解的模型路由兜底（任务 #20）

背景：贾维斯当前处理图片的路径几乎全是 OCR（rapidocr，走 ingest_credential_image/
ingest_document_file），没有"直接把图片喂给一个真正支持视觉的模型，问它任意问题"
这条通用路径。OCR 适合证件/文档这类结构化提取，不适合"这张截图/照片里发生了什么、
帮我看看这个图表"这类开放式理解——主模型若不支持视觉（如迁移到还没原生支持视觉
的模型阶段），这类需求此前无解，只能让模型编。

设计：不是把图片塞进主对话的消息流（那要改主聊天管线的多模态消息构造，改动面大、
风险高，且当前完全没有这条路径可复用），而是提供一个独立、单次、无工具循环的
"问答式"视觉调用——跟 core/search_augment.py（联网检索补偿）同一个思路：不试图让
主模型自己变得有视觉，而是调用方显式发起一次有视觉能力的请求，把结果当文本消息
拿回来。

模型选择（core/model_routing.model_for("vision")）：
  - 若显式配置了 JARVIS_SUBAGENT_MODEL_VISION，用它；
  - 否则若当前主模型本身就有视觉（core/model_capabilities），直接用主模型——
    这也是 DeepSeek 官方 API 迁移后的常态（DeepSeek v4 原生支持视觉），届时这个
    模块大部分调用会退化成"就是主模型自己"，无需额外配置；
  - 两者都没有 → 如实报错，不假装能看图。

⚠️ 目前只支持视觉模型跟主模型走同一个 provider（config.LLM_API_KEY/LLM_BASE_URL）。
如果要接一个跟主模型不同 provider 的视觉模型（如主模型已切 DeepSeek 但视觉还想用
OpenRouter 上的某个模型），本模块目前不支持——那需要第二套 provider 配置，
超出这一版的范围，先如实标注这个限制，不假装支持。
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8MB，防止把巨图整个塞进请求体


async def describe(image_path: str, question: str = "") -> str:
    """把本地图片交给一个支持视觉的模型，回答 question（默认"描述这张图片"）。
    找不到可用视觉模型 / 文件问题 / 调用失败都返回可读的错误文案，不抛异常。"""
    import config
    from core import model_capabilities, model_routing

    p = Path(image_path).expanduser()
    if not p.exists() or not p.is_file():
        return f"图片文件不存在：{image_path}"
    size = p.stat().st_size
    if size == 0:
        return f"图片文件是空的：{image_path}"
    if size > _MAX_IMAGE_BYTES:
        return f"图片太大（{size / 1024 / 1024:.1f}MB），超过 {_MAX_IMAGE_BYTES // 1024 // 1024}MB 上限。"

    mime, _ = mimetypes.guess_type(str(p))
    if not mime or not mime.startswith("image/"):
        return f"看起来不是图片文件（识别出的类型：{mime or '未知'}）：{image_path}"

    vision_model = model_routing.model_for("vision")
    if not vision_model:
        caps = model_capabilities.capabilities_of(config.CLAUDE_MODEL)
        if caps.vision:
            vision_model = config.CLAUDE_MODEL
        else:
            return ("没有可用的视觉模型——当前主模型不支持图片输入，也没有配置 "
                    "JARVIS_SUBAGENT_MODEL_VISION 兜底。这张图片目前理解不了；"
                    "若是证件/文档类结构化信息，改用 ingest_credential_image / "
                    "ingest_document_file（OCR，不依赖视觉模型）。")

    try:
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    except Exception as e:  # noqa: BLE001
        return f"读取图片失败：{e}"

    from core.llm import get_client
    client = get_client()
    try:
        resp = await client.chat.completions.create(
            model=vision_model,
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": question.strip() or "描述这张图片，说清楚里面有什么。"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
        )
    except Exception as e:  # noqa: BLE001
        return f"视觉模型调用失败（model={vision_model}）：{type(e).__name__}: {e}"

    text = (resp.choices[0].message.content or "").strip()
    return text or "视觉模型没有返回任何文字（可能是内容被拒答或模型不支持这类图片）。"
