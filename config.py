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

    # 模型（OpenRouter 格式 provider/model-name；:online 启用内置联网）
    claude_model: str = "deepseek/deepseek-v4-flash:online"
    claude_model_light: str = "deepseek/deepseek-v4-flash"

    # 对话控制
    max_history_turns: int = 20
    max_tokens_response: int = 4096
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
    # 常驻核心工具（仅在渐进披露开启时有意义；按工具名常驻、与组无关）。
    # 选取标准：任意对话里都可能随时需要的跨域工具——发文件、记长期事实、读文件、
    # 查两个保险箱（"我有哪些证件/保单"）。其余领域工具靠 load_tools 按需加载。
    core_tool_names: str = "send_file_to_chat,remember_fact,read_document,list_credentials,list_documents"

    # 服务绑定（默认仅本地回环；容器/公网部署用 JARVIS_HOST/JARVIS_PORT 覆盖）
    host: str = Field(default="127.0.0.1", validation_alias="JARVIS_HOST")
    port: int = Field(default=8000, validation_alias="JARVIS_PORT")

    # 潜客树文件路径（潜客工作流读它选节点）。留空则回退 DATA_DIR/prospect_tree.json。
    # 例：指向 Autoworker 里在用的那份。
    prospect_tree_path: str = Field(default="", validation_alias="JARVIS_PROSPECT_TREE")

    # 信号新鲜度阈值（天）。潜客工作流跑前自检：信号库最近采集日期超过此天数即视为
    # "过期"——仍照常出名单，但在推送/xlsx/情报台卡上标注"意向排序仅供参考"。
    signal_freshness_days: int = Field(default=7, validation_alias="JARVIS_SIGNAL_FRESHNESS_DAYS")


settings = Settings()

# ── 向后兼容的模块级常量（全代码库以 config.X 访问）────────────────
OPENROUTER_API_KEY  = settings.openrouter_api_key
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CLAUDE_MODEL        = settings.claude_model
CLAUDE_MODEL_LIGHT  = settings.claude_model_light

MEMORY_DB_PATH        = DATA_DIR / "memory.db"
MEMORY_ENCRYPTION_KEY = settings.memory_encryption_key

MAX_HISTORY_TURNS         = settings.max_history_turns
MAX_TOKENS_RESPONSE       = settings.max_tokens_response
CONTEXT_WINDOW_SOFT_LIMIT = settings.context_window_soft_limit

SESSION_TTL_SECONDS = settings.session_ttl_seconds
MAX_SESSIONS        = settings.max_sessions

HOST = settings.host
PORT = settings.port

PROSPECT_TREE_PATH = settings.prospect_tree_path
SIGNAL_FRESHNESS_DAYS = settings.signal_freshness_days

# 渐进披露（默认开）
PROGRESSIVE_TOOLS = settings.progressive_tools
CORE_TOOL_NAMES   = tuple(
    n.strip() for n in settings.core_tool_names.split(",") if n.strip()
)
