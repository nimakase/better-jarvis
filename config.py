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
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    memory_encryption_key: str = ""    # 留空则首次运行自动生成并存系统钥匙串

    # 模型（OpenRouter 格式 provider/model-name；:online 启用内置联网）
    claude_model: str = "deepseek/deepseek-v4-flash:online"
    claude_model_light: str = "deepseek/deepseek-v4-flash"

    # 对话控制
    max_history_turns: int = 20
    max_tokens_response: int = 4096
    context_window_soft_limit: int = 800_000

    # 服务绑定（默认仅本地回环；容器/公网部署用 JARVIS_HOST/JARVIS_PORT 覆盖）
    host: str = Field(default="127.0.0.1", validation_alias="JARVIS_HOST")
    port: int = Field(default=8000, validation_alias="JARVIS_PORT")


settings = Settings()

# ── 向后兼容的模块级常量（全代码库以 config.X 访问）────────────────
OPENROUTER_API_KEY  = settings.openrouter_api_key
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CLAUDE_MODEL        = settings.claude_model
CLAUDE_MODEL_LIGHT  = settings.claude_model_light

FEISHU_APP_ID     = settings.feishu_app_id
FEISHU_APP_SECRET = settings.feishu_app_secret
FEISHU_BASE_URL   = "https://open.feishu.cn/open-apis"

MEMORY_DB_PATH        = DATA_DIR / "memory.db"
MEMORY_ENCRYPTION_KEY = settings.memory_encryption_key

MAX_HISTORY_TURNS         = settings.max_history_turns
MAX_TOKENS_RESPONSE       = settings.max_tokens_response
CONTEXT_WINDOW_SOFT_LIMIT = settings.context_window_soft_limit

HOST = settings.host
PORT = settings.port
