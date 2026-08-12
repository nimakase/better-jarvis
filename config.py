"""
配置（pydantic-settings）

env / .env 驱动的配置集中在 Settings 中，带类型与校验；模块顶层仍导出
与历史一致的大写常量（全代码库以 config.X 方式访问），因此本次升级对
所有调用方完全向后兼容。
"""

import os
import platform
import tempfile
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── 路径（由代码推导，非 env 驱动）──────────────────────────────
BASE_DIR = Path(__file__).parent
SKILLS_DIR = BASE_DIR / "skills"
SCHEDULES_DIR = BASE_DIR / "schedules"

# 数据存到系统用户目录，避免项目文件夹权限问题
# Windows: C:\Users\<user>\AppData\Roaming\Jarvis
# macOS:   ~/Library/Application Support/Jarvis
if platform.system() == "Windows":
    DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Jarvis"
elif platform.system() == "Darwin":
    DATA_DIR = Path.home() / "Library" / "Application Support" / "Jarvis"
else:
    DATA_DIR = Path.home() / ".jarvis"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Web / 文件目录
FRONTEND_DIR = BASE_DIR / "frontend"
UPLOAD_DIR   = Path(tempfile.gettempdir()) / "jarvis_uploads"
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "jarvis_downloads"
UPLOAD_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)


