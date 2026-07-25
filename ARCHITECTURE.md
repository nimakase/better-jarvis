# 贾维斯（Jarvis）架构文档

> 个人 AI 助理 · 本地优先（local-first）· 可安装 Python 包 · **当前版本 v1.0.0**
>
> 🏁 **v1.0.0 里程碑（2026-07-18）**：首个正式大版本。全量代码审查后的整理提交：
> 修复 env probe 路径缺 `import asyncio` 的 NameError；清除全部无用 import 与死赋值
> （pyflakes 零告警，controller/main 两处有意保留的注册用导入除外）；删除已失效的
> `start.bat`（探测的还是 anthropic 包）；`docs/tool_authoring_redesign.md`（一~三期
> 方案，已全部实施）归档；`pyproject` 版本 0.1.0 → 1.0.0。测试基线 11/11 全绿。
>
> 更新日期：2026-07-18 · 本次改动：
> · **造工具框架四期——从「能跑」到「跑得对」**（诊断见 `docs/tool_authoring_diagnosis.md`）。
>   起因：自建的 `oem_ems_screener` 通过了全部三道门（静态校验 / API 一致性 / 子进程冒烟），
>   真跑却列索引错位、静默只抓第一页、`input()` 卡死。根因是前三期只解决**存在性**问题
>   （方法在不在、能不能 import），而这些缺陷全属**语义正确性**问题。三处改动：
>   ① `skill_policy.BUILDING_BLOCKS` 注入面从「只给公共签名」扩为 **签名 + 常量取值 +
>   显式暴露的私有复用方法 + 实例属性 + 语义约定(notes) + 可抄范例(recipes)**——此前
>   `_extract_name`/`_column_idx` 这些**正确用法全是私有方法、被过滤掉了**，模型看不见自然只能自造；
>   ② 新增 `check_runtime_contract` 阻断 `input`/`getpass`/`breakpoint`（技能跑在服务进程里没有终端），
>   并在 `CODE_GEN_PROMPT` 加【运行环境契约】与【结果诚实性】两节（后者要求如实报告覆盖范围、
>   判定类须有第三态——静默数据截断是最贵的缺陷）；
>   ③ `config._export_env_for_skills` 增加 `JARVIS_DATA_DIR`/`JARVIS_DOWNLOAD_DIR`，
>   技能不再靠猜仓库根定位运行态目录（此前会另建 chrome_profile → 登录态不共用）。
> · **造工具补上两条「接触现实」的通道**——复盘发现它写不好工具主要不是不会写代码，
>   而是①看不到目标环境 ②看不到执行结果 ③不会追问需求。本轮补了后者相关的两项：
>   `_user_context_text()` 把**用户档案注入造工具提示词**（此前提示词只有「工具名+需求」，
>   模型不知道用户做什么生意，只能照需求字面直译判据）；`_gather_clarifications()` 加了
>   **需求澄清门**，写代码前只问「猜错会产出静默错误结果」的点（规模分页/业务判据/
>   关键取舍/失败处理），并靠 `clarifications` 参数保证**只问一轮**、默认不问、
>   每问必带默认假设。
> · **补上另外两条通道**：`SELFTEST` 钩子——工具可选声明的自检函数，在隔离子进程里
>   **真的执行**（此前冒烟只 import 不执行 handler，"能加载但结果是错的"全部逃逸）；
>   `check_selftest_quality` 把「有 SELFTEST 却零断言」判为阻断级（空测试制造"已验证"的
>   错觉，比没有更糟）。新增 `core/env_probe.py`（🔒 PROTECTED）——**环境探针**：
>   需求里带 URL 时，造工具前先 headless 打开该页抽一份**结构摘要**（翻页控件真实属性、
>   表头及定位属性、行骨架、页面自报总数）注入提示词；无 URL 则零成本跳过。只读、
>   不点击不填表、URL 只取自用户需求（防注入）。实测其输出直接覆盖了本轮全部三个抓取
>   类 bug 的成因。开关 `JARVIS_TOOL_AUTHOR_PROFILE` / `JARVIS_TOOL_AUTHOR_CLARIFY` /
>   `JARVIS_TOOL_ENV_PROBE`。护栏由 `tests/test_skill_policy.py`（85 项断言）钉死。
>
> 更新日期：2026-07-16 · 本次改动（多批）：
> · **自建工具读/审/激活闭环**——新增 `read_tool_code`/`review_tool`/`activate_tool` 三个元工具：模型可按名读自建工具源码（消除"我读不了 py"幻觉）、随时重弹审查、对话内直接激活草稿，飞书通道也能完整闭环（`lark_bridge` 的 `code_review` 改为真正下发代码卡片）；`create_tool` 静态校验有阻断级错误时自动喂回重生成一次。
> · **文档读取本地优先 + 云端 OCR 兜底**——`read_document` 先本地读；扫描件（无文字层）按敏感度分流：非敏感→OpenRouter `file-parser`（mistral-ocr）云端 OCR、敏感→仅本机并诚实报错、存疑→先问用户（`cloud=allow/deny` 重调）。新增 `core/sensitivity.py`（硬规则+轻模型判定）与 `PDF_CLOUD_FALLBACK/ENGINE`、`SENSITIVITY_LLM` 开关；本地空提取一律明确报错，不再静默假成功。
>
> 更新日期：2026-07-12 · 本次改动（多批）：
> · **记忆分层扩展**——新增**实体记忆 L2**（`core/entities.py`，精确查对象事实）+ **情节记忆 L4**（`core/episodic.py`＋本地嵌入 `core/embedding.py`，语义召回，长对话压缩摘要自动落盘）+ 记忆工具 `connectors/{entity,episodic}_tools.py`。
> · **飞书实时通道**——`lark_bridge.py`（官方 lark-oapi 长连接，后台线程 + 桥接回主循环），`main.lifespan` 守卫式启动，凭据入 `.env`。
> · **主循环健壮性**——模型调用加超时（防 600s 静默长挂）；`_compress_history` 摘要调用超时即优雅退化；工具轮次上限 12→30（env 可调）+ 重复无进展的卡循环检测；嵌入落盘一律 `asyncio.to_thread` 后台化，绝不阻塞事件循环。
> · **信号采集修复**——采集改直连高 token 调用 + 抢救式 JSON 解析（容忍截断）+ 0 信号明确报错（`intel/workflow_defs.py`）。
> 2026-07-11 · 内置日历（时间真源）+ 自我迭代反思闭环（self_review/self_iteration）+ 定时任务预设目录（schedule_presets）+ 文档保险箱 REST（web/documents）+ 部署隧道单一事实源（deploy/）+ 删除已下线模块（memory_tools/feishu/signal_intel/connectors.availability）

---

> 📌 本文件是架构的**唯一权威**。文档总索引见 `docs/README.md`；代码的核心/周边边界（自我迭代用）以 `core/self_model.py` 为事实源、镜像见 `docs/SELF_MODEL.md`。已废弃文档在 `docs/archive/`。

## 1. 一句话定位

