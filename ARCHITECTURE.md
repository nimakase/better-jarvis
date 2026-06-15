# 贾维斯（Jarvis）架构文档

> 个人 AI 助理 · 本地优先（local-first）· 代码审查参考基线
> 整理日期：2026-06-15 · 对应代码：`git 8a6b4ed`（初始版本）

---

## 1. 一句话定位

一个跑在本机的个人 AI 助理：FastAPI + WebSocket 后端，PWA 网页前端，主控模型走 OpenRouter（DeepSeek V4 Flash）。核心能力包括加密记忆库、证件保险箱、飞书（日历/消息）集成、文档读取、**模型自建工具**、**定时任务**。设计上把高敏感数据（证件真实号码）严格隔离在本机，绝不进入对话历史或云端。

---

## 2. 技术栈

| 层 | 选型 |
|----|------|
| Web 框架 | FastAPI + Uvicorn |
| 实时通信 | WebSocket（`/ws/chat` 流式对话） |
| 前端 | 单页 PWA（`frontend/index.html` + Service Worker） |
| LLM 接入 | OpenRouter（OpenAI 兼容 SDK `AsyncOpenAI`） |
| 主控模型 | `deepseek/deepseek-v4-flash:online`（`:online` 启用联网） |
| 持久化 | SQLite（`memory.db`，单库多表） |
| 加密 | `cryptography` Fernet，密钥存系统钥匙串（`keyring`） |
| 定时 | APScheduler（`AsyncIOScheduler`，挂在 FastAPI 事件循环上） |
| 文档解析 | pdfplumber / python-docx / openpyxl / python-pptx |
| 本地 OCR | rapidocr-onnxruntime（纯 CPU） |

---

## 3. 分层结构

```
                          浏览器（PWA 前端）
                                │
        WebSocket /ws/chat      │      REST /api/*
                                ▼
┌───────────────────────────────────────────────────────────┐
│  main.py  ── 入口 / FastAPI 应用 / 路由 / WebSocket 编排      │
│  · 注册所有工具（meta → feishu → document → credential）     │
│  · 流式转发对话 + 扫描 tool 结果中的「侧信道动作」标记         │
│  · 证件 / 技能 / 文件上传下载 / 记忆 的 REST 接口            │
└───────────────────────────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
┌──────────────┐      ┌──────────────────┐     ┌──────────────────┐
│ core/         │      │ connectors/       │     │ skills/           │
│ 主控与基础设施 │      │ 外部能力连接器     │     │ 模型自建工具（动态）│
│              │      │                  │     │                  │
│ controller   │◀────▶│ feishu           │     │ lookup_feishu_user│
│ memory       │      │ document         │     │ market_intel_report│
│ safety       │      │ credentials      │     │ ...（运行时增删）   │
│ scheduler    │      │ vault            │     └──────────────────┘
│ tool_builder │      │ cred_ocr         │
└──────────────┘      └──────────────────┘
        │                       │
        ▼                       ▼
   SQLite memory.db      OpenRouter / 飞书 API
   系统钥匙串(密钥)        本地 OCR 模型
```

工具注册顺序固定（`main.py` 顶部）：先内置元工具，再飞书、文档、证件连接器，最后启动时加载所有 `active` 的自建技能。所有工具最终汇入 `core/controller.py` 的全局注册表 `_tool_registry` / `_tool_definitions`，对模型呈现为统一的 OpenAI function-calling 工具列表。

---

## 4. 模块职责

### 4.1 `main.py` — 入口与编排

- **应用生命周期**：`lifespan` 中启动/停止 APScheduler，启动时 `load_all_active_schedules()`。
- **对话 WebSocket** `/ws/chat`：接收用户消息 → 处理特殊命令（`/reset`、`/memory`、`/skills`）→ 调 `controller.chat()` 流式回吐 → 结束后扫描最近消息中的「动作标记」。
- **侧信道动作扫描**：tool 执行结果里若含特殊 JSON 标记，main 解析后通过 WebSocket 单独推事件给前端：
  - `__skill_action__: code_review` → 推 `code_review`（自建工具代码待审查激活）
  - `__file_action__: download` → 拷贝到下载目录，推 `file_download`
  - `__credential_reveal__` → **在本机解密真实字段**，推 `credential_reveal`（真实值只走「加密库 → main → 浏览器」）
- **REST 接口**：证件保险箱（增删查、扫描 OCR、揭示、照片）、技能管理（激活/停用/列表/查看代码）、记忆查看删除、文件上传下载、健康检查、PWA 静态资源。

### 4.2 `core/controller.py` — 主控对话循环（L1）

