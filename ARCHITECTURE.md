# 贾维斯（Jarvis）架构文档

> 个人 AI 助理 · 本地优先（local-first）· 可安装 Python 包
> 更新日期：2026-06-15 · 对应代码：`git 553fe8d`（5 步重构 + 工具写法统一之后）

---

## 1. 一句话定位

一个跑在本机的个人 AI 助理：FastAPI + WebSocket 后端，PWA 网页前端，主控模型走 OpenRouter（DeepSeek V4 Flash）。核心能力包括加密记忆库、证件保险箱、飞书（日历/消息）集成、文档读取、**模型自建工具**、**定时任务**。高敏感数据（证件真实号码）严格隔离在本机，绝不进入对话历史或云端。

现已是可安装项目（`pip install -e .` / wheel / Docker），配置走 pydantic-settings，全部工具统一为 `@tool` 自注册并经单一注册中心管理。

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
                          浏览器（PWA 前端）
            sessionStorage 持久化 session_id（刷新续聊、多标签隔离）
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
| 内置记忆 | `connectors/memory_tools.py` | 3 | 第一方 |
| 元工具 | `core/tool_builder.py` | 11 | 第一方（管理系统自身） |
| 连接器 | `connectors/feishu/document/credentials.py` | 10 | 第一方（外部 API） |
| 自建技能 | `skills/<名>/tool.py` | 运行时可变 | **不可信·沙箱** |

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
按域拆分的 `APIRouter`：`pwa`（前端/PWA 静态）、`chat`（`/ws/chat` + 带外动作分发）、`credentials`、`skills`、`memory`、`files`。各端点逐字承接自原 `main.py`，逻辑不变。

`chat.py` 关键：每个 WebSocket 连接默认分配独立 `session_id`；客户端也可在消息里带 `session_id`（前端用 `sessionStorage` 持久化）以跨重连/刷新续聊；会话控制器由 `ctx.sessions` 管理，断开回收。聊天结束调用 `controller.drain_actions()` 取出带外动作并 `_dispatch_actions` 分发。

### 5.3 `core/registry.py` — 工具注册中心
见第 4 节。全项目工具的单一事实来源。

### 5.4 `core/results.py` — 结构化工具返回
`ToolResult(text, actions)` + `Action(type, payload)`。`text` 进对话历史给模型看（脱敏/确认），`actions` 走带外通道交传输层执行（`code_review` / `file_download` / `credential_reveal`），**绝不进对话历史/云端**。取代了早期"在字符串里塞魔法 JSON 标记 + 扫描消息历史"的脆弱侧信道。工具仍可直接返回 `str`，由 controller 归一化。

### 5.5 `core/context.py` — 应用上下文与会话
`SessionManager` 按 `session_id` 维护独立 `JarvisController`（解决多标签/多连接共享历史）；`AppContext` 为进程内共享容器，挂在 `app.state.ctx`。

### 5.6 `core/controller.py` — 主控对话循环（L1）
`JarvisController` 持有 `AsyncOpenAI` 客户端、`messages`、`pending_actions`。`chat()` 组装 system prompt（人格+时间+注入记忆）→ 流式调用 → 工具调用循环（`_execute_tool` 统一经 `registry.get_handler`，结果归一化为 `ToolResult`，动作累积到 `pending_actions`）。已无全局单例、无 BUILTIN 双路径。`register_tool` 从 registry 再导出以保持向后兼容。

### 5.7 `core/memory.py` — 记忆子系统（L5）
SQLite `memory` + `snapshots` 两表；敏感字段 Fernet 加密；`build_context_block()` 取最近记忆注入 system prompt（记忆影响模型的唯一通道）。

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
