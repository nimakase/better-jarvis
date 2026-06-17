# 贾维斯（Jarvis）架构文档

> 个人 AI 助理 · 本地优先（local-first）· 可安装 Python 包
> 更新日期：2026-06-17 · 本次改动：对话持久化 + WS 双通道 + 统一右侧抽屉 + 下线记忆库模型层 + 关闭渐进披露 + 联网自知

---

## 1. 一句话定位

一个跑在本机的个人 AI 助理：FastAPI + WebSocket 后端，PWA 单页前端（聊天 + 右侧功能抽屉），主控模型走 OpenRouter（DeepSeek V4 Flash，`:online` 联网）。核心能力：**持久化对话历史**（跨刷新/设备回看）、**常驻用户档案 core memory**、证件保险箱、飞书集成、文档读取、模型自建工具、定时任务、**可扩展报告框架**、**卡片化情报台**、**工作流引擎与注册表**（潜客→HubSpot）。高敏感数据（证件真实号码）严格隔离本机，绝不进对话历史/云端。

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
│ pwa          │      │ registry  ←──────┼─────│ feishu           │
│ chat (WS)    │─ctx─▶│ context(Session) │     │ document         │
│ credentials  │      │ controller       │     │ credentials      │
│ skills       │      │ results          │     │ memory_tools     │
│ memory       │      │ memory  safety   │     │ vault / cred_ocr │
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
| 元工具 | `core/tool_builder.py` | 11 | 第一方（管理系统自身） |
| 连接器 | `connectors/feishu/document/credentials/signal_intel/delivery/report/availability` | 多组 | 第一方（外部 API / 业务） |
| 自建技能 | `skills/<名>/tool.py` | 运行时可变 | **不可信·沙箱** |

> 注：面向模型的「记忆库」工具（`query_memory`/`write_memory`/`list_memory`）已于
> 2026-06-17 下线（精确 key 命中对 LLM 不友好、盲目注入更多是噪音）；对话连续性改由
> `core/history.py` 持久化 transcript 承担。`connectors/memory_tools.py` 已中和为空模块、
> 可 `git rm`。底层加密 KV 存储 `core/memory.py` 仍保留（vault / availability 依赖）。

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
按域拆分的 `APIRouter`：`pwa`、`chat`（`/ws/chat` + 带外动作分发）、`credentials`、`skills`、**`history`**（`/api/history` 回放/清空 + `/api/health`，取代原 `memory` 路由）、`files`、`schedules`、`push`、`intel`、`hubspot`、`reports`。

`chat.py` 关键：消费 `controller.chat()` 的结构化事件（text→`chunk` 帧、tool→`tool_status` 帧）；每轮把 user / assistant 文本落盘到 `core/history`（单一主对话 `CONVERSATION_ID`），新控制器内存为空时用历史回灌上下文。客户端用 `localStorage` 持久化 `session_id`（跨标签/设备续聊）。聊天结束 `drain_actions()` → `_dispatch_actions` 分发；文件卡片安全持久化、证件揭示绝不入库。

**前端单页 + 右侧抽屉**：`frontend/index.html` 是常驻聊天页，头部「情报台 / 定时任务 / 保险箱 / 设置」统一开同一个右侧 slide-over 抽屉（`#drawer`，标签切换面板），不再整页跳转——WS 不断、对话不丢。原 `/intel`、`/schedules` 独立页保留可直达，但内容已迁入抽屉面板复用同一批 REST API。「记忆库」按钮已移除。

### 5.3 `core/registry.py` — 工具注册中心
见第 4 节。全项目工具的单一事实来源。

### 5.4 `core/results.py` — 结构化工具返回
`ToolResult(text, actions)` + `Action(type, payload)`。`text` 进对话历史给模型看（脱敏/确认），`actions` 走带外通道交传输层执行（`code_review` / `file_download` / `credential_reveal`），**绝不进对话历史/云端**。取代了早期"在字符串里塞魔法 JSON 标记 + 扫描消息历史"的脆弱侧信道。工具仍可直接返回 `str`，由 controller 归一化。

### 5.5 `core/context.py` — 应用上下文与会话
`SessionManager` 按 `session_id` 维护独立 `JarvisController`（解决多标签/多连接共享历史）；`AppContext` 为进程内共享容器，挂在 `app.state.ctx`。

