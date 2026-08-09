# 贾维斯 vs Hermes Agent：架构对比与借鉴（2026-08-09）

> 起因：`jarvis-lacks-plan-mode-engineering-capability`（2026-08 上旬）诊断出贾维斯缺"任务拆解+长工作流"能力后，
> Ned 提出更根源的问题——把贾维斯和 Hermes Agent（Nous Research 的开源自我进化 agent）做一次更全面的对照，
> 看还有哪些地方该学、避免关起门来重复造轮子。本文档是这次对照的结论，覆盖面比此前只聚焦"计划模式"更宽。
>
> 信息来源见文末。Hermes 的官方文档站/GitHub README 本次仍抓取失败（客户端渲染），结论基于 WebSearch 摘要，
> 细节（如具体配置项名称）以 Hermes 官方文档为准，不要盲目照抄字面实现。

---

## 一、结论先行

三件事值得学、一件事刻意不学：

1. **【值得学·未开始】任务分解 + 长工作流（对应 Hermes 的 Kanban/任务图）**——已在
   `jarvis-lacks-plan-mode-engineering-capability` 里设计过一版（单条 Proposal 列表 + 一次性
   ConfirmGate 确认），本文档确认这个方向没错，但给出一个来自 Hermes 的修正：Hermes 用的是**任务图
   （谁依赖谁）+ 每个叶子可能派给独立子 agent**，不是一个扁平列表。贾维斯要不要做成图结构，取决于任务
   #2（子agent委派）做到什么程度——见下文详述。
2. **【值得学·全新发现，之前没意识到】程序性记忆自动萃取**——这是本次对照挖出的、`jarvis-lacks-plan-mode`
   那次没覆盖到的东西。Hermes 会在任务成功后自动把"这次是怎么解决的"提炼成一条可复用的 skill，下次遇到
   相似问题直接复用；贾维斯现在的 `tool_builder.py` 只有"模型主动决定要不要造工具"这一条路，没有"事后回顾、
   把成功经验固化"这一环。细节见下文第 2.4 节。
3. **【值得学·中优先级】命令级审批粒度**——贾维斯的 `core/effects.py` 只能按**已注册工具的名字**分级
   （`effect_of(tool_name)`），一旦贾维斯将来真的拿到 shell/任意文件写入能力（计划模式要用到），"这个工具
   名字叫 write_file，但这次具体在写哪个路径/执行哪条命令"这层判断现在的机制看不见。Hermes 是按**命令/路径
   的具体内容**过一层危险模式匹配，粒度更细。贾维斯扩权前应该先补这层，否则 ConfirmGate 会变成"一刀切走过场"。
4. **【刻意不学】子agent进程级委派（独立终端 + Python RPC）**——Hermes 的子 agent 是"派一个初级工程师去干活"：
   独立会话、独立终端、能跑任意 shell 命令。贾维斯的 `core/spawn.py` 是进程内受限白名单（默认只读、显式点名
   才能升权、天生 `interactive=False`）。**这个差距是刻意的，不是欠缺**——贾维斯跑在个人机器上、挂着证件
   保险箱和 HubSpot 登录态，给子 agent 独立终端等于给了它 shell 全权，风险收益比对个人助理不成立。见第 2.3 节。

---

## 二、逐维度对比

### 2.1 任务分解与长工作流（计划模式）

| | 贾维斯现状 | Hermes |
|---|---|---|
| 拆解 | **无**——`self_iteration.Proposal` 是单文件、一次性、由 `self_review.py` 里单次 `model_fn(prompt)` 生成，无法表达"改 A 前必须先改 B" | 有专门的 decomposer：读已安装的 profile 描述 → LLM 产出 JSON **任务图**（哪些任务、派给谁、依赖关系）；原始任务作为父任务，图跑完才算完 |
| 执行 | 无编排层；`workflow.py`/`workflow_registry.py` 有"多步骤工作流"但节点是**代码里预先写死的 Step 序列**（如 `prospect_daily`），不是模型临时生成的任务图 | 每个叶子任务可能派给独立 profile/子 agent 执行，子任务间通过父任务感知完成状态 |
| 确认闸 | `core/effects.py` 的 `ConfirmGate`——按**单次工具调用**为粒度拦截，天然不知道"这是一整个计划里的第 3 步" | 有独立的审批子系统，approvals.mode 三档（off/手动/仅危险模式），按**命令内容**判断，不按"是第几步" |

