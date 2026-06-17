"""
（已移除）面向模型的记忆库工具 query_memory / write_memory / list_memory。

下线原因：精确 key 命中对 LLM 不友好、build_context_block 盲目注入最近 N 条记忆
更多是噪音；实际使用价值低。对话连续性改由 core/history.py（持久化 transcript +
跨设备回放 + 新会话回灌上下文）承担。

注意：底层的加密 KV 存储 core/memory.py 仍然保留——证件保险箱(connectors/vault.py)
和 connectors/availability.py 都复用它的 Fernet 加密与同一个 memory.db，不可删除。
本模块刻意不再注册任何 @tool，discover_connectors 导入它时为空操作。

此文件已无用，可由仓库维护者 `git rm connectors/memory_tools.py`。
"""