一个跑在本机的个人 AI 助理：FastAPI + WebSocket 后端，PWA 单页前端（聊天 + 右侧功能抽屉），主控模型走 OpenRouter（DeepSeek V4 Flash，`:online` 联网）。核心能力：**持久化对话历史**（跨刷新/设备回看）、**分层记忆**（常驻用户档案 core memory + 按需的实体记忆 L2 精确查询 + 情节记忆 L4 本地向量语义召回）、证件保险箱、文档保险箱、文档读取、模型自建工具、定时任务、**可扩展报告框架**、**卡片化情报台**、**工作流引擎与注册表**（潜客→HubSpot）。高敏感数据（证件真实号码）严格隔离本机，绝不进对话历史/云端。

现已是可安装项目（`pip install -e .` / wheel / Docker），配置走 pydantic-settings。

> 架构主线：**一套同构的"注册表"**贯穿全系统——工具(`registry`)、自建技能、定时任务(`scheduler`)、报告类型(`reports`)、情报台卡片(`intel_cards`)、工作流(`workflow_registry`)。每类能力都"注册即可发现/可扩展"，新增一项 = 注册一条、各层零散逻辑不外溢。详见第 10 节。

---

## 2. 技术栈

| 层 | 选型 |
|----|------|
| Web 框架 | FastAPI + Uvicorn |
| 实时通信 | WebSocket（`/ws/chat` 流式对话） |
| 前端 | 单页 PWA（`frontend/index.html` + Service Worker） |
| LLM 接入 | OpenRouter（OpenAI 兼容 SDK `AsyncOpenAI`） |
| 主控模型 | `deepseek/deepseek-v4-flash:online`（`:online` 启用联网） |
| 配置 | pydantic-settings（env / `.env`，类型化校验） |
| 持久化 | SQLite（`memory.db`，单库多表） |
| 加密 | `cryptography` Fernet，密钥存系统钥匙串（`keyring`） |
| 定时 | APScheduler（`AsyncIOScheduler`，挂在 FastAPI 事件循环上） |
| 日历重复 | python-dateutil（`rrule.between` 区间展开重复事件） |
| 文档解析 | pdfplumber / python-docx / openpyxl / python-pptx（可选依赖） |
| 本地 OCR | rapidocr-onnxruntime（可选依赖，纯 CPU） |
| 打包/分发 | setuptools（`pyproject.toml`）+ wheel + Dockerfile |

---

## 3. 分层结构

```
                          浏览器（PWA 单页 + 右侧抽屉）
       localStorage 持久化 session_id；加载时拉 /api/history 回放历史（跨设备续聊）
                                │
        WebSocket /ws/chat      │      REST /api/*
                                ▼
┌───────────────────────────────────────────────────────────┐
│  main.py  ── 仅装配：注册工具 + 建 AppContext + 挂载路由       │
└───────────────────────────────────────────────────────────┘
        │                       │                        │
        ▼                       ▼                        ▼
┌──────────────┐      ┌──────────────────┐     ┌──────────────────┐
│ web/          │      │ core/             │     │ connectors/       │
│ 传输层(路由)   │      │ 引擎与基础设施     │     │ 第一方工具(@tool) │
│              │      │                  │     │                  │
│ pwa          │      │ registry  ←──────┼─────│ document         │
│ chat (WS)    │─ctx─▶│ context(Session) │     │ credentials      │
│ credentials  │      │ controller       │     │ doc_vault        │
│ skills       │      │ results          │     │ profile_tools    │
│ history      │      │ memory  safety   │     │ vault / cred_ocr │
│ files        │      │ scheduler        │     └──────────────────┘
└──────────────┘      │ tool_builder ────┼──▶ skills/<名>/tool.py
                      └──────────────────┘     （运行时自建·沙箱·不可信）
                                │
                  全部工具汇入 core/registry（单一事实来源）
```

启动装配（`main.py`）：导入 `tool_builder` 即自注册元工具 → `registry.discover_connectors()` 扫描并导入 `connectors/` 全部模块（`@tool` 自注册）→ `load_all_active_skills()` 加载已激活的自建技能。所有工具最终都进入 `core/registry`，对模型呈现为统一的 OpenAI function-calling 列表。

---

## 4. 工具系统（统一注册中心）

工具有四个来源，但**注册与分发完全统一在 `core/registry.py`**：

| 来源 | 位置 | 数量 | 信任级别 |
|------|------|------|----------|
| 元工具 | `core/tool_builder.py` | 15 | 第一方（管理系统自身） |
| 连接器 | `connectors/document/credentials/doc_vault/delivery_control/report` | 多组 | 第一方（外部 API / 业务） |
| 自建技能 | `skills/<名>/tool.py` | 运行时可变 | **不可信·沙箱** |

> 注：面向模型的「记忆库」工具（`query_memory`/`write_memory`/`list_memory`）已于
> 2026-06-17 下线（精确 key 命中对 LLM 不友好、盲目注入更多是噪音）；对话连续性改由
> `core/history.py` 持久化 transcript 承担。空壳模块 `connectors/memory_tools.py` 已于
> 2026-07-11 `git rm` 删除。底层加密 KV 存储 `core/memory.py` 仍保留（vault / availability 依赖）。

- `@tool(name, description, input_schema)`：装饰业务函数即自注册，无需 `TOOL_DEFS`/handler 包装/`register_*` 样板。
- `ToolSpec`：一个工具的完整描述（名称/说明/schema/handler）。
- `register_tool(definition, handler)`：兼容旧的 dict 写法（自建技能加载仍用）。
- `discover_connectors()`：导入 `connectors/` 全部模块触发自注册。
- 关键边界：第一方工具（深访问内部模块、解密证件）与不可信自建技能（被 `validate_tool_code` 沙箱、禁止 import `core/connectors/config` 等）**刻意分离**，不可合并。

---

## 5. 模块职责

### 5.1 `main.py` — 装配入口（约 2KB）
建 `FastAPI(app)`、挂 `app.state.ctx = AppContext()`、`include_router` 装配 `web/` 各路由、`lifespan` 启停调度器。`main()` 用 `config.HOST/PORT` 启动 uvicorn（控制台命令 `jarvis`）。

### 5.2 `web/` — 传输层路由
按域拆分的 `APIRouter`：`pwa`、`chat`（`/ws/chat` + 带外动作分发）、`credentials`、**`documents`**（`/api/documents` 文档保险箱 CRUD）、`skills`、**`history`**（`/api/history` 回放/清空 + `/api/health`，取代原 `memory` 路由）、`files`、`schedules`、`push`、`intel`、`hubspot`、`reports`、`workflows`、**`calendar`**（`/api/calendar` + `/api/calendar/agenda` 统一时间轴）。

`chat.py` 关键：消费 `controller.chat()` 的结构化事件（text→`chunk` 帧、tool→`tool_status` 帧）；每轮把 user / assistant 文本落盘到 `core/history`（单一主对话 `CONVERSATION_ID`），新控制器内存为空时用历史回灌上下文。客户端用 `localStorage` 持久化 `session_id`（跨标签/设备续聊）。聊天结束 `drain_actions()` → `_dispatch_actions` 分发；文件卡片安全持久化、证件揭示绝不入库。

