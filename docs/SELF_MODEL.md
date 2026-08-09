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
| `core/self_review.py` | 反思闭环编排；自改即可绕过路由/送审，必须人工 |
| `connectors/self_inspect.py` | 只读自省工具；改它=改自我认知入口，须人工 |
| `connectors/self_review_tools.py` | 反思触发工具；同上 |
| `tests/` | 测试是自动生效的安全网；若可自改即可作弊，必须人工 |
| `config.py` | 配置与密钥载入 |
| `main.py` | 装配入口；改坏即无法启动 |
| `core/registry.py` | 工具注册中心，全系统单一事实来源 |
| `core/controller.py` | 主控对话循环（大脑接线） |
| `core/context.py` | 会话管理（多会话隔离） |
| `core/results.py` | 结构化返回与带外动作通道（脱敏边界） |
| `core/safety.py` | 路径/文件名防穿越（安全边界本身） |
| `core/tool_builder.py` | 工具自建系统 + code_review 人工闸门（元能力） |
| `core/env_probe.py` | 造工具前的环境探针（驱动浏览器访问已登录页面，安全敏感） |
| `core/memory.py` | Fernet 加密 KV 基础设施 + 密钥管理 |
| `core/workflow.py` | 工作流引擎（轨道，不许改） |
| `core/scheduler.py` | 定时调度基础设施 |
| `core/effects.py` | 动作效应五级分类 + 不可逆确认闸；可自改即可拆闸 |
| `core/trust.py` | 数据信任分级 + 污染闸（防提示注入）；同上 |
| `core/spawn.py` | 子 agent 抽象：白名单授权+预算；可自改即可自我扩权 |
| `core/world_state.py` | 世界状态总线：产出注入 system prompt 的处境块（注入面） |
| `core/channels.py` | 渠道能力画像：同上，注入 system prompt（注入面） |
| `core/consolidation.py` | 记忆巩固：机械闸(证据/去重/限量/只软删)守档案完整性 |
| `core/drift.py` | 自我漂移检测：喂和解闸与冷却判据；可自改即可致盲护栏 |
| `core/llm.py` | 模型客户端单一构建点（超时政策统一处；改它影响全部模型调用） |
| `core/engineering.py` | 计划模式执行器：落地人已想清楚的多文件改动，可写PROTECTED边界须与self_iteration同源，须人工 |
| `connectors/engineering_tools.py` | 计划模式对话入口；改它=改「谁能触发落地」，须人工 |
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
| `connectors/entity_tools.py` | 实体记忆(L2)工具：查/记客户·料号等对象 |
| `connectors/episodic_tools.py` | 情节记忆(L4)工具：语义召回/记经过 |
| `connectors/calendar_tools.py` | 日历工具 |
| `connectors/calendar_providers.py` | 日历数据源 |
| `connectors/calendar_card.py` | 日历情报卡 |
| `connectors/report_tools.py` | 报告工具 |
| `connectors/workflow_tools.py` | 工作流工具 |
| `connectors/customer_loop_tools.py` | 客户循环夜间工作流注册（v2 编排接线；业务性质，可自我迭代） |
| `connectors/delivery_control.py` | 投递控制 |
| `core/reports.py` | 报告类型注册 |
| `core/report_render.py` | 报告渲染 |
| `core/intel_cards.py` | 情报台卡片 |
| `core/calendar.py` | 内置日历业务逻辑 |
| `core/delivery.py` | 投递业务逻辑 |
| `core/availability.py` | 可用性/休假 |
| `core/profile.py` | 用户档案业务规则 |
| `core/entities.py` | 实体记忆(L2)存储与查询业务规则 |
| `core/episodic.py` | 情节记忆(L4)存储与语义召回业务规则 |
| `core/embedding.py` | 本地文本嵌入工具（无安全不变量，纯计算） |
| `core/history.py` | 对话存档业务规则 |
| `core/workflow_registry.py` | 工作流注册/运行记录 |
| `core/skill_policy.py` | 技能策略 |
| `core/telemetry.py` | 执行遥测：工具调用画像/能力缺口日志 |
| `core/capability.py` | 能力索引：四源聚合 + 词面检索 + 先查后建查重 |
| `core/signals.py` | 监督信号打标：纠正/放弃/重试落库 |
| `core/artifacts.py` | 产物登记表 + 图书馆视图 + 生命周期 |
| `connectors/artifact_tools.py` | 产物图书馆工具 |
| `connectors/capability_tools.py` | 能力索引工具 |
| `connectors/spawn_tools.py` | 子 agent 派发工具（升权入口已被拿掉，无安全面） |
| `connectors/consolidation_tools.py` | 记忆巩固触发工具（薄壳） |
| `connectors/web_search.py` | 全局结构化搜索(AnySearch)+用量/降级；只读外部 |
| `connectors/web_fetch.py` | 网页正文抓取(Crawl4AI)+降级；只读外部 |
| `intel/` | 情报层：信号库、工作流定义、领域 prompt/规格 |
| `prospecting/` | HubSpot 潜客匹配/富化流水线 |
| `skills/` | 运行时自建技能（已沙箱） |
| `sensors/` | 感官层采集器：只产出短读数值，经 world_state 汇聚（业务性质） |
| `frontend/` | PWA 前端（注：无测试覆盖，自动迭代暂不应触碰） |

---
未匹配任何条目的路径（如 `data/`、`deploy/`、`evals/`、新增文件）→ **默认 PROTECTED**。

