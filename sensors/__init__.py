"""
sensors/ — 感官层（持续可采样的「眼」，区别于被调用才动的 connectors「手」）

每个感官模块在 import 时向 core.world_state 注册采集器（拉取式 + TTL 缓存）。
在此集中 import，main 装配时 `import sensors` 一次即全部生效。
"""
from sensors import device  # noqa: F401
from sensors import health  # noqa: F401