**前端单页 + 右侧抽屉**：`frontend/index.html` 是常驻聊天页，头部「情报台 / 定时任务 / 保险箱 / 设置」统一开同一个右侧 slide-over 抽屉（`#drawer`，标签切换面板），不再整页跳转——WS 不断、对话不丢。原 `/intel`、`/schedules` 独立页保留可直达，但内容已迁入抽屉面板复用同一批 REST API。「记忆库」按钮已移除。

### 5.2a `lark_bridge.py` — 飞书实时通道（新增 · 2026-07-12）
除网页 WebSocket 外的**第二条入站通道**：让用户直接在飞书里和贾维斯对话。用飞书官方 **lark-oapi 长连接**（`lark.ws.Client`）——飞书长连接是官方 SDK 封装的私有握手协议，无法手写 wss 端点对接，故必须用 SDK。集成三要点，均为「不阻塞主服务」而设计：① SDK 的 `ws.Client.start()` 阻塞 → 放**后台守护线程**，并给该线程**独立的新事件循环**（否则会抢到正在运行的主循环报 `event loop is already running`）；② 事件回调是同步、在 SDK 线程里触发 → 用 `run_coroutine_threadsafe` **桥接回主事件循环**去 await 现有 `controller.chat()`；③ 发/更新卡片是同步 HTTP → `asyncio.to_thread` 包一层。每个飞书用户一个独立会话（`lark_<open_id>`，与网页会话隔离，但共享全部长期存储：档案/实体/情节/日历/保险箱）。由 `main.lifespan` **守卫式启动**：仅当 `config.FEISHU_APP_ID/SECRET` 都配置时才起，且整段 `try` 包裹——飞书任何问题绝不拖垮主服务。凭据入 `.env`，不硬编码。依赖 `lark-oapi`（注意其长连接依赖 protobuf<4.21.1，可能与 onnxruntime/fastembed 冲突）。

**能力对齐网页（目标：尽量替代前端）**：① **对话持久化 + 回灌**——飞书对话按 `lark_<open_id>` 落 `core/history`，新会话/回收后 `_seed` 从历史回灌，跨重启续聊；② **带外动作对等分发**——`file_download` 走飞书**图片/文件消息**上传投递（`im.v1.image/file.create`）；③ **证件安全边界**——`credential_reveal` 在飞书侧**一律拒绝**（真实号码绝不经飞书云端，引导去本机网页），`code_review` 提示需本机网页操作；④ `/reset` 清空该用户会话与历史。差异（当前有意保留）：飞书无逐字流式（占位卡片→整段更新覆盖）、不显示工具进度。

### 5.3 `core/registry.py` — 工具注册中心
见第 4 节。全项目工具的单一事实来源。

### 5.4 `core/results.py` — 结构化工具返回
`ToolResult(text, actions)` + `Action(type, payload)`。`text` 进对话历史给模型看（脱敏/确认），`actions` 走带外通道交传输层执行（`code_review` / `file_download` / `credential_reveal`），**绝不进对话历史/云端**。取代了早期"在字符串里塞魔法 JSON 标记 + 扫描消息历史"的脆弱侧信道。工具仍可直接返回 `str`，由 controller 归一化。

### 5.5 `core/context.py` — 应用上下文与会话
`SessionManager` 按 `session_id` 维护独立 `JarvisController`（解决多标签/多连接共享历史）；`AppContext` 为进程内共享容器，挂在 `app.state.ctx`。

### 5.6 `core/controller.py` — 主控对话循环（L1）
`JarvisController` 持有 `AsyncOpenAI` 客户端、`messages`、`pending_actions`。`chat()` 组装 system prompt（人格+时间+**联网能力声明**）→ 流式调用 → 工具调用循环（`_execute_tool` 统一经 `registry.get_handler`，结果归一化为 `ToolResult`，动作累积到 `pending_actions`）。已无全局单例、无 BUILTIN 双路径。

**结构化事件双通道（2026-06-17）**：`chat()` 不再 `yield` 裸字符串，而是产出 dict 事件——`{"type":"text","text":...}`（模型正文分片）与 `{"type":"tool","name":...}`（工具进度）。把「对话内容」和「工具进度」彻底分开：进度不再混进正文、不会残留进历史，传输层据此发两种 WS 帧（`chunk` / `tool_status`），前端把 `tool_status` 渲染成低调 chip。`scheduler` 也只累加 `text` 事件。

**健壮性（2026-07-12）**：① 模型调用**加超时**（`_LLM_TIMEOUT` 默认 120s，env 可调）——此前 SDK 默认 600s，一次卡住就静默长挂、灯不黄也没回复。② `_compress_history` 的摘要调用在每轮最开头、出任何字之前发生，一旦卡住整轮就冻住 → 给它更紧的超时 + **失败即优雅退化**（省略早期历史继续对话，绝不挂起）+ 限制摘要输入规模。③ 工具轮次上限从写死 12 提到 `_MAX_TOOL_ROUNDS`（默认 30，env 可调），并新增**卡循环检测**（连续数轮调用完全相同即判无进展、提前停）；到上限时**不丢进度**，提示回复「继续」可从断点续跑。④ L4 情节落盘/召回的嵌入（同步、首次下载模型）一律 `asyncio.to_thread` 后台化 + 压缩钩子发射即忘，**绝不阻塞事件循环**。

**联网自知**：`_network_capability_note()` 据 `config.CLAUDE_MODEL` 是否含 `:online` 在 system prompt 里明确告知模型"能/不能联网"，避免模型凭空拒绝或假装联网。

**工具按域渐进披露（现已默认【开】，`config.PROGRESSIVE_TOOLS` / `JARVIS_PROGRESSIVE_TOOLS=0` 关闭）**：开启时每轮只暴露「核心常驻工具（`CORE_TOOL_NAMES`：发文件/记事实/读文件/查两个保险箱）∪ 已激活领域 + `load_tools` 元工具」，其余领域工具靠模型调 `load_tools(group=…)` 按需加载——工具层重构后（29 工具 / 9 个清晰分组）小模型路由更准。`load_tools` 的描述动态列出尚未加载的领域及其工具；模型若直接调用某未暴露工具，其 handler 仍在全量注册表里照常执行，并自动激活该组（纯加法、不破坏调用）。关闭时 `get_exposed_tools()` 逐字等价 `get_all_tools()`（每轮全量），为向后兼容保留。

### 5.7 `core/memory.py` — 加密 KV 存储 + 共享基础设施
SQLite `memory` + `snapshots` 两表；Fernet 加密；密钥存系统钥匙串。**2026-06-17 起其角色变为纯基础设施**：面向模型的记忆库工具已下线，`build_context_block()` 不再注入 system prompt。但本模块的 `encrypt`/`decrypt`/`_get_conn`/`_fernet_instance` 仍被**证件保险箱 `vault`、`availability` 以及新的 `core/history.py` 复用**（共用同一个 `memory.db`、各自独立表），不可删除。