**结论**：贾维斯目前连"扁平的多文件计划"都没有，谈不上"要不要图结构"。`jarvis-lacks-plan-mode-engineering-capability.md`
里设计的方案（人工/Claude 给出完整多文件 Proposal 列表 → 复用 `SelfIterator.execute()` 逐文件先红后绿 → 一次
`ConfirmGate` 确认后连续执行）仍然是对的**起步方案**：贾维斯是个人助理，日常改动大多是"改 3 个文件、彼此没有
条件依赖"，扁平列表够用，没必要一上来抄 Hermes 的任务图。图结构值得留到**任务确实需要跨子任务依赖**（比如
"先起草 A 方案的 3 个候选、再根据用户选的那个改 5 个文件"）时再加，且应该建在任务 #2 子agent委派做实之后
——没有能独立干活的子任务执行者，任务图只是摆设。

### 2.2 写权限与安全护栏

| | 贾维斯 | Hermes |
|---|---|---|
| 文件级 | `core/self_model.py`：显式 PROTECTED/OPEN 两张表，**未列入 OPEN 一律 fail-safe 按 PROTECTED**；`classify(path)` 精确文件优先于目录前缀 | denylist（精确路径如 `~/.ssh/authorized_keys`、前缀如 `~/.aws/`）+ 可选 `HERMES_WRITE_SAFE_ROOT` 白名单目录树；写前先查 denylist，命中直接拒绝、无审批可覆盖 |
| 动作级 | `core/effects.py`：五级效应（read_local→irreversible），`write_external`起步走 `ConfirmGate`（隔一个用户回合才放行） | 按具体命令内容匹配"危险模式"列表，三档审批模式 |

**结论**：这次对照**再次验证**（`jarvis-lacks-plan-mode-engineering-capability.md` 已提过一次）贾维斯的
fail-safe-by-default 设计（未知路径默认按最严处理）比 Hermes 的"denylist 命中才拦、其余放行"更保守也更简单
可推理——继续保持，不用改。**唯一的缺口**：`effects.py` 只按**工具名字**查表，粒度停在"这是不是
write_external 类工具"，看不到"具体在改哪个路径/跑哪条命令"。今天没问题是因为贾维斯没有任何"传入任意路径
的通用写工具"（`update_document`/`calendar_update_event` 这些都是业务语义明确的窄工具）；**一旦计划模式给出
`write_open_file(path, content)` 这类通用工具，这个粒度缺口就会变成真问题**——`effects.py` 那层只能判断
"这是个写本机的工具"，判断不了"这次具体写的 `core/controller.py` 其实是 PROTECTED"（那一步要靠
`self_model.classify()` 另外把关，两层护栏不能合并成一层）。这已经是 `jarvis-lacks-plan-mode` 设计草案里
`write_open_file` 会先查 `self_model.is_writable_by_self_iteration()` 的原因，本次对照确认这个设计方向对，
需要在实现时把"按内容/路径二次判断"作为硬性要求写进函数本身，不能只在工具描述里嘱咐模型自觉。

### 2.3 子agent / 任务委派

| | 贾维斯 `core/spawn.py` | Hermes |
|---|---|---|
| 隔离级别 | 进程内：新建一个白名单受限的 `JarvisController` 实例 | 进程级：独立会话 + 独立终端 + Python RPC |
| 默认权限 | 只读（`effect ≤ read_external`），升权须调用方逐个点名 `extra_tools`，且这个入口**未对模型暴露**（`connectors/spawn_tools.py` 只给对话层调用只读版本） | 能跑任意 shell 命令（受审批闸限制，但闸本身可配置成 `off`） |
| 预算 | 轮次上限 + 墙钟超时，超时不算失败、交回部分结果 | 未搜到明确对应机制说明 |
| 结果契约 | 强制 JSON `{conclusion, evidence, uncertainties, unfinished}`，解析失败降级为塞进 conclusion | 子agent是"独立会话"，结果通过父任务感知完成状态，粒度更粗 |

