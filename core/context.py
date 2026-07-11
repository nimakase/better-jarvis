"""
应用级上下文与会话管理

- SessionManager：按 session_id 隔离每个对话的 JarvisController，
  解决「全局单例 controller 导致多标签 / 多连接共享同一历史」的问题。
- AppContext：进程内共享对象的容器，启动时建一份挂到 app.state.ctx，
  路由通过它取依赖，便于测试与未来扩展（如注入调度器、配置等）。
"""

import time
from collections import OrderedDict
from dataclasses import dataclass, field

import config
from core.controller import JarvisController


class SessionManager:
    """按 session_id 维护独立的对话控制器，带 TTL + LRU 惰性回收。

    背景：客户端把 session_id 存在 localStorage 并跨重连复用；这些会话不像
    按连接分配的临时会话那样在断开时被 discard，过去会随进程无界增长（每个新
    设备/清缓存/新标签都留下一个永不回收的 controller）。

    回收策略（惰性，无后台任务）：每次访问时
      1) 清掉超过 TTL 未活动的会话；
      2) 把驻留总数压到 max_sessions 以内（超出按最久未用 LRU 淘汰）。
    被淘汰的会话下次访问会由 web/chat._seed_controller_from_history 从持久化
    历史透明重建，只丢失内存里的压缩工作态，不影响界面回看与跨设备续聊。
    刚访问过的会话总被移到最近端，绝不会在本轮里把自己淘汰掉。
    """

    def __init__(self, ttl_seconds: int | None = None, max_sessions: int | None = None):
        self._sessions: "OrderedDict[str, JarvisController]" = OrderedDict()
        self._last_access: dict[str, float] = {}
        self._ttl = config.SESSION_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        self._max = config.MAX_SESSIONS if max_sessions is None else max_sessions

    def _touch(self, session_id: str) -> None:
        self._last_access[session_id] = time.monotonic()
        self._sessions.move_to_end(session_id)

    def _evict_expired(self) -> None:
        if self._ttl <= 0:
            return
        cutoff = time.monotonic() - self._ttl
        stale = [sid for sid, t in self._last_access.items() if t < cutoff]
        for sid in stale:
            self._sessions.pop(sid, None)
            self._last_access.pop(sid, None)

    def _enforce_cap(self) -> None:
        if self._max <= 0:
            return
        while len(self._sessions) > self._max:
            sid, _ = self._sessions.popitem(last=False)   # 最久未用（LRU 头部）
            self._last_access.pop(sid, None)

    def get(self, session_id: str) -> JarvisController:
        """取某会话的控制器，不存在则新建；顺带做惰性回收。"""
        self._evict_expired()
        c = self._sessions.get(session_id)
        if c is None:
            c = JarvisController()
            self._sessions[session_id] = c
        self._touch(session_id)        # 先标记最近端，再压容量，避免淘汰自己
        self._enforce_cap()
        return c

    def reset(self, session_id: str) -> JarvisController:
        """清空某会话历史（重建控制器），返回新的控制器。"""
        c = JarvisController()
        self._sessions[session_id] = c
        self._touch(session_id)
        self._enforce_cap()
        return c

    def discard(self, session_id: str) -> None:
        """连接结束时丢弃会话，避免内存无界增长。"""
        self._sessions.pop(session_id, None)
        self._last_access.pop(session_id, None)

    def count(self) -> int:
        return len(self._sessions)


@dataclass
class AppContext:
    """进程内共享上下文。启动时建一份挂到 app.state.ctx。"""
    sessions: SessionManager = field(default_factory=SessionManager)