# ── env / .env 驱动的设置 ───────────────────────────────────────
class Settings(BaseSettings):
    """类型化、可校验的运行配置。环境变量优先，其次读取项目根 .env。"""
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 密钥（缺省留空；相关连接器在调用时给出清晰报错）
    openrouter_api_key: str = ""
    memory_encryption_key: str = ""    # 留空则首次运行自动生成并存系统钥匙串

    # 飞书（Lark）长连接凭据。两者都填才启用飞书桥；留空则不启动（见 main.lifespan）。
    # 绝不硬编码进源码——从 .env 读，避免随 git 泄露。
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    # 主动推送（定时任务的文字/文件）的收件人 open_id。留空则自动用「最近一个
    # 跟 jarvis 说过话的飞书用户」（单用户产品的合理默认，桥会把它记到数据目录）。
    feishu_push_open_id: str = ""
    # 官方 cardkit 流式（卡片实体 + 增量推文本，做原生打字机、免整卡重刷频率限制）。
    # 默认关：需在真实飞书环境联调验证后再开；开启后任一步失败会自动降级回
    # 「占位卡 + patch 整卡」的既有稳定流式（见 lark_bridge._stream_open）。
    feishu_native_streaming: bool = False

    # 模型（OpenRouter 格式 provider/model-name；:online 启用内置联网）
    claude_model: str = "deepseek/deepseek-v4-flash:online"
    claude_model_light: str = "deepseek/deepseek-v4-flash"

    # ── DeepSeek 官方 API 迁移（2026-08-07）──────────────────────────────
    # llm_provider 选主控走哪家："openrouter"（现状）｜"deepseek"（官方 API 直连）。
    # 官方 API 没有 :online 语法——联网检索改由 core/search_augment.py 显式补偿，
    # 见该模块注释。切换只改这一个开关，模型/base_url/key 全部跟着联动，
    # 不用满代码搜哪里硬编码了 OpenRouter。
    llm_provider: str = Field(default="openrouter", validation_alias="JARVIS_LLM_PROVIDER")
    deepseek_api_key: str = Field(default="", validation_alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com/v1",
                                   validation_alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field(default="deepseek-chat", validation_alias="DEEPSEEK_MODEL")
    deepseek_model_light: str = Field(default="deepseek-chat",
                                      validation_alias="DEEPSEEK_MODEL_LIGHT")

    # Exa 搜索（web_search 工具的后端之一；优先于 AnySearch——见 connectors/web_search.py
    # 的后端选择顺序，免费额度更大、多语言表现更好）。
    exa_api_key: str = Field(default="", validation_alias="EXA_API_KEY")
    exa_base_url: str = Field(default="https://api.exa.ai/search", validation_alias="EXA_BASE_URL")
    # 自设软上限（次/天）：Exa 免费额度是 2万次/月≈666/天，留点余量、避免月底意外超额。
    exa_daily_cap: int = Field(default=500, validation_alias="EXA_DAILY_CAP")

    # 对话控制
    max_history_turns: int = 20
    max_tokens_response: int = 4096
    # 工程改动（core/engineering.py 那一组工具激活时）用更宽的输出预算——写代码
    # 这类任务单次生成量天然更大，4096 这个默认值是给普通对话轮设的，偏保守。
    # DeepSeek 官方文档：max_tokens 合法范围到 8192（部分模型/模式下更高）；这里
    # 先按文档给的上限设，不是瞎猜。有了 patch_open_file/append_to_file 之后，
    # 大多数改动其实用不上这么多，这一档更多是给"确实要整篇重写/新建文件起始
    # 内容"这类兜底场景留余量，不是主要的解法（主要解法是把改动切小）。
    max_tokens_engineering: int = Field(default=8192, validation_alias="JARVIS_MAX_TOKENS_ENGINEERING")
    context_window_soft_limit: int = 800_000

    # 会话回收：客户端自带 session_id 的控制器若不回收会随进程缓慢增长内存。
    # 惰性回收——每次访问时清掉超 TTL 未活动的会话，并把驻留总数限制在
    # max_sessions（超出按 LRU 淘汰最久未用）。被淘汰的会话下次访问会从持久化
    # 历史透明重建，不影响跨设备续聊。设 0 关闭对应策略。
    session_ttl_seconds: int = 86_400      # 24h 未活动即可回收
    max_sessions: int = 200                # 同时驻留的会话数上限（LRU）

    # 工具按域渐进披露。默认【开启】：每轮只暴露「核心常驻工具 + load_tools」，
    # 其余按领域按需加载——工具层重构后（29 工具 / 9 个清晰分组）小模型路由更准。
    # 如需回到"每轮全量暴露"的旧行为，设 JARVIS_PROGRESSIVE_TOOLS=0。
    progressive_tools: bool = Field(default=True, validation_alias="JARVIS_PROGRESSIVE_TOOLS")
    # 权限闸（⑤二期，core/grants.check_tool）：工具所在模块必须已被人工审批覆盖
    # 当前代码才允许执行。默认【开启】——Ned 已用 audit_permission_grants /
    # approve_permission_grant 走完首轮审批（2026-08-12）。应急开关：
    # 若审批数据库损坏、或新增模块来不及批准导致误伤生产工具，设
    # JARVIS_PERMISSION_ENFORCEMENT=0 整体关闭本闸（回退到⑤一期：只审计不拦截）。
    permission_enforcement: bool = Field(default=True, validation_alias="JARVIS_PERMISSION_ENFORCEMENT")
    # 常驻核心工具（仅在渐进披露开启时有意义；按工具名常驻、与组无关）。
    # 选取标准：任意对话里都可能随时需要的跨域工具——发文件、记长期事实、读文件、
    # 查两个保险箱（"我有哪些证件/保单"）。其余领域工具靠 load_tools 按需加载。
    core_tool_names: str = "send_file_to_chat,remember_fact,read_document,list_credentials,list_documents,web_search"

    # 服务绑定（默认仅本地回环；容器/公网部署用 JARVIS_HOST/JARVIS_PORT 覆盖）
    host: str = Field(default="127.0.0.1", validation_alias="JARVIS_HOST")
    port: int = Field(default=8000, validation_alias="JARVIS_PORT")

    # 潜客树文件路径（潜客工作流读它选节点）。留空则回退 DATA_DIR/prospect_tree.json。
    # 例：指向 Autoworker 里在用的那份。
    prospect_tree_path: str = Field(default="", validation_alias="JARVIS_PROSPECT_TREE")

    # 信号新鲜度阈值（天）。潜客工作流跑前自检：信号库最近采集日期超过此天数即视为
    # "过期"——仍照常出名单，但在推送/xlsx/情报台卡上标注"意向排序仅供参考"。
    signal_freshness_days: int = Field(default=7, validation_alias="JARVIS_SIGNAL_FRESHNESS_DAYS")

    # 文档读取：本地优先，云端 OCR 兜底。仅当本地提取失败（扫描件/图片型 PDF、无文字层）
    # 且文件被判定为【非敏感】时，才把 PDF 交给 OpenRouter 的 file-parser 插件做云端 OCR。
    # 敏感或存疑一律留在本机（存疑会先问用户）。设 JARVIS_PDF_CLOUD_FALLBACK=0 彻底关闭云端。
    pdf_cloud_fallback: bool = Field(default=True, validation_alias="JARVIS_PDF_CLOUD_FALLBACK")
    # 云端 OCR 引擎：mistral-ocr（$2/1000 页，扫描件效果好）/ pdf-text（免费，仅文字层，等同本地）。
    pdf_cloud_engine: str = Field(default="mistral-ocr", validation_alias="JARVIS_PDF_CLOUD_ENGINE")
    # 敏感度判定是否在硬规则之外再调轻模型做语义判断（关掉则仅靠关键词/内容特征，其余一律存疑）。
    sensitivity_llm: bool = Field(default=True, validation_alias="JARVIS_SENSITIVITY_LLM")

    # AnySearch 结构化搜索（可选增强）：填 key 则 web_search 工具用它，
    # 未填/超额/失败一律优雅退回模型自带 :online 联网。base_url 可覆盖以防端点变更。
    anysearch_api_key: str = Field(default="", validation_alias="ANYSEARCH_API_KEY")
    anysearch_base_url: str = Field(default="https://api.anysearch.com/v1/search",
                                    validation_alias="ANYSEARCH_BASE_URL")
    anysearch_daily_cap: int = Field(default=900, validation_alias="ANYSEARCH_DAILY_CAP")


settings = Settings()

# ── 向后兼容的模块级常量（全代码库以 config.X 访问）────────────────
OPENROUTER_API_KEY  = settings.openrouter_api_key
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# LLM_PROVIDER 是单一事实源：core/llm.py 的 get_client() 只认 LLM_API_KEY/
# LLM_BASE_URL 这两个名字，不再关心到底是哪家——这样切换供应商不用改 llm.py，
# 只用改这里（或直接设 JARVIS_LLM_PROVIDER env）。CLAUDE_MODEL/CLAUDE_MODEL_LIGHT
# 也跟着联动，保持"全代码库以 config.CLAUDE_MODEL 访问模型名"这条既有约定不变——
# 迁移对调用方是透明的，不用满代码搜哪里硬编码了模型字符串。
LLM_PROVIDER = (settings.llm_provider or "openrouter").strip().lower()
DEEPSEEK_API_KEY  = settings.deepseek_api_key
DEEPSEEK_BASE_URL = settings.deepseek_base_url

if LLM_PROVIDER == "deepseek":
    LLM_API_KEY  = DEEPSEEK_API_KEY
    LLM_BASE_URL = DEEPSEEK_BASE_URL
    CLAUDE_MODEL       = settings.deepseek_model
    CLAUDE_MODEL_LIGHT = settings.deepseek_model_light
else:
    LLM_API_KEY  = OPENROUTER_API_KEY
    LLM_BASE_URL = OPENROUTER_BASE_URL
    CLAUDE_MODEL       = settings.claude_model
    CLAUDE_MODEL_LIGHT = settings.claude_model_light

MEMORY_DB_PATH        = DATA_DIR / "memory.db"
MEMORY_ENCRYPTION_KEY = settings.memory_encryption_key

ANYSEARCH_API_KEY   = settings.anysearch_api_key
ANYSEARCH_BASE_URL  = settings.anysearch_base_url
ANYSEARCH_DAILY_CAP = settings.anysearch_daily_cap

EXA_API_KEY  = settings.exa_api_key
EXA_BASE_URL = settings.exa_base_url
EXA_DAILY_CAP = settings.exa_daily_cap

FEISHU_APP_ID       = settings.feishu_app_id
FEISHU_APP_SECRET   = settings.feishu_app_secret

FEISHU_PUSH_OPEN_ID = settings.feishu_push_open_id
FEISHU_NATIVE_STREAMING = settings.feishu_native_streaming
# customer_loop/bitable 相关设置已迁到 prospecting/settings.py(2026-08-12,
# 诊断见项目记忆 jarvis-architecture-migration-plan ②)。

MAX_HISTORY_TURNS         = settings.max_history_turns
MAX_TOKENS_RESPONSE       = settings.max_tokens_response
MAX_TOKENS_ENGINEERING    = settings.max_tokens_engineering
CONTEXT_WINDOW_SOFT_LIMIT = settings.context_window_soft_limit

SESSION_TTL_SECONDS = settings.session_ttl_seconds
MAX_SESSIONS        = settings.max_sessions

HOST = settings.host
PORT = settings.port

PROSPECT_TREE_PATH = settings.prospect_tree_path
SIGNAL_FRESHNESS_DAYS = settings.signal_freshness_days

# 文档读取：云端 OCR 兜底
PDF_CLOUD_FALLBACK = settings.pdf_cloud_fallback
PDF_CLOUD_ENGINE   = settings.pdf_cloud_engine
SENSITIVITY_LLM    = settings.sensitivity_llm


# ── 供自建技能读取的配置（导出到 os.environ）────────────────────────────────
# 自建技能被沙箱禁止 import config/core，调 LLM 时只能从 os.environ 读密钥与模型。
# 但配置是 pydantic-settings 从 .env 读进来的，并不会自动进 os.environ——于是技能
# 要么读不到密钥直接报错，要么模型键缺失退回硬编码的错误默认值。这里把【技能确实
# 需要、且非敏感】的几项显式写回进程环境，让技能拿到与 app 完全一致的值。
# 【安全】只导出下列白名单；MEMORY_ENCRYPTION_KEY 等敏感项绝不导出。
def _export_env_for_skills() -> None:
    for k, v in {
        "OPENROUTER_API_KEY":  OPENROUTER_API_KEY,
        "OPENROUTER_BASE_URL": OPENROUTER_BASE_URL,
        # 供应商无关的名字（2026-08-07 新增）：技能应优先读这两个，而不是假设
        # 一定是 OpenRouter——LLM_PROVIDER=deepseek 时这两个会指向 DeepSeek 官方 API，
        # 上面两个 OPENROUTER_* 仍原样导出只为兼容已写死读它们的老技能。
        "LLM_API_KEY":         LLM_API_KEY,
        "LLM_BASE_URL":        LLM_BASE_URL,
        "CLAUDE_MODEL":        CLAUDE_MODEL,
        "CLAUDE_MODEL_LIGHT":  CLAUDE_MODEL_LIGHT,
        # 技能需要它才能定位到与 app 共用的运行态目录（如 HubSpot 浏览器 profile）。
        # 不导出的话，技能只能用 Path(__file__)/../../.. 猜出仓库根，于是在仓库里另建一份
        # chrome_profile —— 表现为「明明已经登录过 HubSpot，跑技能却还要再登一次」。
        "JARVIS_DATA_DIR":     str(DATA_DIR),
        "JARVIS_DOWNLOAD_DIR": str(DOWNLOAD_DIR),
    }.items():
        if v:
            os.environ[k] = v


_export_env_for_skills()

# 渐进披露（默认开）
PROGRESSIVE_TOOLS = settings.progressive_tools
PERMISSION_ENFORCEMENT = settings.permission_enforcement
CORE_TOOL_NAMES   = tuple(
    n.strip() for n in settings.core_tool_names.split(",") if n.strip()
)