**结论**：这一条**刻意不学**。Hermes 面向的是"愿意让 agent 用 shell 干活"的开发者/CI 场景，贾维斯是挂着
证件保险箱、HubSpot 登录态、飞书长连接的个人机器常驻进程——给子agent独立终端等于给它 shell 全权，一旦
prompt injection 或模型幻觉出个 `rm -rf`/垃圾邮件式外呼，损失是真实、不可逆的。贾维斯现有的"白名单derive
自 effects 分级 + 显式点名升权 + 升权入口不对模型暴露"这套，是用**更小的能力半径**换**更高的确定性**，
这个取舍对个人助理场景是对的，不应该因为"Hermes 更强大"就跟进。如果未来真需要"子agent能改代码"，正确
的加强方向是**扩大 `extra_tools` 里能点名的工具种类**（比如未来的 `write_open_file` 本身），而不是给
子agent一个不受 registry 约束的裸 shell。

### 2.4 记忆：程序性记忆自动萃取（新发现）

这是本次比对之前没被专门提出过的差距，值得单独展开：

**Hermes 怎么做**：memory.md（episodic，"发生过什么/被纠正过什么"）与 SKILL.md（procedural，"怎么做某类事"）
分层维护；agent 在任务过程中会被**周期性 nudge**去把有用的经验固化下来，成功解决一个新问题后倾向于把"这次
是怎么解决的"提炼写成一条 skill，下次遇到同类任务直接复用，不用重新摸索。

**贾维斯现状**：
- L1 `profile.py`（用户档案）+ L2 `entities.py`（对象事实）+ L4 `episodic.py`（语义召回过往经过）——三层都是
  **关于世界/用户的事实**，没有一层是"关于我自己怎么解决问题的经验"。
- `tool_builder.py` 的造工具闭环是**模型主动判断"这个需求值得固化成工具"再动手**，触发靠模型当场决定，
  不存在"事后复盘一次成功的多步骤任务解决过程、自动提炼流程"这一步。
- `self_review.py` 的反思闭环是**读代码找缺陷**（bugfix/robustness/correctness），目标是代码质量，
  不是"把一次成功的问题解决过程变成可复用资产"。

**差距的实际后果**：贾维斯每次遇到"以前处理过的相似复杂任务"（比如一次需要连续调 5 个工具、想清楚 3 个
判断分支的操作），都是从零重新推理，即使上次已经趟出了正确路径。Hermes 的做法本质是"把 agent 自己蹚出来
的过程性知识也当成一等公民存下来"，而不只是存事实。

**建议**（暂不实现，留给后续路线图排期）：可以在**情节记忆(L4)基础上**加一类特殊标记的记录——当一次对话
成功完成一个多步骤/多工具的复杂任务后（比如"跑通了一次此前没做过的组合调用"），由模型判断是否值得写一条
"下次遇到类似任务可以这样做"的程序性摘要，存进 episodic 或新开一张表，`recall()` 命中时connector 层面
可以额外标注"这是一条过程经验，不是事实"。不需要抄 Hermes 的"SKILL.md 文件"具体实现——贾维斯已经有
`core/episodic.py` 这套语义召回基础设施，加一个来源标签/类型字段成本远低于另起一套文件系统。这条比计划模式
优先级低（计划模式是"用户当下卡住的真实需求"，这条是"锦上添花的长期效率"），列入路线图但不抢跑。

### 2.5 记忆：情节记忆检索机制（次要，不建议改）

