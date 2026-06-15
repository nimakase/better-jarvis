"""
应用级上下文与会话管理

- SessionManager：按 session_id 隔离每个对话的 JarvisController，
  解决「全局单例 controller 导致多标签 / 多连接共享同一历史」的问题。
- AppContext：进程内共享对象的容器，启动时建一份挂到 app.state.ctx，
  路由通过它取依赖，便于测试与未来扩展（如注入调度器、配置等）。
"""

from dataclasses import dataclass, field

from core.controller import JarvisController


class SessionManager:
    """按 session_id 维护独立的对话控制器。"""

    def __init__(self):
        self._sessions: dict[str, JarvisController] = {}

    def get(self, session_id: str) -> JarvisController:
        """取某会话的控制器，不存在则新建。"""
        c = self._sessions.get(session_id)
        if c is None:
            c = JarvisController()
            self._sessions[session_id] = c
        return c

    def reset(self, session_id: str) -> JarvisController:
        """清空某会话历史（重建控制器），返回新的控制器。"""
        c = JarvisController()
        self._sessions[session_id] = c
        return c

    def discard(self, session_id: str) -> None:
        """连接结束时丢弃会话，避免内存无界增长。"""
        self._sessions.pop(session_id, None)

    def count(self) -> int:
        return len(self._sessions)


@dataclass
class AppContext:
    """进程内共享上下文。启动时建一份挂到 app.state.ctx。"""
    sessions: SessionManager = field(default_factory=SessionManager)