### 5.7a `core/profile.py` — 用户档案 / core memory（新增）
MemGPT/Letta 式 "core memory" 的轻量单用户版：一小块【每轮注入 system prompt】、可被模型追加、可被用户在设置面板增删的长期硬事实（风险偏好、家庭成员、长期目标、关键日期等）。与 history 互补——history 是会被压缩/滚出窗口的原始流，profile 是钉住不淡化的蒸馏事实。存同一 `memory.db`（独立表 `core_memory`），有条数/字数上限以保持"小而精"，避免退化成旧记忆库的盲目注入。模型经 `remember_fact` 工具（`connectors/profile_tools.py`，仅写）追加；读取无需工具（已常驻注入）。REST：`/api/profile` 增删查改（设置面板）。`controller._build_system_prompt` 每轮拼入 `profile.build_block()`。

### 5.7b `core/history.py` — 对话持久层（新增）
把对话 transcript 落盘到同一个 `memory.db`（`conversations` + `chat_messages` 两表，按 `conversation_id` 多会话设计），让用户**刷新 / 关标签 / 换设备后仍能回看此前对话**。`append`/`get_messages`/`clear`/`list_conversations`。当前前端只用一个固定的 `DEFAULT_CONVERSATION`（单一主对话），schema 已预留多会话、扩展零迁移。传输层每轮把 user/assistant 文本（及安全的文件卡片）落盘；**证件揭示等敏感动作绝不入库**。新会话控制器内存为空时由 `web/chat._seed_controller_from_history` 用历史文本回灌，使模型也能跨设备延续，而不仅是界面能回看。

### 5.7c `core/entities.py` — 实体记忆 L2（新增 · 2026-07-12）
关于「一个个具体对象」（客户 / 供应商 / 料号 / 报价等）的结构化事实，与 profile（关于用户本人）互补。通用 `kind + name + fields(JSON) + notes + tags` 模型，不为每种类型硬编码表结构；**精确匹配优先**（料号、公司名要准，不走向量），辅以子串模糊查。**不常驻** system prompt，由模型按需用工具查/记。存同一 `memory.db`（独立表 `entities`，`UNIQUE(kind,name)`、`upsert` 浅合并）。工具见 `connectors/entity_tools.py`（`save_entity`/`lookup_entity`/`search_entities`/`list_entities`）。

### 5.7d `core/episodic.py` + `core/embedding.py` — 情节记忆 L4（新增 · 2026-07-12）
一段段「发生过什么、聊过什么」的自由文本（对话摘要、过往结论），随时间累积、**不常驻**，由模型带着问题用 `recall(query)` **语义召回** top-k。写入两条来源：① `controller._compress_history` 把被压缩掉的「早期对话摘要」自动落盘并向量化（真人会话，补上原先"摘要用完即蒸发"的缺口，`source=compress`）；② 模型主动 `remember_episode`（`source=model`）。存同一 `memory.db`（独立表 `episodes`，向量以 blob 行内存储，每行记 `dim`，检索只比对同维度行）。`core/embedding.py` 为本地嵌入底座：优先 fastembed（`multilingual-e5-small`，onnxruntime 本机推理，**文本不出机器**），缺模型时自动降级为字符 3-gram 哈希向量，保证始终可用；单用户几千条量级直接 numpy 余弦，无需向量数据库。工具见 `connectors/episodic_tools.py`。写工具 `save_entity`/`remember_episode` 已入 `BACKGROUND_BLOCKED_TOOLS`，后台/工作流实例不写个人记忆。

### 5.8 `core/safety.py` — 输入边界
`safe_name`/`safe_filename`/`under_base`：技能名、上传文件名、路径拼接的防穿越校验。

### 5.9 `core/tool_builder.py` — 工具自建系统
模型运行时"写工具给自己用"。`create_tool`/`edit_tool` 调模型生成代码 → `validate_tool_code`（AST 沙箱：禁危险 import、查可疑模式、SQL 注入提示）→ 存草稿 → 审查（`code_review` 动作）→ 激活动态加载注册。`create_tool` 生成后若静态校验有**阻断级错误**，会把错误喂回模型**自动重生成一次**，减少一上来就是坏代码的草稿。元工具（建/改/删工具、定时任务、`send_file_to_chat`）在模块导入时自注册。`load_all_active_skills` 已加固：坏 `meta.json` 跳过告警而非崩溃。

读/审/激活三工具（2026-07-16）——把"造完工具后的处理"从依赖本机网页解耦，飞书对话内也能完整闭环：`read_tool_code(name)` 按名读源码+校验结果（并在描述里明确"你能读"，根治模型"读不了 py"的幻觉）；`review_tool(name)` 随时把某草稿的代码+校验重新调出来（网页重弹卡片 / 飞书重发代码），解决"审查窗口滚走后调不回来"；`activate_tool(name)` 对话内激活草稿（激活前重新静态校验），不再依赖网页「激活」按钮。三者均归 `authoring` 组；`activate_tool` 与建/改/删一样在后台/定时实例被 `BACKGROUND_BLOCKED_TOOLS` 屏蔽。飞书侧 `lark_bridge._dispatch_action` 的 `code_review` 分支已从"提示去本机网页"改为**真正下发代码 + 校验摘要卡片**，并提示回复「激活 X」即可生效。

工具创建框架重设计·一期（2026-07-16，详见 `docs/archive/tool_authoring_redesign.md`（方案已实施、归档））——根治"盲写→幻觉不存在的内部 API→静态校验放行→真跑才崩"这一类。`skill_policy.BUILDING_BLOCKS` 声明【允许技能复用的第一方 building block】（如 `prospecting.hubspot_worker`/`login_manager`）；`building_blocks_api_text()` 用 AST 抽它们的**真实公共签名**注入 `CODE_GEN_PROMPT`，并加"看不到其它内部模块、绝不臆造 API、缺能力就诚实报缺"的边界声明；校验器放行这些复用 import（仍禁 `core/connectors/config/main`）。**二期（2026-07-16 已实现）**：`check_building_block_usage` 用 AST 抓"在复用的 building block 类上调用了不存在的方法/属性"这类幻觉（如 `HubSpotBrowser.create`），纳入 `validate_tool_code` 为**阻断级** + 附真实可用 API；`activate_skill` 激活前先在**隔离子进程**做 import 冒烟（只 import + 断言 `TOOL_DEF`/handler 存在，**不执行 handler**，捕获 ImportError/缺依赖/模块级崩溃/卡死超时），过了才在主进程 exec+注册。**三期（2026-07-16 已实现）**：`create_tool`/`edit_tool` 统一走 `_author_verified_loop`——生成 → 静态+一致性校验 → 隔离子进程冒烟 → 失败把**真实报错**喂回重生成（最多 `_TOOL_AUTHOR_MAX_ATTEMPTS`，默认 3 轮），全部门过才停、用尽仍不过则诚实交付最后一版 + 未过原因；到用户面前的草稿默认已"通过校验+冒烟"。**参考式造工具（两趟版，2026-07-16 已实现）**：`core/source_read.read_symbol(module,name)` 按符号读源码片段（省 token），也作 `read_symbol` 工具暴露；`tool_builder._gather_references` 在写代码前用轻模型门控地问"要不要参考、读哪些符号"（自包含工具回 NONE 则零开销），把点名的**真实源码片段**注入生成提示词再进验证循环——于是"参考现有代码理解真实用法/网页结构/选择器再写新代码"成立。更彻底的"完整 agentic 造工具子 agent（多轮自主读任意源码 + 自纠，复用 controller 循环）"作为可选未来项，见设计文档（含 token 经济性/鲁棒性设计）。

