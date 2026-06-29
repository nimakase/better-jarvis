# 贾维斯自我模型 · 代码边界

> 本文件由 `core/self_model.py` 自动生成，请勿手改。
> 改边界 = 改 `core/self_model.py`（它自身受保护，须人工审核）。

判据：**改了会动摇框架/安全的 = 核心（保护）；改了只影响某个业务好坏的 = 周边（开放）。**
未列入「开放」的一切默认按「保护」处理（fail-safe）。

## 🔒 PROTECTED（核心 · 不自动迭代；要改须人工 + 影响 + 动机）

| 路径 | 职责 |
|------|------|
| `core/self_model.py` | 边界定义自身；移动边界 = 拆护栏，必须人工 |
| `core/self_iteration.py` | 自我迭代执行器；若可自改即可拆掉自己的护栏，必须人工 |
| `tests/` | 测试是自动生效的安全网；若可自改即可作弊，必须人工 |
| `config.py` | 配置与密钥载入 |
| `main.py` | 装配入口；改坏即无法启动 |
| `core/registry.py` | 工具注册中心，全系统单一事实来源 |
| `core/controller.py` | 主控对话循环（大脑接线） |
| `core/context.py` | 会话管理（多会话隔离） |
| `core/results.py` | 结构化返回与带外动作通道（脱敏边界） |
| `core/safety.py` | 路径/文件名防穿越（安全边界本身） |
| `core/tool_builder.py` | 工具自建系统 + code_review 人工闸门（元能力） |
| `core/memory.py` | Fernet 加密 KV 基础设施 + 密钥管理 |
| `core/workflow.py` | 工作流引擎（轨道，不许改） |
| `core/scheduler.py` | 定时调度基础设施 |
| `connectors/vault.py` | 证件保险箱加密存储核心 |
| `connectors/credentials.py` | 证件工具（真实号绝不上云的边界） |
| `connectors/cred_ocr.py` | 证件本地 OCR（不上云） |
| `web/` | 传输层路由 + 证件揭示带外分发（安全管线） |

## 🟢 OPEN（周边 · 可自我迭代；测试绿才自动生效 + 可回滚）

| 路径 | 职责 |
|------|------|
| `connectors/document.py` | 读文档 |
| `connectors/doc_vault.py` | 文档保险箱 |
| `connectors/profile_tools.py` | 写用户档案 |
| `connectors/calendar_tools.py` | 日历工具 |
| `connectors/calendar_providers.py` | 日历数据源 |
| `connectors/calendar_card.py` | 日历情报卡 |
| `connectors/report_tools.py` | 报告工具 |
| `connectors/workflow_tools.py` | 工作流工具 |
| `connectors/delivery_control.py` | 投递控制 |
| `connectors/memory_tools.py` | （已中和的空模块） |
| `core/reports.py` | 报告类型注册 |
| `core/report_render.py` | 报告渲染 |
| `core/intel_cards.py` | 情报台卡片 |
| `core/calendar.py` | 内置日历业务逻辑 |
| `core/delivery.py` | 投递业务逻辑 |
| `core/availability.py` | 可用性/休假 |
| `core/profile.py` | 用户档案业务规则 |
| `core/history.py` | 对话存档业务规则 |
| `core/workflow_registry.py` | 工作流注册/运行记录 |
| `core/skill_policy.py` | 技能策略 |
| `intel/` | 情报层：信号库、工作流定义、领域 prompt/规格 |
| `prospecting/` | HubSpot 潜客匹配/富化流水线 |
| `skills/` | 运行时自建技能（已沙箱） |
| `frontend/` | PWA 前端（注：无测试覆盖，自动迭代暂不应触碰） |

---
未匹配任何条目的路径（如 `data/`、`deploy/`、`evals/`、新增文件）→ **默认 PROTECTED**。