### 5.6 `core/controller.py` — 主控对话循环（L1）
`JarvisController` 持有 `AsyncOpenAI` 客户端、`messages`、`pending_actions`。`chat()` 组装 system prompt（人格+时间+**联网能力声明**）→ 流式调用 → 工具调用循环（`_execute_tool` 统一经 `registry.get_handler`，结果归一化为 `ToolResult`，动作累积到 `pending_actions`）。已无全局单例、无 BUILTIN 双路径。

**结构化事件双通道（2026-06-17）**：`chat()` 不再 `yield` 裸字符串，而是产出 dict 事件——`{"type":"text","text":...}`（模型正文分片）与 `{"type":"tool","name":...}`（工具进度）。把「对话内容」和「工具进度」彻底分开：进度不再混进正文、不会残留进历史，传输层据此发两种 WS 帧（`chunk` / `tool_status`），前端把 `tool_status` 渲染成低调 chip。`scheduler` 也只累加 `text` 事件。

**联网自知**：`_network_capability_note()` 据 `config.CLAUDE_MODEL` 是否含 `:online` 在 system prompt 里明确告知模型"能/不能联网"，避免模型凭空拒绝或假装联网。

**工具按域渐进披露（step7，现已默认关，`config.PROGRESSIVE_TOOLS` / `JARVIS_PROGRESSIVE_TOOLS=1` 开启）**：关闭时每轮把全部工具暴露给模型，行为简单可预测、不会在对话里冒出 `load_tools` 工具清单（这正是之前用户看到"一长串列表"的来源）。开启时才走「核心常驻工具 ∪ 已激活领域 + `load_tools` 元工具」的按需加载，`get_exposed_tools()` 关闭时逐字等价 `get_all_tools()`。机制代码保留，待来日在真实小模型上单独 A/B 再议。

### 5.7 `core/memory.py` — 加密 KV 存储 + 共享基础设施
SQLite `memory` + `snapshots` 两表；Fernet 加密；密钥存系统钥匙串。**2026-06-17 起其角色变为纯基础设施**：面向模型的记忆库工具已下线，`build_context_block()` 不再注入 system prompt。但本模块的 `encrypt`/`decrypt`/`_get_conn`/`_fernet_instance` 仍被**证件保险箱 `vault`、`availability` 以及新的 `core/history.py` 复用**（共用同一个 `memory.db`、各自独立表），不可删除。

### 5.7a `core/profile.py` — 用户档案 / core memory（新增）
MemGPT/Letta 式 "core memory" 的轻量单用户版：一小块【每轮注入 system prompt】、可被模型追加、可被用户在设置面板增删的长期硬事实（风险偏好、家庭成员、长期目标、关键日期等）。与 history 互补——history 是会被压缩/滚出窗口的原始流，profile 是钉住不淡化的蒸馏事实。存同一 `memory.db`（独立表 `core_memory`），有条数/字数上限以保持"小而精"，避免退化成旧记忆库的盲目注入。模型经 `remember_fact` 工具（`connectors/profile_tools.py`，仅写）追加；读取无需工具（已常驻注入）。REST：`/api/profile` 增删查改（设置面板）。`controller._build_system_prompt` 每轮拼入 `profile.build_block()`。

### 5.7b `core/history.py` — 对话持久层（新增）
把对话 transcript 落盘到同一个 `memory.db`（`conversations` + `chat_messages` 两表，按 `conversation_id` 多会话设计），让用户**刷新 / 关标签 / 换设备后仍能回看此前对话**。`append`/`get_messages`/`clear`/`list_conversations`。当前前端只用一个固定的 `DEFAULT_CONVERSATION`（单一主对话），schema 已预留多会话、扩展零迁移。传输层每轮把 user/assistant 文本（及安全的文件卡片）落盘；**证件揭示等敏感动作绝不入库**。新会话控制器内存为空时由 `web/chat._seed_controller_from_history` 用历史文本回灌，使模型也能跨设备延续，而不仅是界面能回看。

### 5.8 `core/safety.py` — 输入边界
`safe_name`/`safe_filename`/`under_base`：技能名、上传文件名、路径拼接的防穿越校验。