注册表可替换/可注销 + `update_tool_code`（2026-07-16）——修掉"编辑/重建已激活工具在不重启进程时刷新不了"的死结。根因：`register_spec` 旧语义是"重名首次优先、后者静默跳过"，且 `delete/deactivate` 从不动内存注册表 `_SPECS`——于是一个工具第一次激活后，其登记（schema+handler）就被永久锁死，edit/delete/recreate 都撬不动（表现为"框架缓存了旧注册信息"）。修法：① `ToolSpec` 加 `origin`（builtin/skill）；`register_skill_tool` 用 `replace=True` **允许自建技能替换自己之前的注册**（编辑→重激活即时生效），但**仍拒绝用技能覆盖第一方工具**；② `deactivate_skill`/`delete_skill` 调 `registry.unregister` 从运行中的注册表**即时注销**（第一方拒绝注销）；③ 新增元工具 `update_tool_code(name, code)`——把**确切代码原样写入**（不经模型改写/重生成），补上"我已写好完整代码、只想原样保存"的缺失入口（`create_tool`/`edit_tool` 都会让模型重写）。

### 5.10 `core/scheduler.py` — 定时任务
APScheduler；任务存 `schedules/<名>/config.json`；触发时用独立 `JarvisController` 执行，结果写入本地文件投递。

### 5.11 `connectors/` — 第一方工具（全部 `@tool`）
`document`（`read_document`，见下）、`credentials`（证件 5 个）、`doc_vault`（文档保险箱）、`profile_tools`（`remember_fact` 写用户档案）、`calendar_tools`（内置日历建/读/改/删，group=`calendar`）、`self_review_tools`（`run_self_review` 触发自我迭代反思，group=`self`）；`vault`（证件加密存储核心）、`cred_ocr`（本地 OCR）、`calendar_providers`（派生来源 provider，导入即注册）、`calendar_card`（近期日程情报卡）为被调用的非工具助手/注册模块。

**`connectors/document.py`（`read_document`）— 本地优先 + 云端 OCR 兜底（2026-07-16）**：支持 PDF/DOCX/XLSX/PPTX/CSV/TXT/图片。PDF 走决策树、隐私优先：① 先本地 `pdfplumber` 读，抽到足够文字（`_pdf_is_poor` 阈值判定）直接返回，零成本不上云；② 判为扫描件（几乎无文字层）时按 `core/sensitivity.py` 的敏感度分流——**非敏感**→ 交 OpenRouter `file-parser` 插件云端 OCR（`PDF_CLOUD_ENGINE`，默认 mistral-ocr）；**敏感**→ 绝不上云、仅本机并诚实报错；**存疑**→ 返回提示让模型先问用户，同意后以 `cloud="allow"` 重调、拒绝 `cloud="deny"`。③ 本地空提取 / Office 空壳一律明确报错，不再静默返回空壳。总开关 `PDF_CLOUD_FALLBACK`（设 0 彻底关闭云端）。`core/sensitivity.py` 判定顺序：硬规则·敏感（保险箱数据目录 / 文件名敏感词 / 局部内容出现身份证号·护照号·银行卡号 Luhn）→ 硬规则·非敏感（说明书/规格书/白皮书等公开技术资料）→ 轻模型语义兜底（`SENSITIVITY_LLM`，拿不准强制 `uncertain`，fail-safe）。

### 5.13 自我认知：`core/self_model.py` + `connectors/self_inspect.py`（新增）

让贾维斯「读得到自己」。`core/self_model.py` 是**代码边界的单一事实源**：把所有源码划为 🔒核心 PROTECTED（框架与安全不变量，不自动迭代；要改须人工 + 影响 + 动机）与 🟢周边 OPEN（业务能力，可自我迭代）。三条不变量焊死在内：未列入 OPEN 的一切默认按 PROTECTED 处理（fail-safe，新文件天生受保护）；`tests/` 受保护（测试是「绿了就自动生效」的安全网，禁止自改作弊）；`self_model.py` 自身受保护（不能偷挪护栏）。`classify(path)` / `is_writable_by_self_iteration(path)` 供后续自迭代闭环判定。镜像见 `docs/SELF_MODEL.md`。

`connectors/self_inspect.py` 暴露两个**只读**第一方工具（group=`self`）：`list_self_modules`（列模块地图 + 分区 + 行数）、`read_self_source`（按边界读源码并标注归属）。纯只读、有仓库围栏（`under_base`）、拒读密钥/运行态（`.env`、`data/`、`.git` 等）。

### 5.14 自我迭代闭环：`core/self_iteration.py` + `core/self_review.py`（🔒 受保护）

在「读得到自己」之上再让贾维斯「安全地改自己」。二者均属 PROTECTED（自身不可自改，否则可拆掉自己的护栏）。

- `core/self_iteration.py`（执行器）：把一条**已生成**的优化提案安全落地——区位判定 → 文件快照 → **先红后绿**验证（新测试改前必失败、改后必通过，挡空测试）→ **全量 gate**（`run_all` 必过，挡改坏别处）→ 通过则保留(+git 提交)、任一步失败精确回滚。安全靠机械护栏：只写 OPEN 路径，PROTECTED 物理拒写；自动测试只能新建 `test_auto_*.py`、绝不覆盖既有测试。生成（调 LLM）刻意留在上层，核心机制可确定性单测。
- `core/self_review.py`（反思编排）：读上轮复盘 → 构建反思 prompt → 调模型生成提案 → 按区位路由（OPEN 交执行器自动落地；PROTECTED 生成 `code_review` 提案送人工审）→ 写本轮复盘（下轮先读）。LLM 经 `model_fn` 注入，编排可脱离真实模型单测。触发经 `connectors/self_review_tools.py` 的 `run_self_review` 工具（有副作用，仅用户显式或定时触发，且先告知）。

### 5.15 `core/calendar.py` — 内置日历（时间真源，🟢 周边）

