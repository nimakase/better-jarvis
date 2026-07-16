"""
Lark Bridge — 飞书实时双向通道（官方 lark-oapi 长连接）

为什么用官方 SDK 而不是手写 WebSocket：飞书的长连接是官方 SDK 封装的私有握手
协议（连接后网关地址动态下发、鉴权只在建连时做），无法靠手写 wss 端点对接。
故这里用 lark-oapi 的 ws.Client。依赖：pip install lark-oapi

集成要点（与本项目的异步主循环安全共存）：
  - SDK 的 ws.Client.start() 是【阻塞】的 → 丢到后台守护线程跑，不阻塞 FastAPI。
  - 事件回调是【同步】函数、在 SDK 线程里被调用 → 用 run_coroutine_threadsafe
    把真正的处理调度回主事件循环，从而能 await 现有的 async controller.chat()。
  - 发送/更新卡片是同步 HTTP → 在协程里用 asyncio.to_thread 包一层，不阻塞主循环。

飞书后台需先配置：事件订阅方式改为「使用长连接接收事件」，并给应用加
「接收消息 im.message.receive_v1」事件权限与机器人能力。

坑点（来自 SDK 实践）：飞书长连接依赖 protobuf3（<4.21.1）。若装了需要更高
protobuf 的库（如某些 mysql-connector）可能冲突，届时需降级 protobuf。
"""

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateFileRequest,
    CreateFileRequestBody,
)

from core import history

logger = logging.getLogger("jarvis.lark")

# 走「图片消息」内联展示的扩展；其余一律作「文件消息」发送。
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _card(content: str) -> str:
    """把纯文本/markdown 包成飞书交互卡片的 content JSON 字符串。"""
    if len(content) > 10000:            # 飞书卡片有长度限制，超长截断
        content = content[:9990] + "\n\n…（截断）"
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "🤖 贾维斯"}},
        # lark_md 是最稳的富文本写法（跨卡片版本通用）
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
    }
    return json.dumps(card, ensure_ascii=False)