### 5.9 `core/tool_builder.py` — 工具自建系统
模型运行时"写工具给自己用"。`create_tool`/`edit_tool` 调模型生成代码 → `validate_tool_code`（AST 沙箱：禁危险 import、查可疑模式、SQL 注入提示）→ 存草稿 → 前端审查（`code_review` 动作）→ 激活动态加载注册。元工具（建/改/删工具、定时任务、`send_file_to_chat`）在模块导入时自注册。`load_all_active_skills` 已加固：坏 `meta.json` 跳过告警而非崩溃。

### 5.10 `core/scheduler.py` — 定时任务
APScheduler；任务存 `schedules/<名>/config.json`；触发时用独立 `JarvisController` 执行，结果经飞书或本地文件投递。

### 5.11 `connectors/` — 第一方工具（全部 `@tool`）
`feishu`（日历/消息 4 个）、`document`（`read_document`）、`credentials`（证件 5 个）、`memory_tools`（记忆 3 个）；`vault`（证件加密存储核心）、`cred_ocr`（本地 OCR）为被调用的非工具助手。

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
与重构前一致：`create_tool → 生成 → 验证 → code_review 审查 → 激活动态加载`；`create_schedule → config.json + APScheduler → 触发用独立 Controller 执行 → 投递`。

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
| 用户档案 | `core/profile` | 长期硬事实 | 每轮注入 system prompt |

新增一项能力 = 注册一条，前端/模型零散逻辑不外溢；其中报告/工作流/卡片的目录都会注入 system prompt 让模型可发现。

### 10.2 三种信息面（各管一件事）

- **情报台**（`intel_cards` + `/api/intel/dashboard` + 抽屉面板）：实时、按异常浮现、可一眼扫的**工作驾驶舱**。卡片按 `action/monitor/status` 三层；空卡自动隐藏；"今日潜客名单"是待办层置顶主角。新接信息域（如月度投资）= 注册一张卡。
- **对话**（`/ws/chat` + `history`）：临时问答 + 持久 transcript。默认用对话回答，不产工件。
- **报告/工作流产出**：**冻结成档的工件**（报告 PDF、潜客 xlsx），经结构化通道产出、归档、可在线查看，绝不污染对话。

### 10.3 对话与记忆三层

- **工作记忆** `controller.messages`（含 `_compress_history` 压缩）。
- **持久 transcript** `core/history.py`：落盘 + 回放 + 新会话回灌，跨刷新/设备续聊。
- **core memory** `core/profile.py`：少量长期硬事实，每轮钉进 system prompt，不随历史淡化。
- 旧的 key-value「记忆库」工具已下线（`memory_tools` 中和）；`core/memory.py` 降级为加密存储基础设施（vault / history / profile 共用）。

### 10.4 工作流范式

`core/workflow.py`（引擎：有序 Step + 共享 ctx + abort/skip/degrade + 重试 + 可观测 `WorkflowRun`）＋ `core/workflow_registry.py`（注册/运行/落盘运行记录）。旗舰 `prospect_daily`（`intel/workflow_defs.py`）：选节点→联网生成→接意向信号→**HubSpot 富化**（`pipeline.enrich_records` + matcher，登录失效则降级仅按意向排序）→排序→出富 xlsx + 落「今日名单」喂情报台卡。

**触发纪律**（system prompt 政策 + 工具描述双重约束）：报告与工作流**默认不做**，只在用户显式索取或定时触发；有副作用的工作流（开浏览器/连 HubSpot）**跑前先告知并确认**；不明确先问。UI 触发（情报台"运行潜客名单"按钮、报告中心生成按钮）等同显式动作。

### 10.5 前端形态

单页 `index.html`：常驻聊天 + 一个"功能"键开右侧抽屉，抽屉内标签切换 `情报台 / 定时任务 / 保险箱 / 设置`（记忆库按钮已移除，HubSpot 登录移入设置·连接）。WS 分两条通道：`chunk`（正文）与 `tool_status`（工具进度），进度不再混进正文/历史。`session_id` 存 `localStorage`、加载即回放历史。

### 10.6 运行前置（潜客工作流实跑需要）

`DATA_DIR/prospect_tree.json`（潜客树）+ 已登录 HubSpot + playwright。缺登录→自动降级出"仅按意向"名单；缺树→`select` 步失败并记入运行记录。报告与情报台其余功能不依赖这些。