贾维斯 L4 用本地 embedding（fastembed，缺模型时降级字符 3-gram 哈希）+ numpy 余弦；Hermes 用 SQLite FTS5
全文检索 + LLM 摘要后再注入。两者目标相同（"别把整段旧对话塞回窗口，只捞相关的"），实现路线不同，各有
优劣（embedding 抓语义相似但需要模型/計算，FTS5 抓关键词精确但语义弱、多一次 LLM 摘要调用的延迟和花费）。
贾维斯当前量级（单用户、几千条量级）用 embedding 方案没有性能问题，**不建议为了"和 Hermes 一样"去换成
FTS5**——这属于"实现选型不同但解决的是同一个问题"，没有谁明显更优，换了也拿不到实际收益。

### 2.6 多渠道接入 / 定时自动化（已对齐，无需借鉴）

Hermes 提到的"多渠道存在 + 定时自动化"，贾维斯已经有对应能力且成熟度不低：`lark_bridge.py`（飞书长连接，
守卫式启动、故障不拖垮主服务）对应"多渠道"，`core/scheduler.py` + `core/schedule_presets.py`（APScheduler +
可视化预设目录）对应"定时自动化"。这一条不需要额外借鉴动作。

---

## 三、给路线图的建议顺序

1. **计划模式起步版**（扁平多文件 Proposal + 单次 ConfirmGate 确认，复用 `SelfIterator.execute()`）——
   `jarvis-lacks-plan-mode-engineering-capability.md` 已有设计草案，本文档确认方向不变，**新增一条实现
   约束**：`write_open_file` 类通用写工具必须在函数内部强制二次调用 `self_model.is_writable_by_self_iteration()`，
   不能只依赖 `effects.py` 的粗粒度工具名分级（见 2.2 节）。用户此前说"稍后再做"，路线图里排在这，不代表
   现在启动。
2. **程序性记忆萃取**（2.4 节新发现）——优先级低于计划模式，可以晚一些排期，成本不高（复用 episodic 基建
   加类型字段），收益是长期的，不紧急。
3. **任务图/子agent委派升级**——明确排在最后，且只有当"贾维斯真的开始频繁处理带依赖关系的多步任务"时才
   值得做；子agent的能力半径扩大要跟着"扩大 `extra_tools` 可点名的工具种类"走，不要照抄 Hermes 的裸终端模式
   （2.3 节，安全取舍是刻意的）。

---

## 四、信息来源

Hermes Agent 官方文档站点渲染依赖 JS，本次抓取仍失败（`SyntaxError: Unexpected token '<'`），以下结论来自
WebSearch 摘要，字面配置项名称/细节请以 Hermes 官方文档实测为准：

- [DeepWiki: NousResearch/hermes-agent](https://deepwiki.com/NousResearch/hermes-agent)
- [DeepWiki: Security and Command Approval](https://deepwiki.com/NousResearch/hermes-agent/5.4-security-and-command-approval)
- [Hermes Agent 官方文档 · Security](https://hermes-agent.nousresearch.com/docs/user-guide/security)
- [Hermes Agent 官方文档 · Persistent Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- [Hermes Agent 官方文档 · Kanban](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban)
- [Hermes Agent 官方文档 · CLI](https://hermes-agent.nousresearch.com/docs/user-guide/cli)
- [The Hermes Kanban: A Complete Guide to Multi-Agent Task Orchestration](https://magnus919.com/2026/05/the-hermes-kanban-a-complete-guide-to-multi-agent-task-orchestration/)
- [Inside Hermes Agent: How a Self-Improving AI Agent Actually Works](https://mranand.substack.com/p/inside-hermes-agent-how-a-self-improving)
- [Hermes Agent Memory System: Curated Memory, Session Search, and Self-Improvement](https://medium.com/@xpf6677/hermes-agent-memory-system-curated-memory-session-search-and-self-improvement-a84d2a9d5d01)
- [Better Stack: Hermes Agent — Persistent Memory, Dynamic Skills, and Self-Improvement](https://betterstack.com/community/guides/ai/hermes-agent/)
- [GitHub: hermes-agent SECURITY.md](https://github.com/NousResearch/hermes-agent/blob/main/SECURITY.md)
- [GitHub Issue #49535: Support multiple HERMES_WRITE_SAFE_ROOT directories](https://github.com/NousResearch/hermes-agent/issues/49535)
