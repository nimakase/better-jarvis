#!/usr/bin/env python3
"""子 agent 模型路由表 + 视觉理解兜底（任务 #20）——自动生成，绝不覆盖既有测试文件。

覆盖：
  1. core/model_routing：env 覆盖 / 默认空 / 未知 purpose 退回 default（动态读取，
     不在导入时缓存——跟 core.spawn._subagent_model() 此前"随时改 env 立即生效"
     的既有行为保持一致，tests/test_workflow_dispatch.py 里已有用例依赖这一点）
  2. core/spawn._subagent_model(purpose) 接到 model_routing，spawn() 把 purpose 传下去
  3. core/vision.describe：文件校验（不存在/空/超限/非图片）、无可用视觉模型时的
     明确报错、主模型自带视觉时直接复用、显式配置视觉路由时优先、调用异常降级
  4. connectors/vision_tools.describe_image 经 registry 注册，effect/duration 正确
  5. controller._network_capability_note 按 caps.vision 分支提示 describe_image
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


_ROUTE_ENV_VARS = ("JARVIS_SUBAGENT_MODEL", "JARVIS_SUBAGENT_MODEL_VISION",
                    "JARVIS_SUBAGENT_MODEL_CODE_REVIEW")


def _clear_route_envs():
    for v in _ROUTE_ENV_VARS:
        os.environ.pop(v, None)


# ── 1. model_routing ──────────────────────────────────────────────────────────
print("[1] core/model_routing")
from core import model_routing  # noqa: E402

_clear_route_envs()
check(model_routing.model_for("default") == "", "未配置任何 env 时 default 路由为空（=跟主模型一样）")
check(model_routing.model_for("不存在的用途") == model_routing.model_for("default"),
      "未知 purpose 退回 default 路由，不报错")

try:
    os.environ["JARVIS_SUBAGENT_MODEL_VISION"] = "some-vision-model"
    os.environ["JARVIS_SUBAGENT_MODEL_CODE_REVIEW"] = "some-code-model"
    check(model_routing.model_for("vision") == "some-vision-model", "vision 路由生效")
    check(model_routing.model_for("code_review") == "some-code-model", "code_review 路由生效")
    check("vision" in model_routing.known_purposes() and "default" in model_routing.known_purposes(),
          "known_purposes 列出已注册用途")

    os.environ.pop("JARVIS_SUBAGENT_MODEL_VISION", None)
    os.environ["JARVIS_SUBAGENT_MODEL"] = "fallback-model"
    check(model_routing.model_for("vision") == "fallback-model",
          "该 purpose 的专属 env 未设置时退回 default 路由的值")
finally:
    _clear_route_envs()


# ── 2. spawn._subagent_model / spawn(purpose=) 接线 ───────────────────────────
print("[2] spawn 接线")
from core import spawn  # noqa: E402
import inspect  # noqa: E402

try:
    os.environ["JARVIS_SUBAGENT_MODEL_VISION"] = "vision-model-x"
    check(spawn._subagent_model("vision") == "vision-model-x",
          "spawn._subagent_model(purpose) 正确经 model_routing 路由")
    check(spawn._subagent_model("default") == model_routing.model_for("default"),
          "spawn._subagent_model('default') 与此前唯一行为等价")
finally:
    _clear_route_envs()

spawn_src = inspect.getsource(spawn.spawn)
check("purpose" in spawn_src and "_subagent_model(purpose)" in spawn_src,
      "spawn() 把 purpose 参数传给 _subagent_model")


# ── 3. core/vision.describe ───────────────────────────────────────────────────
print("[3] core/vision.describe")
from core import vision  # noqa: E402

_TMPDIR = Path("/tmp/_jarvis_test_vision")
_TMPDIR.mkdir(exist_ok=True)


async def _t_vision():
    # 文件不存在
    out = await vision.describe(str(_TMPDIR / "不存在.png"))
    check("不存在" in out, "文件不存在 → 明确报错")

    # 空文件
    empty = _TMPDIR / "empty.png"
    empty.write_bytes(b"")
    out2 = await vision.describe(str(empty))
    check("空" in out2, "空文件 → 明确报错")

    # 超大文件（改小上限测试，不真的造 8MB 文件）
    big = _TMPDIR / "big.png"
    big.write_bytes(b"\x89PNG" + b"0" * 1000)
    orig_cap = vision._MAX_IMAGE_BYTES
    vision._MAX_IMAGE_BYTES = 500
    try:
        out3 = await vision.describe(str(big))
        check("太大" in out3, "超过上限 → 明确报错")
    finally:
        vision._MAX_IMAGE_BYTES = orig_cap

    # 非图片文件（.txt）
    txt = _TMPDIR / "not_image.txt"
    txt.write_text("hello")
    out4 = await vision.describe(str(txt))
    check("不是图片" in out4, "非图片 mimetype → 明确报错")

    # 正常小 png，主模型无视觉、也未配置视觉路由 → 明确报错，指向 OCR 工具
    small = _TMPDIR / "small.png"
    small.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)

    orig_model = config.CLAUDE_MODEL
    config.CLAUDE_MODEL = "deepseek-chat"  # 非视觉
    _clear_route_envs()
    try:
        out5 = await vision.describe(str(small))
        check("没有可用的视觉模型" in out5, "无视觉能力且未配置路由 → 明确报错")
        check("ingest_credential_image" in out5, "报错里指向 OCR 工具作为替代路径")
    finally:
        config.CLAUDE_MODEL = orig_model
        _clear_route_envs()

    # 主模型本身带视觉 → 直接复用，走真实调用路径（mock get_client）
    class _FakeResp:
        class _Choice:
            class _Msg:
                content = "这是一张测试图片。"
            message = _Msg()
        choices = [_Choice()]

    calls = []

    class _FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return _FakeResp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    import core.llm as llm_mod
    orig_get_client = llm_mod.get_client
    llm_mod.get_client = lambda *a, **kw: _FakeClient()
    config.CLAUDE_MODEL = "deepseek-v4-flash"  # 内置能力表里标了 vision=True
    _clear_route_envs()
    try:
        out6 = await vision.describe(str(small), "这是什么？")
        check(out6 == "这是一张测试图片。", "主模型带视觉时能拿到真实回复文本")
        check(calls and calls[0]["model"] == "deepseek-v4-flash",
              "主模型带视觉时直接复用主模型，不需要额外配置")
        msg_content = calls[0]["messages"][0]["content"]
        check(any(b.get("type") == "image_url" for b in msg_content),
              "请求体里带了 image_url 内容块")
        check(any(b.get("type") == "text" and b.get("text") == "这是什么？" for b in msg_content),
              "自定义问题被正确带入请求")
    finally:
        llm_mod.get_client = orig_get_client
        config.CLAUDE_MODEL = orig_model
        _clear_route_envs()

    # 显式配置视觉路由 → 即便主模型没视觉也优先用配置的路由
    calls.clear()
    llm_mod.get_client = lambda *a, **kw: _FakeClient()
    config.CLAUDE_MODEL = "deepseek-chat"
    os.environ["JARVIS_SUBAGENT_MODEL_VISION"] = "explicit-vision-model"
    try:
        out7 = await vision.describe(str(small))
        check(calls and calls[0]["model"] == "explicit-vision-model",
              "显式配置的视觉路由优先于主模型能力判断")
    finally:
        llm_mod.get_client = orig_get_client
        config.CLAUDE_MODEL = orig_model
        _clear_route_envs()

    # 调用异常 → 优雅降级
    class _BoomCompletions:
        async def create(self, **kwargs):
            raise RuntimeError("模拟网络错误")

    class _BoomChat:
        completions = _BoomCompletions()

    class _BoomClient:
        chat = _BoomChat()

    llm_mod.get_client = lambda *a, **kw: _BoomClient()
    os.environ["JARVIS_SUBAGENT_MODEL_VISION"] = "some-model"
    try:
        out8 = await vision.describe(str(small))
        check("调用失败" in out8, "视觉模型调用异常 → 明确报错文案，不抛异常")
    finally:
        llm_mod.get_client = orig_get_client
        _clear_route_envs()

asyncio.run(_t_vision())


# ── 4. connectors/vision_tools 注册 ────────────────────────────────────────────
print("[4] vision_tools 注册")
from core import registry, effects  # noqa: E402
import connectors.vision_tools as vt  # noqa: E402, F401

spec = registry._SPECS.get("describe_image")  # noqa: SLF001
check(spec is not None, "describe_image 已注册")
check(spec.effect == effects.READ_LOCAL, "describe_image 效应等级为 read_local")
from core import tool_timeout as tt  # noqa: E402
check(tt.timeout_of("describe_image") == 60.0, "describe_image 走 slow(60s) 超时档")


# ── 5. controller._network_capability_note 视觉分支 ────────────────────────────
print("[5] controller 视觉能力提示分支")
from core import controller as ctrl  # noqa: E402

orig_model = config.CLAUDE_MODEL
try:
    config.CLAUDE_MODEL = "deepseek-v4-flash"  # 带视觉
    note_vision = ctrl._network_capability_note()
    check("你当前的模型支持图片输入" in note_vision, "带视觉的模型 → 提示自己能直接看图")
    check("describe_image" not in note_vision, "带视觉时不需要提示 describe_image 兜底")

    config.CLAUDE_MODEL = "deepseek-chat"  # 不带视觉
    note_no_vision = ctrl._network_capability_note()
    check("不支持】直接看图片" in note_no_vision, "不带视觉的模型 → 明确告知看不了")
    check("describe_image" in note_no_vision, "不带视觉时提示 describe_image 作为兜底路径")
    check("ingest_credential_image" in note_no_vision, "同时提示 OCR 路径用于证件/文档场景")
finally:
    config.CLAUDE_MODEL = orig_model


# ── 收尾 ──────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败")
    sys.exit(1)
print("✅ test_auto_model_routing 全部通过")
sys.exit(0)
