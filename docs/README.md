# 贾维斯文档索引（单一事实源地图）

本文件是「哪份文档算数、哪份已归档」的唯一入口。新增/废弃文档时，**先更新这里**。

## 活文档（当前权威）

| 文档 | 位置 | 职责 | 是否被代码引用 |
|------|------|------|----------------|
| `ARCHITECTURE.md` | 仓库根 | **架构唯一权威**：分层、模块职责、数据流、注册表家族 | 否（纯文档） |
| `docs/SELF_MODEL.md` | docs/ | 代码边界（核心/周边）人类可读镜像 | 由 `core/self_model.py` 生成 |
| `分发与部署.md` | 仓库根 | 分发方式与配置总览 | 否 |
| `deploy/README.md` | deploy/ | Cloudflare 隧道配置单一事实源 | 否 |
| `内置日历设计.md` | 仓库根 | 内置日历设计（进行中） | `core/calendar.py` 注释引用 |

## 运行时活文件（**不是文档，勿动**）

这些 `.md` 被代码当 prompt / 规格在运行时读取，移动会断引用：

| 文件 | 被谁读取 |
|------|----------|
| `intel/prospect_generation_prompt.md` | `intel/workflow_defs.py`（运行时加载） |
| `intel/signal_collection_prompt.md` | `intel/workflow_defs.py`（运行时加载） |
| `intel/delivery_and_resilience_spec.md` | `connectors/delivery_control.py` 等引用 |
| `intel/prospect_pipeline_contract.md` | `prospecting/` 多处引用 |
| `intel/signal_library_spec.md` | `intel/signal_library.py` 引用 |

## 已归档（历史，仅供回溯）

见 `docs/archive/README.md`。

---

## 文档纪律

- **唯一权威是 `ARCHITECTURE.md`**；其余活文档是它的补充，不得与之冲突。
- 代码边界（哪些能自动迭代、哪些受保护）的事实源是 **`core/self_model.py`**，
  `docs/SELF_MODEL.md` 只是它的生成镜像——改边界改代码，不改 md。
- 一份文档一旦被实现取代或废弃，移入 `docs/archive/` 并在那里的 README 记一行。
