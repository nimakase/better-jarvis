# 贾维斯 vs DeepSeek Harness 架构对比与借鉴

2026-08-22，Ned 要求调研 DeepSeek 新发布的 Harness（对话代号「贾维斯」自建 agent 项目），
判断有没有可参考/可直接拿来用的部分，或者整个换过去是否更划算。

## 0. DeepSeek Harness 是什么

2026-08-17 前后，DeepSeek 以 developer preview 形式开源了 `dsh`（DeepSeek Harness），
MIT 协议，命令行工具 `npx @deepseek-ai/dsh web`，同期发布 V4-Pro-0813（API 提价）。
定位对标 Claude Code/Codex 这类"编码 agent 外壳"，发布 48 小时内 GitHub star 破 3 万+。
官方不接受外部 PR，把仓库定位成"一个示范和灵感来源，不是必须遵循的规范"，鼓励社区做插件。

来源：
- [DeepSeek Harness developer preview 官方页面](https://deepseek.com/harness/en/)
- [MarkTechPost：DeepSeek AI Releases DeepSeek Harness in Developer Preview](https://www.marktechpost.com/2026/08/17/deepseek-ai-releases-deepseek-harness-in-developer-preview/)
- [The New Stack：DeepSeek open sources an agent harness where everything is a plugin](https://thenewstack.io/deepseek-harness-open-source-plugins/)
- [VentureBeat：DeepSeek Harness launches as open source rival to Claude Code](https://venturebeat.com/technology/deepseek-harness-launches-as-open-source-rival-to-claude-code-alongside-v4-pro-on-api-with-higher-prices)

## 1. Harness 架构速览

**核心理念**："Agent = Model + Harness"——Harness 负责让模型感知环境、调工具、在真实
场景里持续工作，"everything is a plugin"：模型适配、工具、技能、会话、沙箱、存储、
主循环、调度、UI，全部是可插拔组件，没有一个"特权核心"需要改源码才能扩展。

**Cordis 内核**：一个专注"时空可组合性"的元框架，管插件的挂载/卸载/依赖解析。开发者
通过配置文件增删换任意能力层，不用碰 Harness 源码本身。

**四种运行模式**：Standard（文件编辑+shell+联网搜索+子agent+规划，全功能编码 agent）、
Code Mode（模型生成 TypeScript 程序而不是逐次 function call，走 SDK 编排多步操作）、
Minimal（只留 bash + 字符串替换编辑器，专门给 benchmark 用）、Creator（自定义 preset +
运行时插件实验）。

**Append-only session log**：单一事件流是唯一事实来源——模型看到的一切（system prompt、
推理轨迹、工具调用与结果、子 agent 调度、每次 context 注入）都落进同一条日志。resume/
fork/replay/search/transcript/telemetry/Web UI 全部从这一条流派生，不是各自维护一份
状态。

**OS 级沙箱**：Linux 用 Landlock（Node addon）、macOS 用 Seatbelt、Windows 用 ACL
受限 token runner——权限边界下沉到操作系统层，不只是应用层的逻辑判断。

**模型无关**：Anthropic/OpenAI/Bedrock/Azure/Gemini/自定义 OpenAI 兼容端点/DeepSeek
自己，配置文件切换即可热切，不需要重启进程。另带 Claude Code / Codex 子 agent 桥接、
`hooks.json` 兼容层、MCP 支持、`AGENTS.md`/`CLAUDE.md` 这类 markdown 配置读取——
明显是冲着"编码 agent 生态互操作"去的。

## 2. 逐项对照贾维斯现状

**扩展机制**：Harness 是单一 Cordis 插件核统管一切层；贾维斯是 ARCHITECTURE.md §10.1
写的"注册表家族"——工具（`core/registry`）、自建技能（`tool_builder`+`skills/`）、
定时任务（`core/scheduler`）、报告类型（`core/reports`）、情报台卡片（`core/intel_cards`）、
工作流（`core/workflow_registry`）、L1/L2/L4 三层记忆、日历来源、定时预设，一共十种
各自独立的注册表，各管一类能力，新增能力=在对应注册表里加一条。两者本质都是"避免
散落逻辑外溢"，区别是 Harness 用一个通用元内核统一了所有层（连"主循环"本身都是插件），
贾维斯是按贾维斯实际拥有的能力种类各开一张表，更窄但也更贴合现有产品形态。

**权限/安全模型**：Harness 靠 OS 级 sandbox（Landlock/Seatbelt/ACL）兜底，所以敢把
shell/文件访问这类高权限工具直接给子 agent。贾维斯完全是应用层护栏：`self_model.py`
按路径分 PROTECTED/OPEN、`effects.py` 给每个工具定五级效应（read_local→irreversible，
write_external 以上机械拦截要求隔轮确认）、`grants.py`+`permission_scan.py` 用 AST
静态扫描+SQLite 记录按模块授权、运行时 `check_tool()` 兜底拦截——这套体系刚在
2026-08-12 那次架构调整里补完（[[jarvis-architecture-migration-plan]]）。没有 OS 层
兜底是贾维斯迄今**刻意**不给子 agent 裸 shell 的核心理由（[[jarvis-vs-hermes-full-architecture-comparison]]
已经论证过一次，这次对 Harness 结论不变，见下）。

**可观测性/会话状态**：Harness 的 append-only session log 是"单一事实来源"，resume/
fork/replay 都从同一条流重放。贾维斯现在是 `core/trace.py` 的 trace_id 贯穿
telemetry/workflow/self_iteration 做关联，加 `logs/jarvis.log` 明文日志+
`read_recent_logs` 自查工具（[[log-visibility-and-self-diagnosis]]）——能定位问题，
但不是"从一条事件流就能完整重建/重放整个会话"这个强度，也没有 fork/replay 能力。

**模型路由**：Harness 配置文件热切换、不重启。贾维斯现状是两层：`core/model_routing.py`
按 purpose 在已启用的模型间路由，但"启用哪个 provider"这一级（`JARVIS_LLM_PROVIDER`）
是 `.env` 改完要重启进程才生效（刚完成的 DeepSeek 迁移就是这么切的，见
[[deepseek-migration-status]]）。

**子 agent 委派**：Harness 因为有 OS sandbox 兜底，子 agent 可以拿到更大权限。贾维斯
`core/spawn.py` 默认白名单只读，升权走 `extra_tools` 显式点名，对话入口不暴露——这个
选择在 vs Hermes 那次对比里已经定过（"贾维斯常驻在挂着证件保险箱+HubSpot登录态+飞书
长连接的个人机器上，给子agent裸shell的风险收益比不成立"），这次面对 Harness 结论一致：
Harness 敢给，是因为它有 Harness 自己不提供、贾维斯也没有的 OS 级隔离在兜底，不是
"子agent权限模型"本身更先进。

**产品定位差异（最关键的一条）**：Harness 是给开发者攒 agent 产品用的**底座工具包**——
四种 preset、Code Mode 的 TS SDK、Claude Code/Codex 兼容层，都是"帮你造别的 agent"
的基础设施，它自己不内置任何具体业务能力。贾维斯已经是**落地的具体产品**：客户循环
（HubSpot/Breeze）、潜客生成树、情报台卡片、飞书长连接、日历、定时任务体系、L1-L4
记忆分层——这些都是在 Harness 之上还要再造的应用层，Harness 本身完全不提供。

## 3. 结论

**3 处可参考/借鉴，1 处印证现状不用改，2 处刻意不学**：

1. **append-only session log 的设计原则值得借鉴**——不是抄 Cordis，是抄"单一事件流
   做唯一事实来源，resume/fork/replay 都从它派生"这个思路。贾维斯 `core/trace.py`+
   `workflow.py`+`view_manager.run_view_cycle` 的 resume 现在是各管一段，往这个方向
   收拢是真实的架构升级方向，也呼应 [[jarvis-next-gen-architecture-research]] 里已经
   提过的 durable execution/checkpoint 理论那条线，不是新话题，只是 Harness 给了一个
   具体的当代实现参照物。
2. **OS 级 sandbox 是贾维斯权限体系真正缺的一层，值得单独立项调研**——现在的
   self_model/effects/grants 三件套全在 Python 应用层判断，跟 Landlock/Seatbelt 这类
   操作系统级隔离不是一回事、也不能互相替代。如果以后计划模式/子agent权限要进一步放开
   （比如给子agent更大自主权），补上这一层是前提，不是可选项——这也是"子agent裸shell
   刻意不学"这条结论成立的边界条件，条件变了（贾维斯有了OS sandbox）结论要重新评估。
3. **多 provider 配置热切换值得抄，成本低收益明确**——贾维斯现在切 provider 要改
   `.env`+重启进程，Harness"配置里填个 key 立刻生效不重启"这个具体能力可以直接在
   `core/llm.py`/`config.py` 现有的 provider-agnostic 改造基础上加一层热加载，不需要
   引入插件内核。
4. **印证现状不用改**：Harness 强调"approval policy 也是可插拔的一层"，跟贾维斯
   `effects.py` ConfirmGate 的设计方向一致（都是"审批策略跟核心逻辑分层"），不需要
   因为 Harness 而调整。
5. **刻意不学①：Cordis 式全局插件内核**——贾维斯的"注册表家族"已经覆盖了贾维斯实际
   拥有的全部能力种类，成本更低。Harness 需要"一切皆插件"是因为它要当通用底座服务
   未知的下游产品；贾维斯自己就是下游产品，再往通用内核方向抽象是倒退成"给自己造
   一个还要自己再用的框架"，性价比不成立。
6. **刻意不学②：子 agent 直接给 shell/更大权限**——见上文分析，前提（OS sandbox）
   不具备之前不应该跟进。

**"整个拿来用"是否更划算：不建议**。三个硬理由：①Harness 是编码 agent 外壳，不含
任何贾维斯已经落地的业务能力（客户循环/潜客树/情报台/飞书/日历/定时任务/记忆分层），
换过去等于把这些全部按 Harness 插件形态重写一遍，工作量远超"抄几个设计思路"；②
developer preview，官方原话"会有破坏兼容性的变更"，且不接受外部 PR，作为贾维斯这种
长期跑在个人机器、接触真实凭据/CRM 登录态的常驻服务的底座，稳定性和长期可控性都不够；
③贾维斯现有的护栏体系（self_model/effects/grants/spawn 白名单）是针对贾维斯自己的
风险场景（个人机器、真实登录态、7x24常驻）反复踩坑调出来的，直接套 Harness 的通用
护栏（尤其是依赖 OS sandbox 兜底放开子agent权限那部分）反而可能在贾维斯还没补上 OS
sandbox 这层的情况下变得更不安全。

**Why**：避免看到"3万星开源项目"就无脑迁移，也避免因为它新就忽略"贾维斯已经是具体
产品、Harness 是造产品的底座"这个根本性的定位差异。
**How to apply**：下次要做「session log 统一化」「OS sandbox 调研」「provider 热切换」
这三项工期排期时，先读这条摘要判断优先级和范围，不用重新调研 Harness；除非 Harness
出了稳定版且贾维斯真的要往"给子agent更大自主权"发展，否则不用重新评估"整体迁移"
这个选项。

另见 [[jarvis-vs-hermes-full-architecture-comparison]]、[[jarvis-next-gen-architecture-research]]、
[[jarvis-architecture-migration-plan]]、[[deepseek-migration-status]]。