class LarkBridge:
    """飞书桥：官方长连接收消息 → 交给 controller → 流式刷新卡片回复。"""

    # 流式刷新卡片的最小间隔（秒）。太密会撞飞书更新频率限制；0.8s ≈ 打字机手感。
    _STREAM_INTERVAL = 0.8

    def __init__(self, app_id: str, app_secret: str, get_controller=None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.get_controller = get_controller

        # 同步 API 客户端（发/更新消息用）
        self._api = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        self._thread: Optional[threading.Thread] = None

    # ── 启停 ──────────────────────────────────────────────────────────────
    async def start(self):
        """记录主事件循环（回调桥接用），然后另起线程跑 SDK。

        关键：SDK 的 ws.Client 内部会 loop.run_until_complete(...)。若在主循环上
        构造/启动它，会抓到【正在运行的主循环】而报 'event loop is already running'。
        所以把 Client 的构造与 start() 全部放进后台线程，并给该线程一个【全新的
        事件循环】——SDK 用它自己的循环，绝不碰主循环。
        """
        self._loop = asyncio.get_running_loop()   # 主循环，供 _on_message 桥接回来
        self._thread = threading.Thread(target=self._run_ws, daemon=True, name="lark-ws")
        self._thread.start()
        logger.info("[飞书] 🚀 长连接线程已启动")

    def _run_ws(self):
        """后台线程入口：建独立事件循环 → 构造并启动 ws.Client（阻塞）。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # 关键修复：lark ws 的 start() 用的是它【模块级全局 loop】，该全局在
        # `import lark_oapi.ws.client` 时就绑定了当时的当前事件循环。由于我们是在
        # uvicorn 已运行的主循环里才 import 的 lark_bridge，那个全局绑到了【正在运行
        # 的主循环】→ start() 的 run_until_complete 报 'event loop is already running'。
        # 这里把该全局改绑到本线程这个全新的、未运行的循环。
        import lark_oapi.ws.client as _wsc
        _wsc.loop = loop

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)   # 同步回调
            .build()
        )
        self._ws = lark.ws.Client(
            self.app_id, self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        try:
            self._ws.start()   # 阻塞；用本线程这个全新的、未运行的循环
        except Exception:
            logger.exception("[飞书] 长连接线程异常退出")

    async def stop(self):
        # SDK 未暴露稳定的 async 关闭；守护线程随进程退出。这里尽力而为。
        stop_fn = getattr(self._ws, "stop", None) or getattr(self._ws, "close", None)
        if callable(stop_fn):
            try:
                stop_fn()
            except Exception:
                pass
        logger.info("[飞书] 🛑 已停止")

    # ── 事件回调（同步，运行在 SDK 线程）────────────────────────────────────
    def _on_message(self, data) -> None:
        try:
            event = data.event
            msg = event.message
            open_id = event.sender.sender_id.open_id
            if getattr(msg, "message_type", None) != "text" or not open_id:
                return
            text = (json.loads(msg.content).get("text") or "").strip()
            if not text:
                return
        except Exception:
            logger.exception("[飞书] 解析消息失败")
            return

        logger.info("[飞书] 收到消息: %s", text[:60])
        # 桥回主事件循环去 await 异步 controller
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._handle(open_id, text), self._loop)

    # ── 真正处理（协程，运行在主事件循环）──────────────────────────────────
    def _seed(self, controller, conversation_id: str) -> None:
        """新会话控制器内存为空时，用持久化历史回灌上下文（跨重启/回收续聊）。"""
        if controller.messages:
            return
        for m in history.get_messages(conversation_id):
            if m["kind"] == "text" and m["role"] in ("user", "assistant"):
                controller.messages.append({"role": m["role"], "content": m["content"]})

    async def _handle(self, open_id: str, text: str):
        cid = f"lark_{open_id}"   # 每个飞书用户一条独立、可持久化的对话线

        # 特殊命令：/reset 清空该用户的会话与历史
        if text == "/reset":
            history.clear(cid)
            if self.get_controller:
                c = await self.get_controller(open_id)
                c.reset_session()
            await asyncio.to_thread(self._send_card, open_id, "✅ 会话已重置")
            return

        # 先发「思考中」占位卡片（同步 HTTP → 丢线程，不阻塞主循环）
        card_id = await asyncio.to_thread(self._send_card, open_id, "⏳ 正在思考...")

        reply = ""
        controller = None
        if self.get_controller:
            try:
                controller = await self.get_controller(open_id)
                self._seed(controller, cid)                 # 回灌历史
                history.append("user", text, conversation_id=cid)

                last_push = 0.0

                async def _stream_push(force: bool = False, cursor: bool = True):
                    """节流地把已累积文本刷到卡片，做出打字机式流式效果。"""
                    nonlocal last_push
                    if not card_id:
                        return
                    now = time.monotonic()
                    if not force and now - last_push < self._STREAM_INTERVAL:
                        return
                    last_push = now
                    body = (reply or "…") + (" ▌" if cursor else "")
                    await asyncio.to_thread(self._update_card, card_id, body)

                async for ev in controller.chat(text):
                    if not isinstance(ev, dict):
                        continue
                    if ev.get("type") == "text":
                        reply += ev["text"]
                        await _stream_push()                 # 边生成边刷新
                    elif ev.get("type") == "tool" and card_id:
                        # 露出工具进度（会被后续文本覆盖）
                        await asyncio.to_thread(
                            self._update_card, card_id,
                            (reply or "") + f"\n\n_🔧 正在调用 {ev.get('name', '')}…_")
                        last_push = time.monotonic()

                await _stream_push(force=True, cursor=False)  # 收尾：去光标、刷全文
            except Exception as e:
                logger.exception("[飞书] 控制器处理异常")
                reply = f"抱歉，处理时出错了：{e}"
        else:
            reply = "我还没有接入处理引擎。"

        # 兜底：异常 / 无占位卡片 / 空回复时，确保最终结果落地
        final = reply.strip() or "（处理完成，无文本输出）"
        if card_id:
            await asyncio.to_thread(self._update_card, card_id, final)
        else:
            await asyncio.to_thread(self._send_card, open_id, final)

        if reply.strip():
            history.append("assistant", reply, conversation_id=cid)

        # 带外动作（发文件 / 证件揭示 / 代码审查）——飞书侧对等分发
        if controller is not None:
            for action in controller.drain_actions():
                try:
                    await self._dispatch_action(open_id, cid, action)
                except Exception:
                    logger.exception("[飞书] 带外动作分发失败")
        logger.info("[飞书] ✅ 已回复")

    async def _dispatch_action(self, open_id: str, cid: str, action) -> None:
        """把 controller 产生的带外动作对等地送到飞书。"""
        kind = action.type
        p = action.payload

        if kind == "file_download":
            path = Path(p["file_path"])
            filename = p.get("filename") or path.name
            if not path.exists():
                return
            ok = await asyncio.to_thread(self._send_file, open_id, path, filename)
            if ok:   # 文件卡片可安全持久化（不含敏感值）
                history.append("assistant", {"filename": filename}, kind="file",
                               conversation_id=cid)

        elif kind == "credential_reveal":
            # 安全边界：证件/卡号【真实号码】绝不经飞书云端外发。飞书一律拒绝显示，
            # 引导用户去本机网页保险箱查看（与"真实值只走本机浏览器"的不变量一致）。
            await asyncio.to_thread(
                self._send_card, open_id,
                "🔒 出于安全，证件 / 卡号的**真实号码**只能在**本机网页**的保险箱查看，"
                "不会通过飞书发送。",
            )

        elif kind == "code_review":
            # 飞书侧对等审查：直接把代码 + 校验结果发成卡片，用户回复「激活 X」即可生效
            # （模型已具备 activate_tool / read_tool_code / review_tool，无需回本机网页）。
            name = p.get("name", "")
            code = p.get("code", "") or ""
            try:
                from core.tool_builder import validation_summary
                vsum = validation_summary(p.get("validation"))
            except Exception:
                vsum = ""
            # 代码可能很长，卡片有长度上限——超长截断，完整代码可让贾维斯 read_tool_code 再发
            MAX = 2500
            shown = code if len(code) <= MAX else code[:MAX] + "\n…（代码较长已截断，可对我说「看 " + name + " 的完整代码」）"
            card = (
                f"🛠️ 工具「**{name}**」代码已生成 / 修改，请审查：\n\n"
                f"{vsum}\n\n"
                f"```python\n{shown}\n```\n\n"
                f"—— 审查通过就回复「**激活 {name}**」使其生效；"
                f"要改就说「**修改 {name}：<说明>**」。"
            )
            await asyncio.to_thread(self._send_card, open_id, card)

    # ── 上传并发送文件 / 图片（同步 HTTP）───────────────────────────────────
    def _send_file(self, open_id: str, path: Path, filename: str) -> bool:
        try:
            if path.suffix.lower() in _IMAGE_EXTS:
                with open(path, "rb") as f:
                    up = self._api.im.v1.image.create(
                        CreateImageRequest.builder().request_body(
                            CreateImageRequestBody.builder()
                            .image_type("message").image(f).build()
                        ).build()
                    )
                if not up.success():
                    logger.error("[飞书] 图片上传失败 code=%s msg=%s", up.code, up.msg)
                    return False
                content, msg_type = json.dumps({"image_key": up.data.image_key}), "image"
            else:
                with open(path, "rb") as f:
                    up = self._api.im.v1.file.create(
                        CreateFileRequest.builder().request_body(
                            CreateFileRequestBody.builder()
                            .file_type("stream").file_name(filename).file(f).build()
                        ).build()
                    )
                if not up.success():
                    logger.error("[飞书] 文件上传失败 code=%s msg=%s", up.code, up.msg)
                    return False
                content, msg_type = json.dumps({"file_key": up.data.file_key}), "file"

            req = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(open_id).msg_type(msg_type).content(content).build()
                )
                .build()
            )
            resp = self._api.im.v1.message.create(req)
            if not resp.success():
                logger.error("[飞书] 文件消息发送失败 code=%s msg=%s", resp.code, resp.msg)
                return False
            return True
        except Exception:
            logger.exception("[飞书] 发送文件异常")
            return False

    # ── 发送 / 更新卡片（同步 HTTP）─────────────────────────────────────────
    def _send_card(self, open_id: str, content: str) -> Optional[str]:
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("interactive")
                .content(_card(content))
                .build()
            )
            .build()
        )
        resp = self._api.im.v1.message.create(req)
        if resp.success():
            return resp.data.message_id
        logger.error("[飞书] 发送卡片失败 code=%s msg=%s", resp.code, resp.msg)
        return None

    def _update_card(self, message_id: str, content: str) -> None:
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(PatchMessageRequestBody.builder().content(_card(content)).build())
            .build()
        )
        resp = self._api.im.v1.message.patch(req)
        if not resp.success():
            logger.error("[飞书] 更新卡片失败 code=%s msg=%s", resp.code, resp.msg)
