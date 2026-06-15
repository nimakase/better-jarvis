import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── 路径 ──────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
SKILLS_DIR = BASE_DIR / "skills"
SCHEDULES_DIR = BASE_DIR / "schedules"

# 数据存到系统用户目录，避免项目文件夹权限问题
# Windows: C:\Users\<user>\AppData\Roaming\Jarvis
# macOS:   ~/Library/Application Support/Jarvis
import platform
if platform.system() == "Windows":
    DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Jarvis"
elif platform.system() == "Darwin":
    DATA_DIR = Path.home() / "Library" / "Application Support" / "Jarvis"
else:
    DATA_DIR = Path.home() / ".jarvis"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── OpenRouter ────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# 模型名称用 OpenRouter 格式：provider/model-name
# 完整列表见 https://openrouter.ai/models
CLAUDE_MODEL       = "deepseek/deepseek-v4-flash:online"  # 主控模型（:online 启用 OpenRouter 内置联网）
CLAUDE_MODEL_LIGHT = "deepseek/deepseek-v4-flash"         # 子任务/摘要用（压缩历史不需要联网）

# ── 飞书 ──────────────────────────────────────────────
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"

# ── 记忆库 ────────────────────────────────────────────
MEMORY_DB_PATH = DATA_DIR / "memory.db"
MEMORY_ENCRYPTION_KEY = os.getenv("MEMORY_ENCRYPTION_KEY", "")  # 留空则首次运行自动生成

# ── 对话控制 ──────────────────────────────────────────
MAX_HISTORY_TURNS = 20        # 超过此轮数触发摘要压缩
MAX_TOKENS_RESPONSE = 4096
CONTEXT_WINDOW_SOFT_LIMIT = 800_000  # token 软上限，超过则压缩历史（V4 Flash 支持 1M context）


# ── Web / 文件目录 ────────────────────────────────────
import tempfile

FRONTEND_DIR = BASE_DIR / "frontend"
UPLOAD_DIR   = Path(tempfile.gettempdir()) / "jarvis_uploads"
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "jarvis_downloads"
UPLOAD_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)