系统的**单一时间事实源**（设计见根目录《内置日历设计.md》）。一张可扩展事件表 `calendar_events`（`kind` + `meta` JSON 袋 → 新事件种类零迁移；`rrule` 字段 → 重复事件为第一类公民，走 `dateutil.rrule.between` 区间展开）。真源 vs 派生：用户事件/休假/一次性提醒入表；证件保单到期、定时任务下次运行等不入表，由各 `register_source(name, fn)` 注册的 provider（`calendar_providers.py`：证件到期/文档到期/定时下次/档案关键日）**读时现算**。`agenda(start, end)` 合并两者出一条排好序的时间轴。存同一 `memory.db`（独立表），与 history/profile/vault 同构。REST 见 `web/calendar.py`，情报卡见 `calendar_card.py`。

### 5.16 `core/schedule_presets.py` — 定时任务预设目录

前端可视化启动用的「可启动任务」纯数据目录（`PRESETS`）：每条含标题、说明、默认 cron、以及**服务端预置的 prompt**（不经前端，避免提示词注入）。前端列预设卡 + 频率选择器，一键 `create_from_preset` 即按所选 cron 复用 `core.scheduler` 建真实定时任务。新增可启动任务 = 往 `PRESETS` 加一条。

### 5.12 `config.py` — 配置
`Settings(BaseSettings)` 承载 env/`.env`（密钥、模型、对话参数、`JARVIS_HOST/PORT`），带类型与校验；模块级大写常量向后兼容导出。路径常量（`DATA_DIR`/`FRONTEND_DIR`/`UPLOAD_DIR`/`DOWNLOAD_DIR` 等）按平台推导。

---

## 6. 证件保险箱安全边界（重点设计，未变）

1. 证件存独立 `credentials` 表、字段整体 Fernet 加密；**绝不被 `build_context_block()` 注入云端 prompt**。
2. 模型只接触代号 `alias`、类型、字段名、脱敏预览、有效期。
3. 真实值只在两处：磁盘密文 + 用户本机浏览器。`reveal_credential` 工具只返回脱敏 `text` + 一个 `credential_reveal` 动作（仅携 `alias` + 字段名）；由 `web/chat.py` 在本机 `vault.get_fields` 解密后经 WebSocket 直推浏览器。
4. 照片本机 OCR 不上云；扫描时加密暂存，确认时归档或丢弃，未归档定时清理。

---

## 7. 关键数据流

### 7.1 一次带工具调用的对话
```
浏览器 ─ws(message+session_id)─▶ web/chat
   └▶ ctx.sessions.get(session_id) → controller.chat(msg)
        ├ 组装 system(人格+时间+记忆) ；流式调 OpenRouter → 文本分片 ─ws─▶ 浏览器
        ├ finish=tool_calls? → _execute_tool(registry.get_handler) → ToolResult
        │     · text 入对话历史；actions 累积到 pending_actions → 再次调模型
        └ 结束：controller.drain_actions() → web/chat._dispatch_actions
              （code_review / file_download / credential_reveal 三类带外动作）
```

### 7.2 自建工具 / 定时任务
`create_tool → 生成（阻断级错误自动重生成一次）→ 验证 → code_review 审查 → 激活动态加载`；审查/激活不再只能走本机网页——`read_tool_code`/`review_tool`/`activate_tool` 让整条链在对话内（含飞书）闭环。`create_schedule → config.json + APScheduler → 触发用独立 Controller 执行 → 投递`。

---

## 8. 配置与运行 / 分发

- 配置：环境变量或项目根 `.env`（见 `分发与部署.md` 与 `.env.example`）。
- 源码运行：`pip install -e .` 然后 `python main.py`（或 `jarvis`），默认 `http://127.0.0.1:8000`。
- wheel：`python -m build --wheel`（注意 wheel 不含 `frontend/` 静态资源）。
- Docker：`docker build -t jarvis .`；镜像默认 `JARVIS_HOST=0.0.0.0`，容器须显式提供 `MEMORY_ENCRYPTION_KEY`，并挂卷持久化 `/root/.jarvis`。
- 可选能力：`pip install -e ".[documents]"` / `".[ocr]"` / `".[all]"`。

---

## 9. 代码审查建议关注方向

> 重构已解决：脆弱字符串侧信道（→ ToolResult/Action）、全局单例共享历史（→ 会话分桌）、路径 hack（→ 可安装包）、风格不一（→ 全 `@tool` + 单一 registry）、坏 meta.json 崩溃（→ 加固）。以下为仍值得确认的点：

- **绑定与鉴权**：`/ws/chat` 与 `/api/*` 默认无鉴权，仅靠绑定 `127.0.0.1`；若改 `JARVIS_HOST=0.0.0.0` 暴露需自加反代+鉴权。
- **自建工具沙箱强度**：`validate_tool_code` 为静态分析，激活后在主进程 `importlib` 执行，无运行时隔离；`open(`/白名单外模块仅警告级。
- **会话内存**：客户端自带 `session_id` 的会话不会自动回收，长期运行有缓慢增长（可加 TTL/LRU）。
- **SQLite 并发**：每次操作新建连接，无连接池/WAL 显式配置。
- **密钥管理**：记忆库与证件库共用同一 Fernet 密钥；确认轮换与备份策略。
- **错误吞没**：仍有若干 `except Exception: pass`（动作分发、图片清理等）。
- **写入稳定性**：项目目录若位于同步盘（OneDrive 等），`meta.json` 等小文件重写可能被截断；建议放非同步本地路径。
- **list_skills_info 健壮性**：与 `load_all_active_skills` 同类，坏 `meta.json` 仍可能影响 `/api/tools`，可同样加固。

---

## 10. 重构后架构总览（2026-06-17）

本轮把若干能力归并到**同构的注册表家族**，并理清了三种信息面与一套工作流范式。

### 10.1 注册表家族（统一的扩展模式）

| 注册表 | 模块 | 注册什么 | 触发/呈现 |
|---|---|---|---|
| 工具 | `core/registry` | `@tool` 函数 | 模型 function-calling |
| 自建技能 | `tool_builder` + `skills/` | 运行时生成的工具 | 沙箱校验后激活 |
| 定时任务 | `core/scheduler` | cron 任务（prompt） | APScheduler |
| 报告类型 | `core/reports` | 自描述报告 spec | `generate_report` 工具 / `/api/reports` |
| 情报台卡片 | `core/intel_cards` | 仪表盘卡片 provider | `/api/intel/dashboard` |
| 工作流 | `core/workflow_registry` | 多步骤工作流 | `run_workflow` 工具 / `/api/workflows` / 调度 |
| 用户档案 L1 | `core/profile` | 关于用户的长期硬事实 | 每轮注入 system prompt |
| 实体记忆 L2 | `core/entities` | 具体对象的结构化事实 | `lookup_entity`/`search_entities`/`save_entity`（按需） |
| 情节记忆 L4 | `core/episodic`+`embedding` | 过往经过/摘要（向量） | `recall`（语义召回）/ 压缩自动落盘 |
| 日历来源 | `core/calendar` | `register_source` 派生来源 | `agenda()` / `/api/calendar/agenda` |
| 定时预设 | `core/schedule_presets` | 可启动任务预设 | 前端预设卡 / `create_from_preset` |