- `JarvisController`（单例 `controller`）持有 `AsyncOpenAI` 客户端与 `self.messages` 对话历史。
- `chat()` 是核心循环：组装 system prompt（固定人格 + 当前时间 + 注入的记忆上下文）→ 流式调用模型 → 累积 tool_call 分片 → 若 `finish_reason == tool_calls` 则执行工具、把结果作为 `tool` 消息追加 → 继续循环直到模型产出纯文本。
- **工具注册表**：`register_tool(definition, handler)`，definition 用内部 `input_schema` 格式，发送前 `_to_openai_tool()` 转 OpenAI 格式。重复注册自动跳过（按 name 去重）。
- **内置工具**：`query_memory` / `write_memory` / `list_memory`（直接操作 `core.memory`）。
- **历史压缩** `_compress_history()`：超过 `MAX_HISTORY_TURNS`（20）或估算 token 超软上限（800k）时，用轻量模型把早期消息摘要成一条。
- **工具执行兼容层** `_safe_call()`：统一处理同步函数、async 函数、返回 coroutine 的 lambda。

### 4.3 `core/memory.py` — 记忆子系统（L5）

- SQLite 单库，两张表：`memory`（KV，可选过期 `expires_at`、可选 Fernet 加密 `encrypted`）与 `snapshots`（情节快照）。
- 加密密钥来源优先级：系统钥匙串 → `.env` 的 `MEMORY_ENCRYPTION_KEY` → 自动生成并写回钥匙串。
- `build_context_block()` 取最近更新的至多 20 条记忆，拼成文字注入 system prompt——**这是记忆影响模型的唯一通道**。
- 读取时惰性 `_purge_expired()` 清理过期项。

### 4.4 `core/safety.py` — 输入边界安全

- `is_safe_name` / `safe_name`：技能名、任务名只允许 `[A-Za-z0-9_-]{1,64}`，防路径穿越。
- `safe_filename`：清洗上传文件名，去目录/盘符、过滤危险字符、保留中文与扩展名。
- `under_base`：路径拼接兜底，resolve 后越出基目录则抛错。

### 4.5 `core/tool_builder.py` — 工具自建系统

模型可在运行时「写工具给自己用」，这是本项目最有特色也最需重点审查的部分。

- **流程**：用户描述需求 → `create_tool` 调模型生成代码 → 保存为 `skills/<name>/tool.py`（`draft` 状态）→ 前端 `code_review` 展示代码 + 验证结果 → 用户点激活 → `POST /api/tools/<name>/activate` → `importlib` 动态加载注册 → 立即可用，下次启动自动加载。
- **静态验证** `validate_tool_code()`（AST + 文本）：
  - 阻断级（`errors`）：语法错误、导入 `BLOCKED_IMPORTS`（内部模块 / subprocess / socket / ctypes / pickle / importlib 等）、缺 `TOOL_DEF`、缺 async 函数。
  - 警告级（`warnings`）：`os`/`sys` 导入、白名单外模块、`os.system`/`open(`/`__import__` 等可疑模式、SQL 字符串拼接（f-string/+/.format）。
  - 激活前会**重新验证**（防止绕过草稿直接改文件）。
- **元工具集**（注册给模型）：`create_tool`、`edit_tool`、`list_tools_meta`、`delete_tool`、`cleanup_drafts`、`send_file_to_chat`，以及调度相关 `create_schedule` / `list_schedules` / `delete_schedule` / `pause_schedule` / `resume_schedule`。
- 代码生成 prompt（`CODE_GEN_PROMPT`）内置了严格的格式、安全转义（SQL 参数化、路径白名单、URL 编码、HTML 转义）和跨平台（pathlib / `Path.home()`）规则。

### 4.6 `core/scheduler.py` — 定时任务

- APScheduler `AsyncIOScheduler`（时区 `Asia/Shanghai`），与 FastAPI 共用事件循环。
- 每个任务存 `schedules/<name>/config.json`（name、cron、prompt、delivery、status）。
- 触发时 `_run_job` 创建**独立** `JarvisController` 实例执行 prompt，不污染用户主对话历史；结果经 `_deliver` 推送：`feishu`（发消息）或 `file`（写 `~/jarvis_data/reports/`）。

### 4.7 `connectors/` — 外部能力

| 文件 | 暴露工具 | 说明 |
|------|----------|------|
| `feishu.py` | `get_calendar`、`create_calendar_event`、`get_feishu_messages`、`send_feishu_message` | 租户 access token 自动续期；不可逆操作（发消息/建日程）描述里要求先确认 |
| `document.py` | `read_document` | PDF/DOCX/XLSX/PPTX/TXT/MD/CSV/图片 统一读取 |
| `credentials.py` | `list_credentials`、`reveal_credential`、`ingest_credential_image`、`update_credential`、`delete_credential` | 注册给模型的证件工具；模型只见脱敏数据 |
| `vault.py` | （非工具，被 main/credentials 调用） | 证件加密存储核心 |
| `cred_ocr.py` | （非工具，本地 OCR） | rapidocr 解析证件，CVV/有效期策略见文件注释 |

