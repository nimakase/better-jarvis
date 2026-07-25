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
    GetMessageResourceRequest,
)

from core import history
from core import lark_cards

logger = logging.getLogger("jarvis.lark")

# 走「图片消息」内联展示的扩展；其余一律作「文件消息」发送。
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _card(content: str) -> str:
    """把纯文本/markdown 包成飞书【卡片 JSON 2.0】的 content 字符串。

    正文用 markdown 组件（2.0 富文本），长度护栏在 lark_cards 内做。发送/流式
    patch 都复用它——保证 1.0→2.0 迁移只有这一处结构真源。
    """
    return lark_cards.text_card_json(content)


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
            .register_p2_im_message_receive_v1(self._on_message)      # 收消息（同步回调）
            .register_p2_card_action_trigger(self._on_card_action)    # 卡片按钮/表单回传
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
        """收到用户消息。文本直接进对话；图片/文件/语音/富文本【不再丢弃】——
        下载落盘并转述成一句话喂给控制器，模型即可据此行动（如识别证件照、
        读入文档）。真实资源只落本机 inbox，不外发。"""
        try:
            event = data.event
            msg = event.message
            open_id = event.sender.sender_id.open_id
            if not open_id:
                return
            text = self._message_to_text(msg)
            if not text:
                return
        except Exception:
            logger.exception("[飞书] 解析消息失败")
            return

        logger.info("[飞书] 收到消息: %s", text[:60])
        # 桥回主事件循环去 await 异步 controller
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._handle(open_id, text), self._loop)

    def _message_to_text(self, msg) -> str:
        """把一条飞书消息（任意类型）翻译成喂给控制器的文本。

        text → 原文；post（富文本）→ 抽取纯文本；image/file/audio/media →
        下载到本机 inbox，返回一句带【本机路径】的转述，让模型能接着处理。
        无法处理的类型返回空串（忽略）。"""
        mtype = getattr(msg, "message_type", None)
        mid = getattr(msg, "message_id", "") or ""
        try:
            content = json.loads(msg.content) if getattr(msg, "content", None) else {}
        except Exception:
            content = {}

        if mtype == "text":
            return (content.get("text") or "").strip()

        if mtype == "post":
            return self._extract_post_text(content).strip()

        if mtype == "image":
            key = content.get("image_key", "")
            p = self._download_resource(mid, key, "image", self._suffix_from_key(key, ".png"))
            if p:
                return (f"[用户发来一张图片，已保存到本机：{p}]\n"
                        f"（如需识别证件/银行卡请调 ingest_credential_image；"
                        f"若是单据/文档可据路径处理。）")
            return "[用户发来一张图片，但下载失败了。]"

        if mtype in ("file", "audio", "media"):
            key = content.get("file_key", "")
            fname = content.get("file_name") or self._suffix_from_key(key, ".bin")
            rtype = "file"  # 语音/文件/视频的二进制统一按 file 资源取
            p = self._download_resource(mid, key, rtype, fname)
            if p:
                kind = {"file": "文件", "audio": "语音", "media": "视频"}.get(mtype, "文件")
                extra = "（语音未转写；如需处理请告知）" if mtype == "audio" else \
                        "（如是文档可调 ingest_document_file 读入。）"
                return f"[用户发来一个{kind}「{fname}」，已保存到本机：{p}]\n{extra}"
            return f"[用户发来一个文件，但下载失败了。]"

        # sticker / 其它：忽略，避免噪声打扰对话
        return ""

    @staticmethod
    def _extract_post_text(content: dict) -> str:
        """从飞书 post（富文本）内容里抽取纯文本（拼接所有 text 段）。"""
        parts: list[str] = []
        title = content.get("title")
        if title:
            parts.append(str(title))
        # post 内容可能是 {"content":[[seg,...],...]} 或按语言键包裹
        blocks = content.get("content")
        if blocks is None:
            for v in content.values():
                if isinstance(v, dict) and "content" in v:
                    blocks = v["content"]
                    break
        for line in blocks or []:
            for seg in (line or []):
                if isinstance(seg, dict) and seg.get("tag") in ("text", "a") and seg.get("text"):
                    parts.append(seg["text"])
        return "\n".join(parts)

    @staticmethod
    def _suffix_from_key(key: str, default: str) -> str:
        return default  # 资源 key 不含扩展名；交由后续按内容判断，这里只给个占位名

    def _inbox_dir(self) -> Path:
        try:
            import config
            d = config.DATA_DIR / "lark_inbox"
        except Exception:
            d = Path(__file__).resolve().parent / "lark_inbox"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _download_resource(self, message_id: str, file_key: str,
                           rtype: str, filename: str) -> Optional[Path]:
        """下载一条消息里的资源（图片/文件/语音）到本机 inbox，返回路径。"""
        if not (message_id and file_key):
            return None
        try:
            req = (
                GetMessageResourceRequest.builder()
                .message_id(message_id).file_key(file_key).type(rtype).build()
            )
            resp = self._api.im.v1.message_resource.get(req)
            if not resp.success():
                logger.error("[飞书] 资源下载失败 code=%s msg=%s", resp.code, resp.msg)
                return None
            raw = getattr(resp, "file", None)
            data = raw.read() if hasattr(raw, "read") else raw
            if not data:
                return None
            safe = "".join(c for c in (filename or "resource") if c not in '/\\:*?"<>|') or "resource"
            out = self._inbox_dir() / f"{time.strftime('%Y%m%d_%H%M%S')}_{safe}"
            out.write_bytes(data)
            return out
        except Exception:
            logger.exception("[飞书] 资源下载异常")
            return None

    # ── 卡片回传交互（同步，运行在 SDK 线程）────────────────────────────────
    def _on_card_action(self, data):
        """用户点了卡片里的按钮 / 提交了表单 → 回调到这里。

        核心思路：把回调翻译成一句「用户输入」，用 run_coroutine_threadsafe 喂回
        _handle——于是「点按钮」= 「用户打了这句话」，完全复用整条文本管线，
        无需给控制器加任何新分支。返回一个 toast 给用户即时反馈。"""
        try:
            ev = data.event
            action = ev.action
            open_id = ev.operator.open_id if ev.operator else ""
            value = getattr(action, "value", None)
            form_value = getattr(action, "form_value", None)
            intent = lark_cards.intent_from_callback(value, form_value)
        except Exception:
            logger.exception("[飞书] 解析卡片回调失败")
            return None

        if intent and open_id and self._loop and self._loop.is_running():
            logger.info("[飞书] 卡片回调 → 意图: %s", intent[:60])
            asyncio.run_coroutine_threadsafe(self._handle(open_id, intent), self._loop)
            return self._ack_toast("已收到 ✅")
        return None

    @staticmethod
    def _ack_toast(text: str):
        """构造一个卡片回调的即时 toast 响应（失败则不返回，飞书忽略即可）。"""
        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": text}})
        except Exception:
            return None

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
        self._remember_recipient(open_id)   # 记住最近的对话人，供主动推送用

        # 特殊命令：/reset 清空该用户的会话与历史
        if text == "/reset":
            history.clear(cid)
            if self.get_controller:
                c = await self.get_controller(open_id)
                c.reset_session()
            await asyncio.to_thread(self._send_card, open_id, "✅ 会话已重置")
            return

        # 开一条流式卡：默认「占位卡 + patch」（稳定），若开启原生流式则走 cardkit，
        # 任一步失败自动降级回 patch（见 _stream_open）。
        stream = await asyncio.to_thread(self._stream_open, open_id)

        reply = ""
        controller = None
        if self.get_controller:
            try:
                controller = await self.get_controller(open_id)
                controller.channel = "lark"   # 渠道感知（㉒）：本轮从飞书来
                self._seed(controller, cid)                 # 回灌历史
                history.append("user", text, conversation_id=cid)

                last_push = 0.0

                async def _stream_push(force: bool = False, cursor: bool = True):
                    """节流地把已累积文本刷到卡片，做出打字机式流式效果。"""
                    nonlocal last_push
                    now = time.monotonic()
                    if not force and now - last_push < self._STREAM_INTERVAL:
                        return
                    last_push = now
                    await asyncio.to_thread(self._stream_write, stream, reply or "…", cursor)

                async for ev in controller.chat(text):
                    if not isinstance(ev, dict):
                        continue
                    if ev.get("type") == "text":
                        reply += ev["text"]
                        await _stream_push()                 # 边生成边刷新
                    elif ev.get("type") == "tool":
                        # 露出工具进度（会被后续文本覆盖）
                        await asyncio.to_thread(self._stream_tool, stream, reply or "",
                                                ev.get("name", ""))
                        last_push = time.monotonic()

                await _stream_push(force=True, cursor=False)  # 收尾：去光标、刷全文
            except Exception as e:
                logger.exception("[飞书] 控制器处理异常")
                reply = f"抱歉，处理时出错了：{e}"
        else:
            reply = "我还没有接入处理引擎。"

        # 兜底：确保最终结果落地（含异常 / 空回复 / 建卡失败等情形）
        final = reply.strip() or "（处理完成，无文本输出）"
        await asyncio.to_thread(self._stream_close, stream, open_id, final)

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

        elif kind == "interactive":
            # 让贾维斯自己设计的可交互回复：按钮组 / 表单。渲染成 2.0 回传卡；
            # 用户点击/提交 → _on_card_action 合成用户消息回流（闭环）。
            # 兜底：富卡若发送失败（如个别组件不被某客户端版本支持），降级成一张
            # 文字卡把选项/字段列出来，让用户可以照着打字——绝不让交互变成死路。
            mode = p.get("mode")
            if mode == "form":
                content = lark_cards.form_card_json(
                    p.get("text", ""), p.get("fields", []),
                    submit_label=p.get("submit_label", "提交"),
                    submit_intent=p.get("submit_intent", "提交表单"),
                    title=p.get("title", "") or lark_cards.DEFAULT_TITLE,
                )
            else:
                content = lark_cards.interactive_card_json(
                    p.get("text", ""), p.get("options", []),
                    title=p.get("title", "") or lark_cards.DEFAULT_TITLE,
                )
            mid = await asyncio.to_thread(self._send_card_content, open_id, content)
            if not mid:
                fallback = self._interactive_text_fallback(p)
                await asyncio.to_thread(self._send_card, open_id, fallback)

    @staticmethod
    def _interactive_text_fallback(p: dict) -> str:
        """富交互卡发送失败时的纯文字降级：把选项/字段列出来让用户照着打字。"""
        text = p.get("text", "") or "请选择："
        if p.get("mode") == "form":
            lines = [text, "", "请依次告诉我："]
            for f in p.get("fields", []):
                lines.append(f"· {f.get('label', f.get('name', ''))}")
            return "\n".join(lines)
        lines = [text, ""]
        for o in p.get("options", []):
            lines.append(f"· {o.get('label', '')} —— 回复「{o.get('intent', o.get('label', ''))}」")
        return "\n".join(lines)

    # ── 流式卡抽象（patch 默认 / cardkit 原生可选）─────────────────────────────
    #
    # _handle 里的流式只认三个动作：open→write→close。底下有两种实现：
    #   · patch  ：发一张占位卡，之后每次整卡 patch 刷新（proven，默认）。
    #   · native ：cardkit 建卡实体 + 增量推文本，飞书端原生打字机、免整卡重刷
    #             的更新频率限制；仅在 config.FEISHU_NATIVE_STREAMING 开启时尝试，
    #             建卡/发送任一步失败即降级回 patch，绝不影响回复落地。
    #
    # 【注意】native 路径涉及 cardkit 多步握手，需在真实飞书环境联调验证后再开。

    def _native_enabled(self) -> bool:
        try:
            import config
            return bool(getattr(config, "FEISHU_NATIVE_STREAMING", False))
        except Exception:
            return False

    def _stream_open(self, open_id: str) -> dict:
        """开一条流式卡，返回状态 dict（mode + 定位信息）。"""
        if self._native_enabled():
            st = self._native_stream_open(open_id)
            if st:
                return st
            logger.warning("[飞书] 原生流式建卡失败，降级为 patch 流式")
        mid = self._send_card(open_id, "⏳ 正在思考...")
        return {"mode": "patch", "message_id": mid}

    def _stream_write(self, stream: dict, text: str, cursor: bool) -> None:
        body = (text or "…") + (" ▌" if cursor else "")
        if stream.get("mode") == "native":
            self._native_stream_write(stream, body)
        elif stream.get("message_id"):
            self._update_card(stream["message_id"], body)

    def _stream_tool(self, stream: dict, text: str, name: str) -> None:
        body = (text or "") + f"\n\n_🔧 正在调用 {name}…_"
        if stream.get("mode") == "native":
            self._native_stream_write(stream, body)
        elif stream.get("message_id"):
            self._update_card(stream["message_id"], body)

    def _stream_close(self, stream: dict, open_id: str, final: str) -> None:
        if stream.get("mode") == "native":
            self._native_stream_write(stream, final)
        elif stream.get("message_id"):
            self._update_card(stream["message_id"], final)
        else:
            # 连占位卡都没发出来（网络/建卡都失败）——兜底直接发一张终态卡
            self._send_card(open_id, final)

    def _native_stream_open(self, open_id: str) -> Optional[dict]:
        """cardkit 建卡实体 → 发引用该实体的 interactive 消息。成功返回状态。"""
        try:
            from lark_oapi.api.cardkit.v1 import (
                CreateCardRequest, CreateCardRequestBody,
            )
            element_id = "md"   # 与 lark_cards 里流式正文组件的 element_id 对齐
            card_json = lark_cards.text_card_json("…", streaming=True)
            req = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder().type("card_json").data(card_json).build()
                ).build()
            )
            resp = self._api.cardkit.v1.card.create(req)
            if not resp.success():
                logger.error("[飞书] cardkit 建卡失败 code=%s msg=%s", resp.code, resp.msg)
                return None
            card_id = resp.data.card_id
            # 发一条引用卡实体的 interactive 消息（内容即卡实体指针）
            content = json.dumps({"type": "card", "data": {"card_id": card_id}})
            mid = self._send_card_content(open_id, content)
            if not mid:
                return None
            return {"mode": "native", "card_id": card_id,
                    "element_id": element_id, "seq": 1, "message_id": mid}
        except Exception:
            logger.exception("[飞书] cardkit 建卡异常")
            return None

    def _native_stream_write(self, stream: dict, text: str) -> None:
        """向卡实体的正文组件推全量文本，飞书端自动算增量做打字机。"""
        try:
            from lark_oapi.api.cardkit.v1 import (
                ContentCardElementRequest, ContentCardElementRequestBody,
            )
            seq = stream["seq"]
            stream["seq"] = seq + 1
            req = (
                ContentCardElementRequest.builder()
                .card_id(stream["card_id"])
                .element_id(stream["element_id"])
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(lark_cards._truncate(text)).sequence(seq).build()
                ).build()
            )
            resp = self._api.cardkit.v1.card_element.content(req)
            if not resp.success():
                logger.error("[飞书] cardkit 推流失败 code=%s msg=%s", resp.code, resp.msg)
        except Exception:
            logger.exception("[飞书] cardkit 推流异常")

    # ── 上传并发送文件 / 图片（同步 HTTP）───────────────────────────────────
    # ── 主动推送（delivery 渠道）────────────────────────────────────────────
    #
    # 定时任务（潜客名单、日报）没有对话上下文，file_download 带外动作没有去处；
    # 它们的产出走 core/delivery 的渠道机制。本桥注册为 "lark" 渠道（见 main.py），
    # push() 即渠道入口：发文字卡片 + 逐个发附件文件。
    #
    # 收件人解析：config.FEISHU_PUSH_OPEN_ID（显式指定）→ 数据目录里记下的
    # 「最近跟 jarvis 说过话的人」（单用户产品的合理默认，_handle 每次都会刷新）。

    def _push_target_path(self) -> Path:
        try:
            import config
            return config.DATA_DIR / "lark_push_target.json"
        except Exception:
            return Path(__file__).resolve().parent / "lark_push_target.json"

    def _remember_recipient(self, open_id: str) -> None:
        """把最近对话人记到数据目录（幂等；失败不影响对话）。"""
        try:
            p = self._push_target_path()
            if p.exists():
                try:
                    if json.loads(p.read_text(encoding="utf-8")).get("open_id") == open_id:
                        return
                except Exception:
                    pass
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"open_id": open_id,
                                     "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}),
                         encoding="utf-8")
        except Exception:
            logger.exception("[飞书] 记录推送收件人失败（不影响对话）")

    def _resolve_push_target(self) -> str:
        try:
            import config
            if getattr(config, "FEISHU_PUSH_OPEN_ID", ""):
                return config.FEISHU_PUSH_OPEN_ID
        except Exception:
            pass
        try:
            p = self._push_target_path()
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8")).get("open_id") or ""
        except Exception:
            pass
        return ""

    def push(self, title: str, content: str, attachments=None) -> dict:
        """delivery 渠道入口（同步，签名与新式 ChannelFn 对齐）。

        文字发卡片；attachments 里存在的文件逐个真实发送（xlsx→文件消息，
        图片→图片消息，复用 _send_file 的分发）。没有收件人时明确报错——
        deliver() 会把它记成该渠道失败，不拖累 webpush。
        """
        open_id = self._resolve_push_target()
        if not open_id:
            raise RuntimeError(
                "没有可用的飞书收件人：在 .env 配 FEISHU_PUSH_OPEN_ID，"
                "或先在飞书里给 jarvis 发一条消息（会自动记住你）")
        body = f"**{title}**\n{content}" if title else content
        self._send_card(open_id, body)
        files = []
        for a in attachments or []:
            fp = Path(a)
            if not fp.exists():
                files.append({"file": str(a), "ok": False, "error": "文件不存在"})
                continue
            ok = self._send_file(open_id, fp, fp.name)
            files.append({"file": fp.name, "ok": bool(ok)})
        return {"pushed": True, "files": files}

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
        """把一段文本/markdown 包成 2.0 文本卡发送。"""
        return self._send_card_content(open_id, _card(content))

    def _send_card_content(self, open_id: str, card_json: str) -> Optional[str]:
        """发送一张【已构建好的】卡片 JSON（供按钮/表单等富交互卡直接发送）。"""
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("interactive")
                .content(card_json)
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