新增一项能力 = 注册一条，前端/模型零散逻辑不外溢；其中报告/工作流/卡片的目录都会注入 system prompt 让模型可发现。

### 10.2 三种信息面（各管一件事）

- **情报台**（`intel_cards` + `/api/intel/dashboard` + 抽屉面板）：实时、按异常浮现、可一眼扫的**工作驾驶舱**。卡片按 `action/monitor/status` 三层；空卡自动隐藏；"今日潜客名单"是待办层置顶主角。新接信息域（如月度投资）= 注册一张卡。
- **对话**（`/ws/chat` + `history`）：临时问答 + 持久 transcript。默认用对话回答，不产工件。
- **报告/工作流产出**：**冻结成档的工件**（报告 PDF、潜客 xlsx），经结构化通道产出、归档、可在线查看，绝不污染对话。

### 10.3 对话与记忆分层（2026-07-12 起从三层扩为常驻 + 按需两大类）

**每轮常驻（自动进 system prompt，无需工具读）：**
- **工作记忆** `controller.messages`（含 `_compress_history` 压缩；真人会话压缩时把早期摘要自动落 L4）。
- **core memory / 用户档案(L1)** `core/profile.py`：少量长期硬事实，每轮钉进 system prompt，不随历史淡化。

**按需检索（不常驻，模型主动调工具捞）：**
- **实体记忆(L2)** `core/entities.py`：具体对象（客户/料号等）的结构化事实，**精确+模糊查**，不走向量。工具 `lookup_entity`/`search_entities`/`save_entity`。
- **情节记忆(L4)** `core/episodic.py` + `core/embedding.py`：过往对话摘要/经过的自由文本，**本地嵌入语义召回**（`recall`）；长对话压缩摘要自动入库，不再蒸发。

**持久 transcript** `core/history.py`：落盘 + 回放 + 新会话回灌，跨刷新/设备续聊（供人回看/回灌，非语义检索层）。

> 分工一句话：L1 管「我是谁、怎么做事」（常驻）；L2 管「精确的实体事实」（查表）；L4 管「过去发生过什么」（语义召回）。读走"常驻自动注入 / 按需工具捞"两条路；写走"模型判断 remember_*/save_entity + 压缩自动落 L4"。旧的 key-value「记忆库」工具仍是下线状态；`core/memory.py` 作为加密存储基础设施，被 vault / history / profile / calendar / **entities / episodic** 共用同一 `memory.db`、各自独立表。

### 10.4 工作流范式

`core/workflow.py`（引擎：有序 Step + 共享 ctx + abort/skip/degrade + 重试 + `StopWorkflow` 干净收尾 + 可观测 `WorkflowRun`）＋ `core/workflow_registry.py`（注册/运行/落盘运行记录）。旗舰 `prospect_daily`（`intel/workflow_defs.py`，v0.5）：选节点（产品类目 × 区域，**或接续存盘批**）→ 联网生成候选 → **checkpoint 落盘**（下游任何失败都不浪费这次生成）→ preflight（HubSpot 未登录 → 通知去登录 + `StopWorkflow`，**不出半成品名单、不推进树**；登录后重跑直接续存盘批）→ **HubSpot 富化**（`pipeline.enrich_records` + matcher）→ **多级排序**（CRM 状态 > confidence）→ 出 xlsx + 落「今日名单」喂情报台卡 → 推进树 → 清存盘批。树全部跑完同样走 `StopWorkflow`（提示 `python -m prospecting.reset` 复位），不是故障。

> **v0.4：潜客轨与信号库彻底解耦。** `prospecting` 包不 import `intel.*`（`tests/test_prospecting.py` 用 AST 钉死）。潜客名单服务 **0→1 大范围开发**（周期以月计、公司级粒度），信号服务**存量决策**（时效以周计、赛道/元件级粒度）——「这周工业自动化在冒余料」并不改变你要不要给某家公司打第一通电话；而且赛道级信号会让**一整批候选拿到同一个分数**，在名单内部几乎没有区分度，只是让不同日子的名单互相不可比。随之移除：`attach_intent`、`signal_check` 步与过期横幅、`intent_score`/`intent_tier`/`weight`/`surplus_signals`/`sources` 列、`node_sectors`、树的 `signal_sectors`。信号侧（采集/日报/`expire_stale`）完全不受影响。
>
> **v0.5：瘦身 + 降级路径重做。** ① `assemble` 步删除（只剩打没人读的 `track` 标记）；② 潜客历史库整条删除（`history.py`、`history_mark`/`history_record` 步、`seen_before` 列与排序级——跨批重复率低，不值一个存储层）；③ 降级从「全批标 `pending` 照样出表」改为**存盘-通知-续跑**（旧路径降级批照样 `mark_done`，节点被消耗、永远补不上匹配，"一键补匹配"从来不存在）；④ 生成提示词硬排除**中国大陆与香港公司**（按母公司/总部判，开发难度过高）；⑤ HubSpot 拉起/登录统一走 `prospecting/hubspot_session`（screener 验证过的 headless 先试 → 有头轮询流程），`make_hubspot_runtime`、screener、`login_manager` 三处同源，profile 与登录态共用。

**触发纪律**（system prompt 政策 + 工具描述双重约束）：报告与工作流**默认不做**，只在用户显式索取或定时触发；有副作用的工作流（开浏览器/连 HubSpot）**跑前先告知并确认**；不明确先问。UI 触发（情报台"运行潜客名单"按钮、报告中心生成按钮）等同显式动作。

### 10.5 前端形态

单页 `index.html`：常驻聊天 + 一个"功能"键开右侧抽屉，抽屉内标签切换 `情报台 / 定时任务 / 保险箱 / 设置`（记忆库按钮已移除，HubSpot 登录移入设置·连接）。WS 分两条通道：`chunk`（正文）与 `tool_status`（工具进度），进度不再混进正文/历史。`session_id` 存 `localStorage`、加载即回放历史。

### 10.6 运行前置（潜客工作流实跑需要）

`data/prospect_tree.json`（潜客树，**仓库内自有副本**，就地推进）+ 已登录 HubSpot + playwright。缺登录→候选存盘 + 通知去「本地调试」登录，登录后重跑直接续跑匹配（v0.5，不再出半成品名单）；缺树→`select` 步失败并记入运行记录。报告与情报台其余功能不依赖这些。

### 10.7 潜客树：产品类目 × 区域（`data/prospect_tree.json`）

12 个赛道 / 76 个**产品类目**叶子（v2.0）。骨架取自海关 HS 编码 84/85/87/88/90/94 章，只保留「含板级电子的成品设备」，剔除元件本体（8532/8533/8541/8542）与原材料——HS 的价值是 **MECE**：能回答「到底扫完没有」，这是自己拍脑袋列赛道永远答不了的。但 HS 分的是**流动的货物**不是**企业**，所以 `label` 一律用人话产品类目（「工业摄像头与机器视觉系统」），`hs_codes` 只在背后框定边界并喂给生成提示词当搜索抓手。