### 4.8 `skills/` — 自建工具实例

当前两个：`lookup_feishu_user`（按手机/邮箱查飞书用户）、`market_intel_report`（每日电子元器件情报 PDF，依赖 Playwright）。每个目录含 `tool.py` + `meta.json`（status/description/validation）。

---

## 5. 证件保险箱的安全边界（重点设计）

这是全项目最关键的隐私设计，审查时应作为独立专题：

1. **存储隔离**：证件存独立的 `credentials` 表（非 `memory` 表），字段整体 Fernet 加密落盘；因此**绝不会**被 `build_context_block()` 注入到发往云端的 system prompt。
2. **模型可见面**：云端模型只接触代号 `alias`、证件类型、字段名、脱敏预览（尾号 1234）、有效期。真实号码 / CVV 从不进入对话历史。
3. **真实值通道**：真实值只出现在两处——磁盘加密密文，以及用户本机浏览器。揭示流程是 `reveal_credential` 工具仅返回脱敏确认，由 `main.py` 在本机解密后经 WebSocket **侧信道**直接推给浏览器。
4. **照片处理**：OCR 在本机完成不上云；扫描时照片加密暂存（`__stage__<token>`），用户确认时决定归档或丢弃，未归档的暂存定时清理；临时明文图用完即删。

---

## 6. 关键数据流

### 6.1 一次带工具调用的对话

```
浏览器 ─ws─▶ main /ws/chat
            └▶ controller.chat(msg)
                 ├ 组装 system(人格+时间+记忆上下文)
                 ├ 流式调 OpenRouter ──▶ 文本分片 ─ws─▶ 浏览器
                 ├ finish_reason=tool_calls?
                 │    └ _execute_tool(name,args) → tool 结果入历史 → 再次调模型
                 └ 纯文本结束
            └▶ 扫描最近消息的动作标记 → 按需推 code_review/file_download/credential_reveal
```

### 6.2 自建工具

```
用户需求 → create_tool → 模型生成代码 → save_skill_draft(draft) + validate
        → 前端 code_review 审查 → /api/tools/<name>/activate
        → 激活前再验证 → importlib 动态加载 → register_tool → 即时可用
```

### 6.3 定时任务

```
create_schedule → schedules/<name>/config.json + APScheduler 注册
   ⏰ cron 触发 → 独立 Controller 执行 prompt → _deliver(feishu | file)
```

---

## 7. 配置与运行

- `config.py`：路径（`BASE_DIR`/`SKILLS_DIR`/`SCHEDULES_DIR`，数据目录按平台落在 AppData / Application Support / `~/.jarvis`）、OpenRouter、飞书、记忆库、对话控制参数。
- 密钥经 `.env`（`.env.example` 为模板）：`OPENROUTER_API_KEY`、`FEISHU_APP_ID/SECRET`、可选 `MEMORY_ENCRYPTION_KEY`。
- 启动：`python main.py` → `http://localhost:8000`（`start.bat` / `start.sh` 为封装）。绑定 `127.0.0.1:8000`，公网访问见 `部署-公网访问.md`。

---

## 8. 代码审查时建议重点关注的方向

> 以下为基于架构梳理出的、值得在审查中逐一确认的关注点，非缺陷结论。

- **自建工具的沙箱强度**：`validate_tool_code` 是静态分析，激活后 `importlib` 在主进程内执行，无运行时隔离；`open(`、白名单外模块仅为警告级。审查这条信任链是否符合预期威胁模型。
- **绑定与鉴权**：`/ws/chat` 与 `/api/*` 默认无鉴权，仅靠绑定 `127.0.0.1`；结合「公网访问」文档需确认暴露面。
- **控制器单例与并发**：`controller` 为全局单例、`self.messages` 单一历史；多标签页/多并发 WebSocket 会共享同一会话历史。
- **侧信道动作扫描**：`_check_and_send_*` 用「最近 6 条消息找 `{...}` 子串解析 JSON」的方式识别动作，确认是否存在误匹配/漏匹配。
- **SQLite 并发**：每次操作新建连接，无连接池/WAL 显式配置；`data/` 下存在 `memory.db-journal`，确认并发写入行为。
- **密钥管理**：Fernet 密钥落系统钥匙串，记忆库与证件库共用同一密钥；确认密钥轮换与备份策略。
- **错误吞没**：多处 `except Exception: pass`（尤其 vault 图片清理、动作扫描），确认是否会掩盖问题。
- **飞书不可逆操作**：发消息/建日程的「先确认」仅靠 system prompt 约束，无代码级强制确认闸门。