### 区域是独立维度（v2.0）：节点 =（产品类目 × 区域）

`regions` 用 `EU / NA / SEA` 三个代号，人话名在树顶层 `regions_meta`；`select_node` 展开成人话再进提示词（否则模型得自己猜 "SEA" 指哪些国家）。

**一次只发一个区域。** 76 类目 × 3 区域 = **228 个节点**，一天一个约 7.5 个月跑完一轮。

> **为什么不在一次输出里覆盖三个区域**：一次生成的预算与注意力有限，让模型同时兼顾多个区域必然厚此薄彼——实际表现是把力气全花在第一个区域，后两个根本不看，而且**不报错**。这是结构性的，改提示词解决不了（v1 曾写「第一个区域挖不出新的了就往下走」，但欧洲一个类目根本挖不尽，等价于只做欧洲）。所以把「一次搜索内的分配问题」改成「跨天的调度问题」。
>
> **为什么同一类目要连跑完三个区域再换**（`_pick` 里 `started` 优先级最高）：这样才能拿到**同类目、同口径、同模型**的区域对照——「工业机器人：欧洲 45 家 / 美洲 28 家 / 东南亚 9 家」。若允许中途跳走，对照就散成一堆拼不起来的马赛克（数控只扫过美洲、机器人只扫过欧洲），任何时点停下来都得不到完整视图。
>
> **平均分配是错的**：各区域真实供给密度差好几倍，强行均分会让稀薄的区域靠凑数——正是提示词里刚修掉的毛病换个地方复发。

状态记在 `leaf.done_regions[]`；三个区域都跑完 `leaf.status` 才转 `done`，据此重算赛道 `partial/done`。`mark_node_done(tree, leaf_id, region)` **必须传 region**，否则会一次标掉整个类目、跳过另外两个区域。`node_key`（`leaf:region`）用于 xlsx 文件名与历史记账，保证三次扫描可区分、事后能做区域对照。

选节点优先级：已开跑但未跑完三区域的类目 > 所在赛道已 partial > DFS 文件序。

**两套分类学的接缝【已随 v0.4 解耦拆除】**：v0.4 之前潜客树叶子靠 `signal_sectors` 字段映射到信号库的 17 赛道词表（`select_node` 产出 `node_sectors` 查信号打意向分）。解耦后树与信号库没有任何关联，选节点是纯确定性推进；此段仅留作历史备注，防止有人照旧文档给树加回 `signal_sectors`。

### 10.8 潜客历史库【v0.5 已整条删除】

原 `prospecting/history.py`（SQLite，域名/公司名归一化主键）负责给跨批重复公司打 `seen_before` 标记。v0.5 删除：同一类目的三个区域不会撞车，只有相邻类目偶发重叠——重复率撑不起一个存储层 + 两个工作流步骤 + 一列表格。偶发撞车由人在表里自行判断。随之删除 `seen_before` 列与第三排序级、`reset.py` 的历史库清理项（保留对老机器残留 `prospect_history.db*` 的顺手清除）。

### 10.9 机会轨已迁往日报（契约 v0.2，**名单只有覆盖轨一条**）

信号点名的具体公司（原 `track=opportunity`）不再拼进潜客名单，改为市场情报日报的「**点名公司（金线索）**」板块（`intel/report.py`）。日报本来就采到了这些公司却从不展示，反倒是潜客表把它们捡去拼在后面——这块内容原本就长错了地方。

拆开的三条理由（详见 `prospect_pipeline_contract.md` §5）：**打分尺度不可比**（覆盖轨 intent 是信号强度求和、无上限；机会轨是 `severity×2`、封顶 10）、**资格口径不一致**（覆盖轨过了 OEM 画像筛，机会轨一道筛都没过）、**队列 vs 快照**（名单是队列，同一家天天重出是缺陷；日报是快照，窗口内重复是正确的）。

> ⚠ `sl.company_pointed_signals()` 是**全库唯一没有强度衰减的消费口**（其余都走 `decayed_strength`，老信号会自己淡出）。它是裸筛 `status='active' AND surplus_implication>=N`，**调用方必须自己给 `days` 窗口**，否则会把库里累积的所有点名公司全捞出来——这正是它挂在潜客名单上时每天刷屏的原因。日报传 `days=14`（与 `query_report` 同口径）。
>
> 另注：`sl.expire_stale()` 现已挂在 `signal_collection` 工作流末尾（`ingest` 之后、`on_error=skip`）。此前它**写好了却没有任何调用方**，导致 `signals.status` 恒为 `active`——对覆盖轨/热点赛道无害（它们靠 `decayed_strength` 收敛而非 status），但那一列长得像「活跃/过期」的开关却是假的，将来谁写 `WHERE status='active'` 都会静默拿到全部历史。接上之后这列说真话，库也自己收敛。
>
> 顺序不能反：排在 `ingest` 之后，本次刚采的信号 `date_collected` 是今天、不会被误伤；而 `ingest` 的合并逻辑会把再次见到的老信号刷新并复活成 `active`，所以反复出现的行情不会被清掉。

**已知代价（有意接受）**：点名公司不再过 HubSpot 匹配——日报纯渲染、不跑浏览器，所以它回答「发生了什么」，不回答「这家是否已被认领」。要跟进就走潜客链单独查。

### 10.10 大批量生成的两条路必须同构（截断保底）

「让模型吐一大坨 JSON」在本项目有**两条路**：`signal_collection`（采集信号）与 `prospect_daily` 的 `generate` 步（生成候选公司）。两者都会撞 `max_tokens`，所以必须共用同一套保底，否则修好一条、另一条继续踩。

- **预算**：都用 16000（`_COLLECT_MAX_TOKENS` / `_GEN_MAX_TOKENS`，均可用 env 覆盖），**不能用 `config.MAX_TOKENS_RESPONSE`（4096）**——那是给聊天回复定的护栏。一个节点最多 100 家候选、每家还要写具体的 `contact_rationale`，约需 8k–15k tokens。
- **解析**：都走 `core/json_salvage.salvage_json_array()`，按括号深度逐个抢救完整的顶层对象，只丢被截断的最后一条。
- **调用方式**：都是**直连 `AsyncOpenAI`**（一次带 `:online` 的文本补全），不走 `JarvisController`——这两步不需要工具循环，联网检索由模型内建。
- **失败要响**：解析不出任何条目时**抛错**而不是返回空，否则工作流会「显示成功但库/名单是空的」。

> **这曾经是真 bug**：潜客生成原先用 `JarvisController`（4096 护栏）+ 朴素解析（`find("[")` … `rfind("]")` … `json.loads`，失败 `return []`）。截断时 `rfind("]")` 会命中内层 `"components":[...]` 的收尾方括号，切出的片段非法 → 返回空 → **前面已生成的几十家全部丢弃**，表现为「工作流跑成功但一家都没有」。采集轨早就修好了这两点，生成轨一直没跟上。`tests/test_prospecting.py` 现在把「截断仍救回」「两边预算同档」「共用同一实现」都钉死了。
